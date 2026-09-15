"""Class which represent the Radoff API."""

import logging
import random
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter, Retry

from ..const import (
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
    DEFAULT_SCAN_INTERVAL,
    RATE_LIMIT_BACKOFF_JITTER,
    RATE_LIMIT_BACKOFF_MAX,
    RATE_LIMIT_BACKOFF_START,
    USER_AGENT,
)
from ..redact import scrub, short_serial
from .auth import AuthExpiredError, CognitoSession
from .exceptions import (
    APIAuthError,
    APIConnectionError,
    APIDeviceNotFoundError,
    APIDomainAccessError,
    APIRateLimitError,
    APIServerError,
    APIUnknownDeviceTypeError,
    DomainNotFoundError,
)
from .models import (
    KNOWN_CONNECTION_STATUSES,
    ConnectionState,
    RadoffDevice,
    Reading,
    classify_connection_status,
)

_LOGGER = logging.getLogger(__name__)

# The device type this integration is tested and supported on. Documentation
# only: nothing filters on it, and a device of any other type is still read.
SUPPORTED_DEVICE_TYPES = frozenset({"nowplus"})

# API paths, the version being in the hostname. The token is accepted on
# `/data/*` and `/analytics/*` only, and a missing route answers 403, not 404.
DISCOVERY_PATH = "/data/user/me/domains"
DEVICES_PATH = "/data/devices"

# The per-device-type measurement schema. Not domain-scoped: a schema
# describes a product, not a customer's estate.
MEASURES_RANGES_PATH = "/analytics/measures-ranges"

# Deliberately large: the quota counts requests, not bytes. A silent cap
# costs one extra request, never a missing device.
DEVICES_PAGE_SIZE = 200

# Hard stop on the pagination loop: reaching 5000 devices means the backend is
# answering with a `pagination` block that never terminates.
MAX_DEVICE_PAGES = 25

# Keys of the `telemetry` block that are not measurements: the block's own
# instant, and the device type repeated inside it.
_TELEMETRY_NON_MEASURE_KEYS = frozenset({"timestamp", "device_type"})

# A numeric timestamp above this (~year 2100 expressed in seconds) is treated
# as milliseconds instead of seconds.
_EPOCH_MS_THRESHOLD = 4_102_444_800


def _safe_url(url: str) -> str:
    """Return `url` without its query string or fragment, safe to log."""
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _get_request_id(response: requests.Response) -> str | None:
    """Return a backend request id from the response headers, if present."""
    for header in ("x-amzn-requestid", "x-request-id", "x-amz-request-id"):
        value = response.headers.get(header)
        if value:
            return value
    return None


