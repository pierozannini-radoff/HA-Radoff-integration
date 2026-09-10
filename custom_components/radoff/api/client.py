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
from .models import RadoffDevice, Reading

_LOGGER = logging.getLogger(__name__)

# The arch 2.0 device type this integration is tested and supported on. The
# value is the payload's own `type` field, lowercase and unversioned
# (`nowplus`, `sense`, `city`, `life`, `now`, `sismoff` - the catalogue
# `/analytics/measures-ranges` enumerates, see
# `tests/fixtures/dev/_findings.md`), where arch 1.x sent a display name
# (`deviceTypeName: "Now+"`).
#
# Card M-04 changes what this constant *does*, and the change is the point.
# It used to be a filter: `_build_device` returned `None` for anything not
# in it, so a `sense` in the user's domain simply did not exist as far as
# Home Assistant was concerned. It is now documentation - the type this
# release promises (README) and the one the live verification runs against -
# and nothing filters on it.
#
# The reason is T-02's decision, carried by M-04: `type` is a cache key for
# the schema, not an eligibility test. Before this card the code had no way
# to describe another type's telemetry, so dropping the device was at least
# honest; now `/analytics/measures-ranges?device_type=<type>` describes any
# type the catalogue holds, and a type it does not hold still leaves the
# device's telemetry readable. Discarding a device the API returned would be
# throwing away data we can render, and it is exactly what M-04's acceptance
# criterion "an unknown device_type does not make the device disappear"
# forbids.
SUPPORTED_DEVICE_TYPES = frozenset({"nowplus"})

# Paths of the arch 2.0 API, relative to the base URL (card M-02). The
# version is in the hostname, not here (T-02 D-01: no `/v2/...`), and the
# leading segment is the API Gateway base path mapping: `/data/*`,
# `/analytics/*`, `/auth/*`, `/admin/*` all live on the same host.
#
# The discovery path is `/data/...`, NOT `/auth/user/me/domains` as T-02
# D-03 stated: M-01 probed both and only the `data` one exists on arch 2.0
# (`scripts/probe_arch2.py`, `tests/fixtures/dev/_findings.md` D-24-surface:
# the token is accepted on `/data/*` and `/analytics/*` only). Trusting the
# answer over the probe here would have produced a 403 that looks like an
# authorization problem, because API Gateway answers a non-existent route
# that carries an `Authorization` header by trying to read it as a SigV4
# signature - see that script's own comment.
DISCOVERY_PATH = "/data/user/me/domains"
DEVICES_PATH = "/data/devices"

# The per-device-type measurement schema (card M-04). Under `/analytics/*`,
# the second base path mapping this token is accepted on, and - unlike
# `DEVICES_PATH` - **not** domain-scoped: M-01 called it with `device_type`
# as its only parameter (`tests/fixtures/dev/_manifest.json`) and a schema
# describes a product, not a customer's estate.
MEASURES_RANGES_PATH = "/analytics/measures-ranges"

# One call per cycle carries the whole domain's devices *and* their
# telemetry, so `page_size` is deliberately large: the cost of a page is a
# round trip, and the quota being protected (T-02 D-21/D-22) is a count of
# requests, not of bytes. 200 is the value M-01 actually exercised against
# dev (`tests/fixtures/dev/devices__full.json`).
#
# Whether the backend honours it in full is not something this client needs
# to know: `/data/user/me/domains` is documented as capping `page_size` at
# 100 (M-01, D-03), the devices endpoint echoed 200 back unchanged, and the
# loop in `get_devices` follows `pagination.total_pages` either way. A cap
# applied silently costs one extra request, not a missing device.
DEVICES_PAGE_SIZE = 200

# Hard stop on the pagination loop: 25 pages of 200 is 5000 devices, far
# past any plausible domain (the largest M-01 censused held 83), so reaching
# it means the backend is answering with a `pagination` block that never
# terminates rather than that someone has that many devices. Bounded here so
# a malformed response costs one logged cycle instead of an endless loop
# inside the executor thread.
MAX_DEVICE_PAGES = 25

# Keys of the `telemetry` block that are not measurements. `timestamp` is
# the block's own instant (see `Reading.measured_at`) and `device_type`
# repeats the device's type inside its telemetry. Everything else in there
# is a field with a value, and is read as one - by name, never by position
# (M-03 AC).
_TELEMETRY_NON_MEASURE_KEYS = frozenset({"timestamp", "device_type"})

