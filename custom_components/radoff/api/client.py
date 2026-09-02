"""Class which represent the Radoff API."""

import base64
import json
import logging
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter, Retry

from ..const import DEFAULT_CLIENT_ID, DEFAULT_POOL_ID, DEFAULT_POOL_REGION, USER_AGENT
from ..properties import MAPPING
from .auth import authenticate_user, compute_token_expiry
from .exceptions import APIAuthError, APIConnectionError, BearerTokenNotFoundError
from .models import Device, RadoffSensor

_LOGGER = logging.getLogger(__name__)

DEVICE_TYPES = ["Now+"]


def _safe_url(url: str) -> str:
    """
    Return `url` without its query string or fragment, safe to log.

    Query strings on these endpoints do not currently carry tokens, but
    stripping them keeps the log line safe even if that changes (see S-03/S6).
    """
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _get_request_id(response: requests.Response) -> str | None:
    """
    Return a backend request id from the response headers, if present.

    The exact header name used by the Radoff backend has not been confirmed
    with the API team (tracked in T-02); this checks the header names commonly
    used by AWS-fronted APIs and returns None rather than guessing further.
    """
    for header in ("x-request-id", "x-amzn-requestid", "x-amz-request-id"):
        value = response.headers.get(header)
        if value:
            return value
    return None


