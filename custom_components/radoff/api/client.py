"""Class which represent the Radoff API."""

import base64
import json
import logging
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter, Retry

from ..const import (
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
    DEFAULT_SCAN_INTERVAL,
    USER_AGENT,
)
from ..properties import MAPPING
from .auth import AuthExpiredError, CognitoSession
from .exceptions import APIAuthError, APIConnectionError
from .models import DeviceFetchError, RadoffDevice, Reading, ReadingKey

_LOGGER = logging.getLogger(__name__)

DEVICE_TYPES = ["Now+"]

# T-02 CONFIRMED (probe run 2026-09-02 against a real account): objects in
# `data`, `aggregatedData` and `recalculatedData` carry only `propertyName`
# plus `value`/`aggregationValue` - no per-sample timestamp field exists at
# all. This tuple therefore stays EMPTY BY DESIGN, not "pending": if a future
# API version ever adds one, add its key here and `_parse_measured_at`
# already knows how to read it (ISO-8601, with or without a trailing "Z", or
# a Unix epoch number in seconds or milliseconds) - nothing else needs to
# change. Until then, `Reading.measured_at` stays `None` and
# `RadoffEntity.available` falls back to the device-level
# `RadoffDevice.last_data_received_at` (see `_get_data` below) instead of
# fully degrading to "the reading exists" - a real, if coarser, freshness
# signal the same probe run found sitting one level up in the same response.
_MEASURED_AT_KEYS: tuple[str, ...] = ()

# A numeric timestamp above this (~year 2100 expressed in seconds) is treated
# as milliseconds instead of seconds.
_EPOCH_MS_THRESHOLD = 4_102_444_800

# Errors from a single per-device `GET /data/devices/{id}` that `get_devices`
# isolates to that one device instead of letting them fail the whole poll
# (card S-13): an HTTP-level failure on that one request (`APIAuthError` -
# 403/429/5xx/other non-200, see `_check_response_status`) or a
# transport-level one (`requests.exceptions.RequestException` - connection
# error, read/connect timeout, malformed response body, ...). Deliberately
# NOT included: `AuthExpiredError`/`AuthInvalidError`/`AuthChallengeRequiredError`
# (all raised via `self._session.get_bearer()`, called from `_get_data`
# below) - those are about the Cognito *session*, not this one device, so
# `get_devices()` must let them propagate unisolated (card S-13, "COSA FARE"
# step 6: "Un errore auth su un dispositivo NON va isolato: risale, perché
# riguarda la sessione e non il dispositivo"). They are simply not part of
# this tuple, so a plain `except _ISOLATABLE_DEVICE_ERRORS` below never
# catches them - no special-casing needed.
_ISOLATABLE_DEVICE_ERRORS = (APIAuthError, requests.exceptions.RequestException)


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


def _parse_timestamp(raw: Any) -> datetime | None:
    """
    Best-effort parse of a single raw value as a timestamp.

    Accepts either an ISO-8601 string (with or without a trailing "Z") or a
    Unix epoch number in seconds or milliseconds. Anything else, or a value
    that fails to parse, returns `None` rather than raising: a single bad or
    absent timestamp must never be able to fail the whole poll (same
    "parsing robusto con fallback a None" requirement as card S-07's step 1).

    Assumes UTC when a parsed ISO-8601 string carries no explicit offset.
    T-02 CONFIRMED (probe run 2026-09-02) that `lastDataReceivedAt` (see
    `RadoffDevice.last_data_received_at`) never exercises that fallback in
    practice: real values look like `"2026-09-02T14:26:40.147Z"` -
    ISO-8601 with millisecond precision and an explicit trailing "Z", i.e.
    already UTC, same as `lastAggregatedDataReceivedAt` and `provisionedAt`
    sampled from the same response. The no-explicit-offset branch stays as
    a defensive fallback for any other timestamp this integration may read
    in the future, not because this field needs it.
    """
    try:
        if isinstance(raw, bool):
            # bool is an int subclass; never a plausible timestamp.
            return None
        if isinstance(raw, int | float):
            seconds = raw / 1000 if raw > _EPOCH_MS_THRESHOLD else raw
            return datetime.fromtimestamp(seconds, tz=UTC)
        if isinstance(raw, str):
            # `fromisoformat` accepts a trailing "Z" natively since 3.11.
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    except (ValueError, TypeError, OSError, OverflowError):
        _LOGGER.debug("Unable to parse %r as a timestamp", raw)
    return None