# A numeric timestamp above this (~year 2100 expressed in seconds) is treated
# as milliseconds instead of seconds.
_EPOCH_MS_THRESHOLD = 4_102_444_800


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

    `x-amzn-requestid` first because that is the one arch 2.0 actually
    sends: M-01 (T-02 D-30) found it on *every* response captured on dev,
    success and error alike, alongside `x-amz-apigw-id` and
    `x-amzn-trace-id` - see `tests/fixtures/dev/_manifest.json`, which
    records the full response headers of each call. The other two names are
    kept as fallbacks, unchanged: they cost one dict lookup each and cover
    an application-level id should the backend ever add one.
    """
    for header in ("x-amzn-requestid", "x-request-id", "x-amz-request-id"):
        value = response.headers.get(header)
        if value:
            return value
    return None


def _error_body(response: requests.Response) -> dict[str, Any]:
    """
    Return the parsed error body of a non-200 response, or `{}`.

    The arch 2.0 error body is deliberately treated as untrusted shape: T-02
    D-29 is still open on the complete form of `ErrorResponse`, and M-01
    alone captured three different shapes - `{"message": ...}` (401, and the
    429 API Gateway generates), `{"error": ...}` (403, 404 on a device) and
    `{"error": ..., "message": ..., "available": [...]}` (404 on an unknown
    device type). A body that is missing, empty or not even JSON must never
    turn a classifiable HTTP status into a parse error.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_detail(body: dict[str, Any]) -> str | None:
    """
    Return the backend's own error text from a parsed error body, if any.

    Reads both keys arch 2.0 uses (`error` for application errors, `message`
    for the ones API Gateway itself produces) so the log line carries what
    the backend said instead of only the status code.
    """
    for key in ("error", "message"):
        value = body.get(key)
        if isinstance(value, str) and value:
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

    Assumes UTC when a parsed ISO-8601 string carries no explicit offset -
    and in arch 2.0 that branch is load-bearing, where in 1.x it was only
    defensive. The two timestamps this client reads are spelled
    differently in the real payloads M-01 captured: `telemetry.timestamp`
    is `"2026-09-10T09:21:25.023Z"` (explicit "Z", already UTC), while
    `connection_status_updated_at` is `"2026-09-10T10:06:41"` - same
    backend, no offset at all. Reading the second as anything but UTC
    would silently shift it by the local timezone, so the assumption is
    made here, once, rather than at each call site.
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

    Arch 2.0 replaced the three response buckets of 1.x with this single
    object, holding the last value of each field, so the reading key is the
    field name again (card M-03, see `api/models.py`).

    Every numeric field is read, not only the ones this integration can
    currently label. The provisional field table lives in `sensor.py` and
    decides which readings become *entities*; keeping the model itself
    exhaustive means M-04, which replaces that table with the schema the
    API serves, changes one file and not this one - and a field the backend
    starts sending (radon, on a device that has it) reaches diagnostics
    immediately instead of being invisible until someone adds it here.

    `bool` is excluded before the numeric check because it is an `int`
    subclass: a `true` would otherwise become a reading with value 1.
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

    # Metadata only (S-03 logging policy): field names and counts, never a
    # value and never the raw payload.
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
        own Cognito app client (see const.py) and are no longer expected to be
        supplied by the config flow or persisted in the config entry (S-02):
        they are internal production infrastructure, not per-user secrets.
        Callers may still override them explicitly (e.g. for a staging
        environment or in tests), but there is no user-facing UI for that.

        `domain_prefix` is the tenant domain already chosen for this account
        (persisted in the config entry after the config flow's
        discovery/selection step). It is used as-is for every subsequent
        call; no domain discovery happens here.

        Card M-02 renames it from `domain_id`: in arch 2.0 the domain is no
        longer a UUID sent in the domain header of arch 1.x (that header does
        not exist any more) but a human-readable prefix sent as the
        `domain_prefix` query parameter, always explicitly. Only this
        client's parameter and attribute are renamed - the config entry key
        stays `domain_id` (`CONF_DOMAIN_ID`, const.py), so no entry
        migration is needed; rewriting what the config flow discovers and
        persists belongs to the config-flow card of this migration.

        `base_url` is the environment to talk to, defaulting to
        `DEFAULT_BASE_URL` (const.py - dev, for the duration of the
        migration) and overridable per config entry from the options flow's
        advanced field. In arch 2.0 the API version is part of the hostname,
        so this one value is the whole difference between environments; a
        trailing slash is stripped so `_url()` can join paths blindly.

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
        self.domain_prefix: str = domain_prefix
        self.scan_interval = scan_interval
        self.base_url = base_url.rstrip("/")

        # Number of consecutive 429s seen on this client, i.e. the exponent
        # of the backoff `_rate_limit_backoff` hands to `APIRateLimitError`.
        # Reset by the first successful response (see
        # `_check_response_status`), so an isolated rate limit costs the
        # ~5s floor and never a delay inherited from an older incident.
        self._consecutive_rate_limits = 0

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
        # Card M-02: 429 is gone from `status_forcelist`. urllib3 retried it
        # here after a 0.5s backoff, which is precisely the "retry now" the
        # backend asked us not to do (T-02 D-22): the quota is per
        # environment and shared with the Radoff apps, so an immediate retry
        # spends more of an already-exceeded budget and makes the saturation
        # worse. A 429 now surfaces at once as `APIRateLimitError` carrying
        # the delay to honour, and the cycle is skipped instead. The 5xx
        # entries stay: those are transient failures where retrying is the
        # right reflex.
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

    def _url(self, path: str) -> str:
        """
        Return the absolute URL of `path` on this client's environment.

        The single place a request URL is built (card M-02 AC: "il base URL è
        un solo valore in const.py; nessun host è ripetuto altrove"). `path`
        always starts with its API Gateway base path (`/data/...`,
        `/analytics/...`) and never with a version segment: in arch 2.0 the
        version is in `self.base_url`'s hostname.
        """
        return f"{self.base_url}{path}"

    def _domain_params(self, **extra: Any) -> dict[str, Any]:
        """
        Return the query params of a domain-scoped request, `domain_prefix` included.

        Card M-02. `domain_prefix` replaces the domain header of arch 1.x and
        is passed **always, explicitly**, as the backend asked (T-02
        D-03/D-04) - which is also why this method refuses to build params
        without one instead of quietly omitting it. M-01 measured what
        omitting it does: `GET /data/devices` answers 200 with every device
        of every domain the account can reach (120 of them on the dev
        account, across several domains - see
        `tests/fixtures/dev/error__devices_no_domain_prefix.json`). A missing
        prefix is therefore not a narrower query, it is a cross-domain read,
        and it must fail loudly.

        `coordinator.py` already refuses to build a client for an entry with
        no domain at all (`ConfigEntryError` + a Repairs issue, RT-2926), so
        this is the defensive floor under that check, not the user-facing
        one.
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

        No `parentDomainId` filtering is applied here anymore (see S-01): the
        caller (config flow) decides what to do with 1, more than 1, or 0 domains.

        Card M-02 removed the chicken-and-egg dance this method used to
        perform. In arch 1.x the discovery endpoint itself required the
        domain header (401 "Missing Domain Header" without one), so the
        client decoded the `d_<domain-uuid>` claims of its own Cognito
        IdToken just to have a domain id to bootstrap the call with. In arch
        2.0 the header does not exist and the endpoint needs nothing but the
        bearer token - verified on dev by M-01 (T-02 D-03: "chiamabile con il
        solo bearer token") - so both the claim decoding and its
        `_extract_domain_claims` helper are gone.

        One consequence worth stating: an account with no accessible domain
        is now discovered from the response (200 with an empty `domains`
        list) instead of from the absence of claims in the token, which is
        the authoritative answer rather than an inference about it.

        Only ever called from the config flow (initial setup or re-auth
        validation) - `RadoffCoordinator`'s periodic polling never calls this
        (card S-12 AC: "Nessuna chiamata a /auth/user/me/domains dopo il
        primo setup" - the path that AC named in arch 1.x, `DISCOVERY_PATH`
        here): the domain it needs is the one already persisted on
        the config entry (see S-01), and `get_devices()` below never
        re-discovers it.

        The response *shape* of arch 2.0 differs from 1.x - each entry is now
        `{"domain": {"prefix": ..., "name": ...}, "role": ...}` rather than a
        flat domain object (M-01, V6) - and the top-level `domains` key is
        returned as-is here for the caller to read. Teaching the config flow
        to read the new shape (and to persist `prefix` rather than a UUID) is
        that card's job, not this one's: this card migrates the transport.

        A non-200 stays an `APIConnectionError` rather than going through
        `_check_response_status`'s taxonomy: this call happens inside the
        config flow, where every failure has the same single remedy
        ("cannot_connect", see config_flow.py) - there is no session to
        invalidate, no cycle to skip and no domain to reconfigure yet.
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

        "Every" is literal since card M-04: the `type` filter that used to
        drop anything but a Now+ is gone (see `_build_device`).

        Card M-03 replaces the 1 + N call pattern of arch 1.x - a
        `POST /data/devices/search` followed by one
        `GET /data/devices/{id}` per device - with the single paginated
        `GET /data/devices?domain_prefix=...&page_size=200` that carries
        each device's `telemetry` block inline. M-02 deliberately kept the
        old endpoints while migrating the transport; this is where they go.

        Bringing that call forward from M-05 (its step 1) was a decision
        about ordering, not scope: this card removes `DeviceFetchError` and
        `RadoffCoordinator._merge_device_errors` because "with one call per
        cycle there is no isolatable per-device error", and that premise is
        only true once the call is actually one. Doing the two separately
        would have left two cards' worth of releases where a single device
        answering 5xx took the whole poll down with the per-device isolation
        already deleted. What stays with M-05 is the rest of its title:
        polling intervals, jitter and the 429 policy.

        Failure semantics follow from that and are deliberately simpler than
        S-13's: there is one request per page, so any error it raises is the
        cycle's. Nothing is caught here - the taxonomy `_check_response_status`
        raises (M-02) and the session errors `get_bearer()` raises reach
        `coordinator.py` unchanged, which already has one clause per
        outcome.

        Two guards on the pagination loop, neither of them hypothetical:
        `MAX_DEVICE_PAGES` bounds a `total_pages` that never terminates, and
        `seen_serials` drops a device already collected from an earlier
        page. M-01 measured the ordering as stable across pages (V1), so a
        repeat means the list shifted mid-walk - a device added or removed
        between two requests - and the cost of not noticing would be two
        entity sets for one device.
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
        """
        Fetch one page of `GET /data/devices`, classified but not interpreted.

        Kept separate from `get_devices` so the loop above reads as the
        pagination it is. The URL comes from `_url()` and the parameters
        from `_domain_params()` (card M-02): a new endpoint added with an
        f-string would bypass both the single-host guarantee and the guard
        that refuses a domain-scoped call without a `domain_prefix`, which
        M-01 measured to be a cross-domain read rather than a broader query.
        """
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
        Return the raw `measures-ranges` schema for one device type (card M-04).

        One call per *type*, made once at setup and cached
        (`coordinator.py::async_load_schemas`) - not one per device, and not
        one per poll: the schema describes a product and changes at the pace
        firmware does, while the request quota it would otherwise burn is
        shared with the Radoff apps (T-02 D-21/D-22).

        With `device_type` omitted the endpoint answers with every type's
        measures merged into one object, which is useful for a census (M-01
        captured it as `measures_ranges__all.json`) and is *not* what setup
        asks for: merged, two types that disagree about a measure cannot be
        told apart.

        An unknown type answers 404 with the valid types in `available`, and
        `_check_response_status` already turns exactly that body into
        `APIUnknownDeviceTypeError` (card M-02). It is raised, not swallowed
        here: whether an unrecognised type is fatal is a decision about
        entities, not about transport, and it belongs to the caller - which
        treats it as "type not recognised", logs it and keeps the device
        (M-04 AC).

        The response is returned raw. Parsing it into `MeasureSpec` objects
        is `schema.py`'s job, deliberately kept out of the API package: this
        module knows HTTP, that one knows Home Assistant's units and device
        classes, and mixing the two is how the deleted `properties.py` came
        to exist in the first place.
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

        `None` now means one thing only: an entry with no `serial_number`,
        which in arch 2.0 has no identity at all - `deviceId`,
        `serial_number` and `deviceSerial` are the same value (T-02 D-02),
        so there is no second field to fall back to.

        Card M-04 removed the other reason, the `type` filter. Every device
        the API returns is built, whatever its type: the schema that makes
        another type's telemetry meaningful is now fetched per type
        (`get_measures_ranges`), and a type the catalogue does not know
        still leaves a device with readings worth showing. `type` is a cache
        key for that schema, never an eligibility test (T-02, see
        `SUPPORTED_DEVICE_TYPES`) - which is also M-04's acceptance
        criterion: an unknown `device_type` must not make the device
        disappear. What stays narrow is the *promise*: Now+ is the type this
        release supports and verifies (README).

        A device whose entry carries no `type` at all keeps an empty string
        rather than `None`, so that `RadoffDevice.device_type` stays a `str`
        for every consumer (`device_info.model`, diagnostics, the schema
        cache key) and only the schema lookup misses.

        The nested-slave check runs first, as it did when a filter followed
        it. A LIFE never appears as a top-level entry of its own: it arrives
        inline, under the `controller_of_device` of its controller, and
        flattening it would create entities for a device that may belong to
        a different domain (M-01, D-33).
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

        telemetry = raw_device.get("telemetry")
        if isinstance(telemetry, dict):
            readings, telemetry_timestamp = _build_readings(telemetry, serial_number)
            stale = False
        else:
            # `telemetry: null` - the real shape of a device that is not
            # reporting (M-01, D-16: the majority of the 120 devices it
            # censused). Not an error, and not "offline" either: see
            # `RadoffDevice.stale`.
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
            firmware_version=raw_device.get("firmware_version"),
            room_name=raw_device.get("room_name"),
            room_slug=raw_device.get("room_slug"),
            building_name=raw_device.get("building_name"),
            building_slug=raw_device.get("building_slug"),
            domain_prefix=raw_device.get("domain_prefix"),
            telemetry_timestamp=telemetry_timestamp,
            stale=stale,
        )

    @staticmethod
    def _log_nested_slave(raw_device: dict[str, Any]) -> None:
        """
        Report a device nested under `controller_of_device`, then ignore it.

        M-01 confirmed the nesting is real (D-33): a `city` carries the
        entire object of the `life` it controls inline, with
        `managed_by_device_serial` pointing back at the controller - and
        that nested device can belong to a *different domain* than the
        parent, so a client that flattened the list would silently create
        entities for a domain it never asked about.

        Modelling the controller/slave pair is separate work, tracked as
        T-08 D-33. What this card refuses is the silent part: an INFO line
        so a nested device shows up in the log of anyone who has one,
        instead of being dropped without trace.
        """
        nested = raw_device.get("controller_of_device")
        if not isinstance(nested, dict):
            return

        _LOGGER.info(
            "Device %s controls a nested %s device (%s, domain %s) that this "
            "version does not model: ignored. Modelling the controller/slave "
            "pair is tracked as T-08 D-33",
            raw_device.get("serial_number") or "unknown",
            nested.get("type") or "unknown",
            nested.get("serial_number") or "unknown",
            nested.get("domain_prefix") or "unknown",
        )

    def _get_headers(self, bearer_token: str) -> dict[str, str]:
        """
        Return the headers every arch 2.0 request carries.

        Card M-02 removed the arch 1.x domain header: it does not exist in
        arch 2.0 (T-02 #10) and the domain travels as the `domain_prefix`
        query param instead (see `_domain_params`).

        `USER_AGENT` is kept exactly as S-03 defined it: there is no WAF in
        arch 2.0 (T-02 D-21/D-22), so identifying our own traffic honestly
        carries no risk of being blocked, and being identifiable is what
        lets the backend tell this integration's load apart from the mobile
        app's on a quota the two share.

        `bearer` is the accepted `Authorization` format, confirmed against
        the real host by M-01 (`auth_header_format_accettato: "bearer"`).
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

        Exponential from `RATE_LIMIT_BACKOFF_START` (~5s, as the backend
        asked in T-02 D-22), doubling per consecutive 429, capped at
        `RATE_LIMIT_BACKOFF_MAX`, minus a jitter of up to
        `RATE_LIMIT_BACKOFF_JITTER` of the nominal value. There is no
        `Retry-After` to read: API Gateway does not send one, so this delay
        is ours to choose and ours alone to honour.

        The jitter is the point of using randomness here, and it is why
        `random` rather than `secrets` is right: thousands of installations
        rate-limited by the same saturated stage must not come back in
        lockstep and re-create the peak they were told to back away from.
        Nothing here is a secret or a token.
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
        Classify one response, raising the arch 2.0 error taxonomy (card M-02).

        One exception class per semantic the caller can actually react to
        differently, instead of the single `APIAuthError` every non-200 used
        to collapse into (finding F3). The classification is driven by the
        HTTP **status**, with the body read only to tell the two shapes of
        404 apart: T-02 D-29 is still open on the complete form of
        `ErrorResponse`, and M-01 captured three different body shapes
        already, so a taxonomy keyed on body contents would be built on
        sand.
        """
        if response.status_code == HTTPStatus.OK:
            # Any success clears the 429 streak: the next rate limit, if it
            # comes, starts its backoff from the ~5s floor again rather than
            # inheriting an exponent from an incident already over.
            self._consecutive_rate_limits = 0
            return True

        safe_url = _safe_url(url or response.url)
        request_id = _get_request_id(response)
        body = _error_body(response)
        detail = _error_detail(body)

        _LOGGER.warning(
            "API request failed: status=%d, url=%s, request_id=%s, detail=%s",
            response.status_code,
            safe_url,
            request_id or "n/a",
            detail or "n/a",
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
            #
            # Card M-02 deliberately keeps this behaviour, against the card's
            # own text ("401 -> APIAuthError, che il coordinator traduce in
            # ConfigEntryAuthFailed"): decided with Piero, because routing a
            # plain expired token straight to the re-auth prompt is exactly
            # the defect S-08 fixed. What did change in arch 2.0 is what a
            # 401 now *means*: the "you do not belong to this domain" case,
            # which 1.x also reported as a 401, is a 403 here (see below), so
            # this branch is now only ever about the session. The body is
            # `{"message": "Unauthorized"}` and carries an
            # `x-amzn-errortype` header, i.e. it comes from the API Gateway
            # authorizer, not from the application (M-01, D-29).
            _LOGGER.debug(
                "Authentication token invalid (401) for domain %s, "
                "will reconnect on next request",
                self.domain_prefix,
            )
            self._session.invalidate()
            msg = "Authentication failed (HTTP 401 Unauthorized). Token may be expired."
            raise AuthExpiredError(msg)

        if response.status_code == HTTPStatus.FORBIDDEN:
            # Card M-02, and a real change of behaviour from arch 1.x, where
            # this same situation arrived as a 401 and was indistinguishable
            # from an expired session (which is why the old comment here
            # called the 403 "ambiguous" and left it as a generic
            # APIAuthError - finding F3). In arch 2.0 the 403 has exactly one
            # cause (T-02 D-29, reproduced by M-01 against dev): the request
            # carried a `domain_prefix` this account does not belong to.
            #
            # Nothing about that is transient: the token is fine, a reconnect
            # changes nothing, and retrying re-sends a request that will be
            # refused identically. The remedy is a reconfiguration, so this
            # is raised as its own class, logged at ERROR naming the domain
            # (the 401 above logs at DEBUG and names no domain: the two are
            # told apart in the log by more than their status code), never
            # retried by `Retry` (403 is not in `status_forcelist`) and never
            # isolated per device (`_NON_ISOLATABLE_API_ERRORS`).
            _LOGGER.error(
                "Radoff refused access to domain '%s' (HTTP 403): %s. The "
                "account does not belong to this domain - this needs a "
                "reconfiguration, not a retry",
                self.domain_prefix,
                detail or "no detail in the response body",
            )
            msg = (
                f"Access to Radoff domain '{self.domain_prefix}' was refused "
                f"(HTTP 403). This account does not belong to that domain: "
                f"reconfigure the integration to select a domain it can reach."
            )
            raise APIDomainAccessError(msg)

        if response.status_code == HTTPStatus.NOT_FOUND:
            # Two different 404s, told apart by the body rather than by the
            # path, because the body is what actually distinguishes them and
            # M-01 captured both (see `_error_body`). `available` is only
            # ever present on the `/analytics/measures-ranges` one, where it
            # lists the device types the backend's catalogue holds.
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
            # Card M-02 / T-02 D-21-D-22: API Gateway generates this one, not
            # the application - body `{"message": "Too Many Requests"}`, no
            # `Retry-After` - and the quota it protects is per environment,
            # shared with the Radoff mobile and web apps. So the delay is
            # computed here (`_rate_limit_backoff`) and the whole cycle is
            # meant to be skipped: `urllib3` no longer retries a 429 at once
            # (see `_create_session`) and `get_devices()` no longer isolates
            # it per device, which together were spending several requests
            # against a budget already exceeded.
            #
            # Card S-11's advice is kept and still names the interval
            # actually configured for this entry rather than a fixed number
            # that may not match it.
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
            # Transient by definition, and already retried twice by `Retry`
            # before reaching this point. Note what does NOT arrive here: a
            # degraded soft dependency (DynamoDB, Aurora) answers 200 with
            # `null` fields rather than 5xx (T-02 D-29), so a 5xx really is
            # the API itself being unavailable.
            msg = (
                f"Radoff API server error (HTTP {response.status_code}). "
                "This is usually temporary - will retry automatically."
            )
            raise APIServerError(msg)

        msg = f"API request failed with HTTP {response.status_code}."
        raise APIAuthError(msg)