class API:
    """API platform."""

    BASE_DOMAIN = "https://api.iot.radoff.life/api/v1/core"
    DEFAULT_TIMEOUT = (10, 30)

    def __init__(  # noqa: PLR0913
        self,
        username: str,
        password: str,
        client_id: str = DEFAULT_CLIENT_ID,
        pool_id: str = DEFAULT_POOL_ID,
        pool_region: str = DEFAULT_POOL_REGION,
        domain_id: str = "",
    ) -> None:
        """
        Initialise.

        `client_id`, `pool_id` and `pool_region` default to this integration's
        own Cognito app client (see const.py) and are no longer expected to be
        supplied by the config flow or persisted in the config entry (S-02):
        they are internal production infrastructure, not per-user secrets.
        Callers may still override them explicitly (e.g. for a staging
        environment or in tests), but there is no user-facing UI for that.

        `domain_id` is the tenant domain already chosen for this account (persisted
        in the config entry after the config flow's discovery/selection step). It is
        used as-is for every subsequent call; no domain discovery happens here.
        """
        self.username = username
        self.password = password
        self.client_id = client_id
        self.pool_id = pool_id
        self.pool_region = pool_region
        self.connected: bool = False
        self.domain: str = domain_id
        self.tokens: dict = {}
        self._token_expires_at: float = 0

        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create a requests session."""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )

        adapter = HTTPAdapter(
            pool_connections=1,
            pool_maxsize=2,
            max_retries=retry_strategy,
        )

        # For all URLs starting with https:// use this adapter
        session.mount("https://", adapter)
        return session

    def _is_token_expired(self) -> bool:
        """Check if current token is expired or will expire soon."""
        if not self.tokens:
            return True
        return time.time() >= (self._token_expires_at - 300)

    @property
    def controller_name(self) -> str:
        """Return the name of the controller."""
        return "cloud_poller"

    def connect(self) -> bool:
        """
        Connect to api.

        Only performs Cognito authentication. Domain resolution is no longer done
        here: the domain to use is either already known (persisted `domain_id`,
        passed to `__init__`) or is discovered separately via `list_domains()`
        during the config flow (see S-01).
        """
        if self.username != "" and self.password != "" and self.client_id != "":
            _LOGGER.debug("Authenticating with AWS Cognito...")
            auth_data = authenticate_user(
                username=self.username,
                password=self.password,
                client_id=self.client_id,
                pool_id=self.pool_id,
                pool_region=self.pool_region,
            )
            if auth_data is not None and "AuthenticationResult" in auth_data:
                self.tokens = auth_data["AuthenticationResult"]

                expires_in = self.tokens.get("ExpiresIn", 3600)
                self._token_expires_at = compute_token_expiry(expires_in)
                _LOGGER.info("Token will expire in %d seconds", expires_in)

                self.connected = True
            return True
        msg = "Error connecting to api. Invalid authentication data."
        raise APIAuthError(msg)

    def disconnect(self) -> bool:
        """Disconnect from api."""
        _LOGGER.debug("Disconnecting from API")
        self.connected = False
        self.tokens = {}
        self._token_expires_at = 0

        # Note: self.domain is intentionally NOT cleared here. It is the
        # persisted tenant domain_id from the config entry, independent of the
        # Cognito session, and must survive a token-refresh reconnect.

        # Close and recreate session
        if self.session:
            self.session.close()
            self.session = self._create_session()

        return True

    def list_domains(self, bearer_token: str | None = None) -> list[dict[str, Any]]:
        """
        Return every domain the authenticated user has access to, unfiltered.

        No `parentDomainId` filtering is applied here anymore (see S-01): the
        caller (config flow) decides what to do with 1, more than 1, or 0 domains.

        The discovery endpoint requires a valid `x-domain` header to be sent even
        for the very first call: it responds 401 "Missing Domain Header" without
        one, and 401 "Authorization Validation Error" for a syntactically valid
        but unauthorized UUID. To bootstrap this without hardcoding any
        production UUID, we decode (without signature verification - these are
        public claims of the caller's own token) the Cognito IdToken and read the
        domain ids embedded in its "d_<domain-uuid>" claims, then use the first
        one as the initial `x-domain`. This was verified empirically against the
        real API (see card S-01).
        """
        if bearer_token is None:
            bearer_token = self._get_bearer_token()

        candidate_domain_ids = self._extract_domain_claims(bearer_token)
        if not candidate_domain_ids:
            _LOGGER.info(
                "No domain claims found in IdToken; user has no accessible domains"
            )
            return []

        url = f"{self.BASE_DOMAIN}/auth/user/me/domains"
        response = self.session.get(
            url,
            headers=self._get_headers(
                bearer_token=bearer_token, x_domain=candidate_domain_ids[0]
            ),
            timeout=self.DEFAULT_TIMEOUT,
        )

        if response.status_code != HTTPStatus.OK:
            _LOGGER.error(
                "Failed to get domains: status=%d, url=%s, request_id=%s",
                response.status_code,
                _safe_url(url),
                _get_request_id(response) or "n/a",
            )
            msg = f"Unable to retrieve the domain list (HTTP {response.status_code})."
            raise APIConnectionError(msg)

        resp_json = response.json()
        return resp_json.get("domains", [])

    def _extract_domain_claims(self, id_token: str) -> list[str]:
        """
        Extract candidate domain ids from the "d_<uuid>" claims of an IdToken.

        These are public (unsigned-read) claims of the caller's own Cognito
        IdToken; no signature verification is performed or needed, as this is
        only used to bootstrap the `x-domain` header for the discovery call, not
        to establish trust.
        """
        try:
            payload_segment = id_token.split(".")[1]
            padding = "=" * (-len(payload_segment) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_segment + padding))
        except (IndexError, ValueError, TypeError, UnicodeDecodeError) as err:
            _LOGGER.warning("Unable to decode IdToken claims: %s", err)
            return []

        if not isinstance(payload, dict):
            return []

        return sorted(
            key[2:] for key in payload if isinstance(key, str) and key.startswith("d_")
        )

    def get_devices(self) -> list[Device]:
        """Get devices on api."""
        # Check if token needs refresh
        if self._is_token_expired():
            _LOGGER.info("Token expired, reconnecting...")
            self.disconnect()
            self.connect()

        device_list: list[Device] = []

        url = f"{self.BASE_DOMAIN}/data/devices/search"
        post_obj = {"filter": {}, "take": 99}

        response = self.session.post(
            url,
            headers=self._get_headers(
                bearer_token=self._get_bearer_token(), x_domain=self.domain
            ),
            json=post_obj,
            timeout=self.DEFAULT_TIMEOUT,
        )

        self._check_response_status(response=response, url=url)

        devices = response.json()["devices"]
        _LOGGER.debug("Found %d devices in response", len(devices))

        for device in devices:
            if "deviceTypeName" in device and device["deviceTypeName"] in DEVICE_TYPES:
                sensors = self._get_data(device["id"])
                device_list.append(
                    Device(
                        device_id=device["id"],
                        device_serial=device["serial"],
                        device_type=device["deviceTypeName"],
                        name=device["name"],
                        sensors=sensors,
                    )
                )
        return device_list

    def _get_data(self, device_id: str) -> dict[str, RadoffSensor]:
        sensors: dict[str, RadoffSensor] = {}

        url = f"{self.BASE_DOMAIN}/data/devices/{device_id}"
        response = self.session.get(
            url,
            headers=self._get_headers(
                bearer_token=self._get_bearer_token(), x_domain=self.domain
            ),
            timeout=self.DEFAULT_TIMEOUT,
        )

        self._check_response_status(response=response, url=url)

        result = response.json()
        data = result.get("data", {})

        # Metadata only: bucket names and a count of readings, never the
        # sensor values themselves or the raw payload (see S-03/S5).
        _LOGGER.debug(
            "device %s: %d readings, keys %s",
            device_id,
            sum(len(v) for v in data.values() if isinstance(v, list)),
            sorted(data.keys()),
        )

        for k, v in MAPPING.items():
            if k in data:
                for obj in data[k]:
                    pn = obj["propertyName"]

                    av = obj["value"] if "value" in obj else obj["aggregationValue"]

                    if pn in v:
                        obj_map = v[pn]
                        fn = obj_map.get("normalize_fn", None)
                        sensors[pn] = RadoffSensor(
                            name=pn,
                            value=av,
                            device_class=obj_map["deviceClass"],
                            friendly_name=obj_map["friendlyName"],
                            unit=obj_map["unit"],
                            normalize_fn=fn,
                        )

        return sensors

    def _get_bearer_token(self) -> str:
        if self.tokens is not None and "IdToken" in self.tokens:
            return self.tokens["IdToken"]
        msg = "Error retrieving bearer token."
        raise BearerTokenNotFoundError(msg)

    def _get_headers(self, bearer_token: str, x_domain: str) -> dict[str, str]:
        return {
            "user-agent": USER_AGENT,
            "x-domain": x_domain,
            "accept-encoding": "gzip",
            "authorization": "Bearer " + bearer_token,
            "content-type": "application/json",
        }

    def _check_response_status(
        self, response: requests.Response, url: str = ""
    ) -> bool:
        """Check response status."""
        if response.status_code == HTTPStatus.OK:
            return True

        safe_url = _safe_url(url or response.url)
        request_id = _get_request_id(response)

        _LOGGER.warning(
            "API request failed: status=%d, url=%s, request_id=%s",
            response.status_code,
            safe_url,
            request_id or "n/a",
        )

        if response.status_code == HTTPStatus.UNAUTHORIZED:
            _LOGGER.debug(
                "Authentication token invalid (401) for domain %s, "
                "will reconnect on next request",
                self.domain,
            )
            self.disconnect()
            msg = "Authentication failed (HTTP 401 Unauthorized). Token may be expired."
            raise APIAuthError(msg)

        if response.status_code == HTTPStatus.FORBIDDEN:
            msg = "Access forbidden (HTTP 403). Check account permissions."
            raise APIAuthError(msg)

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            msg = (
                "API rate limit exceeded (HTTP 429). "
                "Please increase your polling interval in the integration options."
            )
            raise APIAuthError(msg)

        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            msg = (
                f"Radoff API server error (HTTP {response.status_code}). "
                "This is usually temporary - will retry automatically."
            )
            raise APIAuthError(msg)

        msg = f"API request failed with HTTP {response.status_code}."
        raise APIAuthError(msg)