def _error_body(response: requests.Response) -> dict[str, Any]:
    """
    Return the parsed error body of a non-200 response, or `{}`.

    The error body is not uniform across the API - at least three shapes
    exist - so a missing, empty or non-JSON body must never turn a
    classifiable HTTP status into a parse error.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_detail(body: dict[str, Any]) -> str | None:
    """Return the backend's own error text from a parsed error body, if any."""
    for key in ("error", "message"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_timestamp(raw: Any) -> datetime | None:
    """
    Best-effort parse of a single raw value as a timestamp.

    Accepts an ISO-8601 string or a Unix epoch in seconds or milliseconds;
    anything unparseable returns `None` rather than failing the whole poll.
    UTC is assumed when no offset is given, because the API spells its two
    timestamps differently and only one of them carries a "Z".
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


def _build_readings(
    telemetry: dict[str, Any], serial_number: str
) -> tuple[dict[str, Reading], datetime | None]:
    """
    Turn one flat `telemetry` block into readings keyed by field name.

    Every numeric field is read, not only the ones that become entities, so a
    field the backend starts sending reaches diagnostics straight away.
    `bool` is excluded before the numeric check, being an `int` subclass.
    """
    measured_at = _parse_timestamp(telemetry.get("timestamp"))
    readings: dict[str, Reading] = {}
    skipped: list[str] = []

    for name, value in telemetry.items():
        if name in _TELEMETRY_NON_MEASURE_KEYS:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            skipped.append(name)
            continue
        readings[name] = Reading(name=name, value=value, measured_at=measured_at)

    # Metadata only: field names and counts, never a value or the raw payload.
    if skipped:
        _LOGGER.debug(
            "Device %s: ignored %d non-numeric telemetry field(s): %s",
            serial_number,
            len(skipped),
            sorted(skipped),
        )

    return readings, measured_at


class API:
    """API platform."""

    DEFAULT_TIMEOUT = (10, 30)

    def __init__(  # noqa: PLR0913
        self,
        username: str,
        password: str,
        client_id: str = DEFAULT_CLIENT_ID,
        pool_id: str = DEFAULT_POOL_ID,
        pool_region: str = DEFAULT_POOL_REGION,
        domain_prefix: str = "",
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        """
        Initialise.

        `client_id`, `pool_id` and `pool_region` default to this integration's
        own Cognito app client: internal infrastructure, not per-user secrets,
        overridable for a staging environment or in tests. `domain_prefix` is
        the tenant domain chosen for this account and is used as-is; no
        discovery happens here. `base_url` is the whole difference between
        environments, the API version being part of the hostname.
        `scan_interval` schedules nothing here - it is reported in the HTTP 429
        branch so the error can name the interval actually in effect.
        """
        self.domain_prefix: str = domain_prefix
        self.scan_interval = scan_interval
        self.base_url = base_url.rstrip("/")

        # Consecutive 429s, i.e. the exponent of the backoff. The first
        # successful response resets it.
        self._consecutive_rate_limits = 0

        # `connection_status` values already warned about, so a domain where
        # every device shares a new state warns once, not once per device.
        self._unknown_connection_statuses: set[str] = set()

        # Serials this client has built a device from, so an error body
        # naming one is scrubbed of it like any other value of ours.
        self._known_serials: set[str] = set()

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

        # 429 is absent from `status_forcelist`: the quota is shared, so
        # retrying at once makes a saturated stage worse. The 5xx entries stay.
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
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
        """Perform an initial Cognito login and return True on success."""
        self._session.get_bearer()
        return True

    def _scrub(self, text: str | None) -> str | None:
        """Return text this client did not write, with its own identifiers held back."""
        return scrub(text, self.domain_prefix, *self._known_serials) if text else text

    def _url(self, path: str) -> str:
        """Return the absolute URL of `path` on this client's environment."""
        return f"{self.base_url}{path}"

    def _domain_params(self, **extra: Any) -> dict[str, Any]:
        """
        Return the query params of a domain-scoped request, `domain_prefix` included.

        Refuses to build params without a prefix rather than omitting it:
        `GET /data/devices` without one answers 200 with every device of every
        domain the account can reach, so a missing prefix is a cross-domain
        read, not a narrower query.
        """
        if not self.domain_prefix:
            msg = (
                "Refusing to call a domain-scoped Radoff endpoint without a "
                "domain_prefix: the API would answer with every device of "
                "every domain this account can reach."
            )
            raise DomainNotFoundError(msg)

        return {"domain_prefix": self.domain_prefix, **extra}

    def list_domains(self, bearer_token: str | None = None) -> list[dict[str, Any]]:
        """
        Return every domain the authenticated user has access to, unfiltered.

        The endpoint needs nothing but the bearer token, and an account with
        no domain answers 200 with an empty list. Each entry has the shape
        `{"domain": {"prefix": ..., "name": ...}, "role": ...}` and is handed
        back as-is for the caller to read.

        Called only from the config flow, where every failure has the same
        remedy, so a non-200 stays an `APIConnectionError` instead of going
        through the full taxonomy.
        """
        if bearer_token is None:
            bearer_token = self._session.get_bearer()

        url = self._url(DISCOVERY_PATH)
        response = self.session.get(
            url,
            headers=self._get_headers(bearer_token=bearer_token),
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

    def get_devices(self) -> list[RadoffDevice]:
        """
        Return every device of this domain, telemetry included.

        One request per page, so any error it raises is the whole cycle's and
        nothing is caught here. `seen_serials` drops a device already
        collected: the ordering is stable across pages, so a repeat means the
        list shifted mid-walk and would otherwise yield two entity sets for
        one device.
        """
        devices: list[RadoffDevice] = []
        seen_serials: set[str] = set()
        page = 1

        while page <= MAX_DEVICE_PAGES:
            payload = self._get_devices_page(page)

            raw_devices = payload.get("devices")
            if not isinstance(raw_devices, list):
                _LOGGER.warning(
                    "Radoff device list page %d carried no `devices` array; "
                    "treating it as empty",
                    page,
                )
                raw_devices = []

            for raw_device in raw_devices:
                if not isinstance(raw_device, dict):
                    continue
                device = self._build_device(raw_device)
                if device is None or device.serial_number in seen_serials:
                    continue
                seen_serials.add(device.serial_number)
                devices.append(device)

            pagination = payload.get("pagination")
            total_pages = (
                pagination.get("total_pages") if isinstance(pagination, dict) else None
            )
            if not isinstance(total_pages, int) or page >= total_pages:
                break
            page += 1
        else:
            _LOGGER.warning(
                "Radoff device list stopped at the %d-page ceiling; the "
                "response's `pagination.total_pages` never terminated",
                MAX_DEVICE_PAGES,
            )

        _LOGGER.debug(
            "Radoff device list: %d device(s) over %d page(s), "
            "%d of them without telemetry this cycle",
            len(devices),
            min(page, MAX_DEVICE_PAGES),
            sum(1 for device in devices if device.stale),
        )
        return devices

    def _get_devices_page(self, page: int) -> dict[str, Any]:
        """Fetch one page of the device list, classified but not interpreted."""
        url = self._url(DEVICES_PATH)
        response = self.session.get(
            url,
            headers=self._get_headers(bearer_token=self._session.get_bearer()),
            params=self._domain_params(page=page, page_size=DEVICES_PAGE_SIZE),
            timeout=self.DEFAULT_TIMEOUT,
        )

        self._check_response_status(response=response, url=url)

        body = response.json()
        return body if isinstance(body, dict) else {}

    def get_measures_ranges(self, device_type: str | None = None) -> dict[str, Any]:
        """
        Return the raw `measures-ranges` schema for one device type.

        One call per type, made once at setup and cached: the schema
        describes a product, and the quota it would otherwise burn is shared
        with Radoff's own apps. Omitting `device_type` merges every type into
        one object, which hides the measures two types disagree about. An
        unknown type raises `APIUnknownDeviceTypeError`. The response is
        returned raw; parsing it belongs to `schema.py`.
        """
        url = self._url(MEASURES_RANGES_PATH)
        params = {} if device_type is None else {"device_type": device_type}
        response = self.session.get(
            url,
            headers=self._get_headers(bearer_token=self._session.get_bearer()),
            params=params,
            timeout=self.DEFAULT_TIMEOUT,
        )

        self._check_response_status(response=response, url=url)

        body = response.json()
        if not isinstance(body, dict):
            _LOGGER.warning(
                "Radoff measures-ranges for device type '%s' answered with "
                "%s instead of an object; treating it as an empty schema",
                device_type,
                type(body).__name__,
            )
            return {}
        return body

    def _build_device(self, raw_device: dict[str, Any]) -> RadoffDevice | None:
        """
        Turn one entry of the device list into a `RadoffDevice`, or skip it.

        `None` means one thing only: an entry with no `serial_number`, which
        leaves the device no identity at all. Every device is built whatever
        its type - `type` is a cache key for the schema, never an eligibility
        test - and a missing one keeps an empty string so `device_type` stays
        a `str` for every consumer.
        """
        self._log_nested_slave(raw_device)

        raw_type = raw_device.get("type")
        device_type = raw_type if isinstance(raw_type, str) else ""

        serial_number = raw_device.get("serial_number")
        if not serial_number:
            _LOGGER.warning(
                "Skipping a %s device with no serial_number: in arch 2.0 the "
                "serial is the only identity a device has",
                device_type,
            )
            return None

        self._known_serials.add(serial_number)
        self._check_connection_status(raw_device, serial_number)

        telemetry = raw_device.get("telemetry")
        if isinstance(telemetry, dict):
            readings, telemetry_timestamp = _build_readings(telemetry, serial_number)
            stale = False
        else:
            # `telemetry: null` is the shape of a device that is not
            # reporting. Not an error, and not offline either.
            readings, telemetry_timestamp = {}, None
            stale = True
            _LOGGER.debug("Device %s carries no telemetry this cycle", serial_number)

        return RadoffDevice(
            serial_number=serial_number,
            device_type=device_type,
            name=raw_device.get("name") or serial_number,
            readings=readings,
            connection_status=raw_device.get("connection_status"),
            connection_status_updated_at=_parse_timestamp(
                raw_device.get("connection_status_updated_at")
            ),
            status=raw_device.get("status"),
            firmware_version=raw_device.get("firmware_version"),
            room_name=raw_device.get("room_name"),
            room_slug=raw_device.get("room_slug"),
            building_name=raw_device.get("building_name"),
            building_slug=raw_device.get("building_slug"),
            domain_prefix=raw_device.get("domain_prefix"),
            telemetry_timestamp=telemetry_timestamp,
            stale=stale,
        )

    def _check_connection_status(
        self, raw_device: dict[str, Any], serial_number: str
    ) -> None:
        """
        Warn once about a `connection_status` value this client does not know.

        Warned here, where the raw string enters the model, rather than in
        `RadoffEntity.available`: that property is read on every state read of
        every entity. Deduped by value, not by device. The value itself is
        acted on elsewhere - an unrecognised one leaves the entities
        available.
        """
        raw = raw_device.get("connection_status")
        if raw is None:
            # An absent field is not a new state, and the diagnostics dump
            # already shows it.
            _LOGGER.debug(
                "Device %s carries no connection_status this cycle", serial_number
            )
            return

        if classify_connection_status(raw) is not ConnectionState.INDETERMINATE:
            return

        if raw in self._unknown_connection_statuses:
            return

        self._unknown_connection_statuses.add(raw)
        # The device type is what says which family of devices this happens
        # on; four digits of serial only tell two lines of one log apart,
        # which at one line per value is all that is needed.
        _LOGGER.warning(
            "Radoff reported connection_status '%s' on a '%s' device (serial "
            "%s), a value this version does not know (it knows %s). The "
            "device's entities are kept available rather than reported "
            "offline; if this value is here to stay it belongs in "
            "KNOWN_CONNECTION_STATUSES",
            raw,
            raw_device.get("type") or "unknown",
            short_serial(serial_number),
            ", ".join(sorted(KNOWN_CONNECTION_STATUSES)),
        )

    @staticmethod
    def _log_nested_slave(raw_device: dict[str, Any]) -> None:
        """Report a device nested under `controller_of_device`, then ignore it."""
        nested = raw_device.get("controller_of_device")
        if not isinstance(nested, dict):
            return

        # Two serials and a domain in one line, and nothing a user could act
        # on: a device this version does not model is read by whoever is
        # debugging the model, at the level they turn on to do it.
        _LOGGER.debug(
            "Device %s controls a nested %s device (%s, domain %s) that this "
            "version does not model: ignored",
            raw_device.get("serial_number") or "unknown",
            nested.get("type") or "unknown",
            nested.get("serial_number") or "unknown",
            nested.get("domain_prefix") or "unknown",
        )

    def _get_headers(self, bearer_token: str) -> dict[str, str]:
        """
        Return the headers every request carries.

        There is no domain header: the domain travels as a query param.
        `USER_AGENT` identifies this integration honestly, which is what lets
        the backend tell its load apart from the mobile app's on a quota the
        two share.
        """
        return {
            "user-agent": USER_AGENT,
            "accept-encoding": "gzip",
            "authorization": "Bearer " + bearer_token,
            "content-type": "application/json",
        }

    def _rate_limit_backoff(self) -> float:
        """
        Return the delay, in seconds, to honour after a 429 - and count it.

        Exponential, doubling per consecutive 429 and capped, minus a jitter.
        The response carries no `Retry-After`, so the delay is this client's
        to choose; the jitter keeps installations rate-limited by the same
        stage from returning in lockstep.
        """
        nominal = min(
            RATE_LIMIT_BACKOFF_START * 2**self._consecutive_rate_limits,
            RATE_LIMIT_BACKOFF_MAX,
        )
        self._consecutive_rate_limits += 1
        jitter = random.uniform(0, RATE_LIMIT_BACKOFF_JITTER)  # noqa: S311
        return nominal * (1 - jitter)

    def _check_response_status(
        self, response: requests.Response, url: str = ""
    ) -> bool:
        """
        Classify one response, raising one exception class per HTTP semantic.

        Driven by the status, with the body read only to tell the two shapes
        of 404 apart: the error body is not uniform enough to key a taxonomy
        on.
        """
        if response.status_code == HTTPStatus.OK:
            # Any success clears the 429 streak, so the next rate limit starts
            # its backoff from the floor again.
            self._consecutive_rate_limits = 0
            return True

        safe_url = _safe_url(url or response.url)
        request_id = _get_request_id(response)
        body = _error_body(response)
        # Scrubbed here, once: this text is the backend's and every branch
        # below either logs it or carries it into an exception message.
        detail = self._scrub(_error_detail(body))

        _LOGGER.warning(
            "API request failed: status=%d, url=%s, request_id=%s, detail=%s",
            response.status_code,
            safe_url,
            request_id or "n/a",
            detail or "n/a",
        )

        if response.status_code == HTTPStatus.UNAUTHORIZED:
            # A 401 says the session is gone, not that the password is wrong:
            # only the next login can tell, so it must not open re-auth.
            _LOGGER.debug(
                "Authentication token invalid (401) for domain %s, "
                "will reconnect on next request",
                self.domain_prefix,
            )
            self._session.invalidate()
            msg = "Authentication failed (HTTP 401 Unauthorized). Token may be expired."
            raise AuthExpiredError(msg)

        if response.status_code == HTTPStatus.FORBIDDEN:
            # The 403 has one cause: a `domain_prefix` this account does not
            # belong to. Never transient, so never retried.
            _LOGGER.error(
                "Radoff refused access to the configured domain (HTTP 403): "
                "%s. The account does not belong to it - this needs a "
                "reconfiguration, not a retry",
                detail or "no detail in the response body",
            )
            # The name is written once, here, and travels no further: this
            # message reaches the user through `ConfigEntryError`, which Home
            # Assistant logs and stores at a level this integration does not
            # choose.
            _LOGGER.debug("The domain refused on this 403 is '%s'", self.domain_prefix)
            msg = (
                "Access to the configured Radoff domain was refused (HTTP "
                "403). This account does not belong to that domain: "
                "reconfigure the integration to select a domain it can reach."
            )
            raise APIDomainAccessError(msg)

        if response.status_code == HTTPStatus.NOT_FOUND:
            # Two different 404s, told apart by the body rather than the path.
            # `available` appears only on the measures-ranges one.
            available = body.get("available")
            if isinstance(available, list):
                msg = (
                    f"Unknown device type (HTTP 404 on {safe_url}): "
                    f"{detail or 'no detail in the response body'}. The API "
                    f"lists these types as valid: {', '.join(available)}."
                )
                raise APIUnknownDeviceTypeError(msg, available=available)

            msg = (
                f"Device unknown or without data (HTTP 404 on {safe_url}): "
                f"{detail or 'no detail in the response body'}."
            )
            raise APIDeviceNotFoundError(msg)

        if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
            # The quota this protects is shared with Radoff's own apps, so
            # the whole cycle is skipped rather than any part of it retried.
            retry_after = self._rate_limit_backoff()
            _LOGGER.warning(
                "Radoff rate limit hit (HTTP 429); skipping this cycle and "
                "backing off %.1fs (attempt %d in this streak)",
                retry_after,
                self._consecutive_rate_limits,
            )
            msg = (
                f"API rate limit exceeded (HTTP 429); skipping this cycle and "
                f"backing off {retry_after:.1f}s. The limit is per environment "
                f"and shared with the Radoff apps. The current polling interval "
                f"for this account is {self.scan_interval} seconds; increase it "
                "from the integration's options (Settings > Devices & Services "
                "> Radoff > Configure)."
            )
            raise APIRateLimitError(msg, retry_after=retry_after)

        if response.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
            # A degraded backend dependency answers 200 with `null` fields
            # instead, so a 5xx really is the API itself being unavailable.
            msg = (
                f"Radoff API server error (HTTP {response.status_code}). "
                "This is usually temporary - will retry automatically."
            )
            raise APIServerError(msg)

        msg = f"API request failed with HTTP {response.status_code}."
        raise APIAuthError(msg)