def _parse_measured_at(obj: dict[str, Any]) -> datetime | None:
    """
    Best-effort extraction of a per-sample timestamp from a reading object.

    Tries each of `_MEASURED_AT_KEYS` in order (see that tuple's comment for
    why it is empty today) and returns the first one that parses via
    `_parse_timestamp`, or `None` if none does.
    """
    for key in _MEASURED_AT_KEYS:
        if key in obj:
            parsed = _parse_timestamp(obj[key])
            if parsed is not None:
                return parsed
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
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
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

        `scan_interval` (card S-11) is the poll interval currently in effect
        for this account, in seconds, as resolved by the coordinator from
        `config_entry.options` (or `DEFAULT_SCAN_INTERVAL` if unset). It is
        not used to schedule anything here - `RadoffCoordinator.update_interval`
        remains the single source of truth for that - it is only surfaced in
        the HTTP 429 branch of `_check_response_status`, so a rate-limit error
        can name the interval actually in effect instead of a hardcoded
        number that may no longer match what the user configured.

        Card S-12: this class no longer holds any Cognito token state
        itself (`tokens`, `_token_expires_at`, `connected` are all gone) -
        that responsibility, plus the refresh-before-SRP logic, now lives
        entirely in the `CognitoSession` this constructor builds by
        composition. Every method below that needs a bearer token asks
        `self._session.get_bearer()` for one instead of reading `self.tokens`.
        """
        self.domain: str = domain_id
        self.scan_interval = scan_interval
        self._session = CognitoSession(
            username=username,
            password=password,
            client_id=client_id,
            pool_id=pool_id,
            pool_region=pool_region,
        )

        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """Create a requests session."""
        session = requests.Session()

        # Card S-13: total was 3 (three retries, four attempts total) with a
        # 0.5s backoff factor and a (10, 30)s per-request timeout - in the
        # worst case (every attempt hits the read timeout) that is minutes
        # per single device request, which on its own could blow well past
        # `update_interval`. `RadoffCoordinator.async_update_data` now caps
        # the whole poll cycle at `UPDATE_TIMEOUT_FACTOR * update_interval`
        # (see const.py) regardless of what happens here, so this Retry no
        # longer has to be the only thing standing between a slow backend
        # and an overrun cycle - reduced to total=2 (two retries, three
        # attempts total) so a single stuck device leaves more of that
        # overall budget for the other devices in the same poll.
        retry_strategy = Retry(
            total=2,
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

    @property
    def controller_name(self) -> str:
        """Return the name of the controller."""
        return "cloud_poller"

    def connect(self) -> bool:
        """
        Perform an initial Cognito login and return True on success.

        Card S-12 moves all token state and refresh/fallback logic into
        `CognitoSession` (`api/auth.py`); this method is kept as the thin,
        explicit entry point `config_flow.py` uses for setup and re-auth
        validation, where a full login is exactly what is wanted - there is
        no existing session yet to reuse there. It shares `get_bearer()`
        with every other caller rather than reaching into `CognitoSession`'s
        internal `_srp_login()` directly, so a fresh `API` instance's very
        first authentication attempt goes through the exact same code path
        as any later reconnect (`get_devices()` below).

        `coordinator.py` no longer calls this method directly (card S-12,
        "COSA FARE": "coordinator.py: non chiama più connect/disconnect
        direttamente") - `get_devices()` authenticates lazily via
        `get_bearer()` instead.

        Every outcome `CognitoSession.get_bearer()`/`authenticate_user()`
        know how to classify is raised as one of `AuthInvalidError`
        (credentials rejected outright, or no authentication data at all -
        card S-08), `AuthChallengeRequiredError` (Cognito wants a challenge
        this integration cannot complete, e.g. `NEW_PASSWORD_REQUIRED` or
        MFA - card S-09/C8) or `AuthUnavailableError` (Cognito unreachable
        or throttling - never a credentials problem, card S-09/C9) - this
        method does not need to inspect anything itself.
        """
        self._session.get_bearer()
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

        Only ever called from the config flow (initial setup or re-auth
        validation) - `RadoffCoordinator`'s periodic polling never calls this
        (card S-12 AC: "Nessuna chiamata a /auth/user/me/domains dopo il
        primo setup"): the `domain_id` it needs is the one already persisted
        on the config entry (see S-01), and `get_devices()` below never
        re-discovers it.
        """
        if bearer_token is None:
            bearer_token = self._session.get_bearer()

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

    def get_devices(self) -> tuple[list[RadoffDevice], list[DeviceFetchError]]:
        """
        Get devices on api.

        Card S-12: no longer checks token expiry or calls `disconnect()`/
        `connect()` itself - `self._session.get_bearer()` (used by
        `_get_headers` below via each request) handles refreshing or
        re-logging in lazily, on demand, the first time a bearer token is
        actually needed in this call.

        Card S-13 splits this into two isolated pieces:

        1. The `search` call (below) stays fully blocking: a failure here
           means the set of devices itself is unknown, so there is nothing
           to isolate - it still raises exactly as before (S-13 AC: "Un
           fallimento della search porta tutte le entità a unavailable,
           come oggi").
        2. The per-device loop that follows no longer lets one device's
           failure raise out of this method. Each `_get_data()` call is
           tried independently; an isolatable error (see
           `_ISOLATABLE_DEVICE_ERRORS`) is recorded as a `DeviceFetchError`
           and the loop moves on to the next device instead of aborting the
           whole poll (S-13 AC: "le entità dell'altro continuano ad
           aggiornarsi"). A session-level error (an auth exception not in
           that tuple) is NOT caught here and propagates out of this
           method, aborting the rest of the loop - by design, see
           `_ISOLATABLE_DEVICE_ERRORS`'s own comment and this card's "COSA
           FARE" step 6.

        `coordinator.py::RadoffCoordinator._merge_device_errors` is what
        turns the returned `device_errors` into `RadoffDevice` entries with
        `stale=True`, reusing the previous poll's readings when available.
        """
        devices_ok: list[RadoffDevice] = []
        device_errors: list[DeviceFetchError] = []

        url = f"{self.BASE_DOMAIN}/data/devices/search"
        post_obj = {"filter": {}, "take": 99}

        response = self.session.post(
            url,
            headers=self._get_headers(
                bearer_token=self._session.get_bearer(), x_domain=self.domain
            ),
            json=post_obj,
            timeout=self.DEFAULT_TIMEOUT,
        )

        self._check_response_status(response=response, url=url)

        devices = response.json()["devices"]
        _LOGGER.debug("Found %d devices in response", len(devices))

        for device in devices:
            if "deviceTypeName" not in device or device["deviceTypeName"] not in (
                DEVICE_TYPES
            ):
                continue

            device_id = device["id"]
            device_serial = device["serial"]
            device_type = device["deviceTypeName"]
            name = device["name"]

            try:
                readings, last_data_received_at = self._get_data(device_id)
            except _ISOLATABLE_DEVICE_ERRORS as err:
                _LOGGER.debug(
                    "Isolated per-device fetch error for device %s (%s): %s",
                    device_id,
                    device_type,
                    err,
                )
                device_errors.append(
                    DeviceFetchError(
                        device_id=device_id,
                        device_serial=device_serial,
                        device_type=device_type,
                        name=name,
                        error=str(err),
                    )
                )
                continue

            devices_ok.append(
                RadoffDevice(
                    device_id=device_id,
                    device_serial=device_serial,
                    device_type=device_type,
                    name=name,
                    readings=readings,
                    last_data_received_at=last_data_received_at,
                )
            )
        return devices_ok, device_errors

    def _get_data(
        self, device_id: str
    ) -> tuple[dict[ReadingKey, Reading], datetime | None]:
        """
        Fetch one device's readings, keyed by `(bucket, property_name)`.

        Card S-10 / finding C4: `MAPPING` (see `properties.py`) is now keyed
        by `Bucket` first, so this loop walks it bucket by bucket instead of
        over a single flat property-name namespace. `data.airqualityindex`
        and `aggregatedData.airqualityindex` therefore land in two distinct
        `readings` entries - `(Bucket.DATA, "airqualityindex")` and
        `(Bucket.AGGREGATED, "airqualityindex")` - instead of one overwriting
        the other depending on dict iteration order.

        Card S-13: every exception this method can raise - `AuthExpiredError`
        (via `get_bearer()` or a 401 from `_check_response_status`),
        `APIAuthError` (403/429/5xx/other from `_check_response_status`), or
        a `requests.exceptions.RequestException` (network/transport failure)
        - is left to propagate unchanged; `get_devices()` above is the one
        place that decides which of those get isolated to this one device.
        """
        readings: dict[ReadingKey, Reading] = {}

        url = f"{self.BASE_DOMAIN}/data/devices/{device_id}"
        response = self.session.get(
            url,
            headers=self._get_headers(
                bearer_token=self._session.get_bearer(), x_domain=self.domain
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

        # Device-level freshness signal (T-02 finding, card S-07): confirmed
        # absent per-sample, but this sibling field of the same response IS
        # "when this device last reported anything" - coarser than a
        # per-reading timestamp would be, but real, and what
        # RadoffEntity.available falls back to.
        last_data_received_at = _parse_timestamp(data.get("lastDataReceivedAt"))

        for bucket, bucket_mapping in MAPPING.items():
            if bucket not in data:
                continue
            for obj in data[bucket]:
                pn = obj["propertyName"]
                if pn not in bucket_mapping:
                    continue

                obj_map = bucket_mapping[pn]
                av = obj["value"] if "value" in obj else obj["aggregationValue"]
                measured_at = _parse_measured_at(obj)
                fn = obj_map.get("normalize_fn", None)

                readings[(bucket, pn)] = Reading(
                    name=pn,
                    bucket=bucket,
                    value=av,
                    device_class=obj_map["deviceClass"],
                    friendly_name=obj_map["friendlyName"],
                    unit=obj_map["unit"],
                    normalize_fn=fn,
                    measured_at=measured_at,
                )

        return readings, last_data_received_at

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
            # Card S-08: a 401 on an already-authenticated call means the
            # *session* behind the current token is no longer valid - it says
            # nothing yet about whether the configured password itself is
            # still correct. That is only known once the next reconnect
            # attempt actually replays it through `authenticate_user()` (see
            # `api/auth.py`), which is where a definitive `AuthInvalidError`
            # can be raised instead. Until then this stays the recoverable
            # case: invalidate the session's tokens (card S-12: NOT the
            # requests.Session itself - see `CognitoSession.invalidate`'s own
            # docstring for why recreating the HTTP transport on every
            # expired token was unwarranted) so the next call's
            # `get_bearer()` reconnects - preferring a refresh over a full
            # SRP login, since the RefreshToken is deliberately left intact -
            # and raise `AuthExpiredError` so `coordinator.py` keeps
            # retrying (`UpdateFailed`) instead of opening the re-auth flow
            # on every expired session.
            #
            # Card S-13: this is deliberately NOT one of the
            # `_ISOLATABLE_DEVICE_ERRORS` `get_devices()` isolates per
            # device - a 401 on a single device's GET means the whole
            # session's token is bad, not that one device, so it must
            # propagate and abort the rest of that poll cycle.
            _LOGGER.debug(
                "Authentication token invalid (401) for domain %s, "
                "will reconnect on next request",
                self.domain,
            )
            self._session.invalidate()
            msg = "Authentication failed (HTTP 401 Unauthorized). Token may be expired."
            raise AuthExpiredError(msg)

        if response.status_code == HTTPStatus.FORBIDDEN:
            # Deliberately NOT reclassified as AuthInvalidError by card S-08: a 403
            # here is ambiguous (could mean the token's domain access changed,
            # not necessarily "credentials no longer valid"), and the card's
            # own coordinator step only names AuthInvalidError/AuthExpiredError coming
            # from api/auth.py's Cognito handshake, not from this branch. The
            # pre-existing over-broad "everything auth-adjacent is APIAuthError"
            # classification for 403 (see finding F3) is tracked separately.
            msg = "Access forbidden (HTTP 403). Check account permissions."
            raise APIAuthError(msg)

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            # Card S-11: this used to suggest raising the interval "above 60
            # seconds" while the default itself already was 60 (finding, see
            # analisi-codebase-radoff-ha-presa-in-carico.md §5.2 "ironia
            # dell'error handling"). S-03 already made it stop naming a fixed
            # number; this card goes one step further and names the interval
            # actually configured for this entry (`self.scan_interval`, set
            # from `config_entry.options` by the coordinator - see
            # `API.__init__`), so the message is never wrong for this
            # installation, and points at the option that now actually exists
            # (before S-11, RadoffOptionsFlow did not exist, so this advice
            # was inapplicable - finding F6).
            msg = (
                f"API rate limit exceeded (HTTP 429). The current polling "
                f"interval for this account is {self.scan_interval} seconds; "
                "increase it from the integration's options (Settings > "
                "Devices & Services > Radoff > Configure)."
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
