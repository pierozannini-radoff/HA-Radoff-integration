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
from ..properties import MAPPING
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
from .models import DeviceFetchError, RadoffDevice, Reading, ReadingKey

_LOGGER = logging.getLogger(__name__)

DEVICE_TYPES = ["Now+"]

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
DEVICES_SEARCH_PATH = "/data/devices/search"
DEVICE_DETAIL_PATH = "/data/devices/{device_id}"

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

# The two members of the arch 2.0 taxonomy that must NOT be isolated per
# device even though they are `APIAuthError` subclasses (card M-02):
#
# - `APIDomainAccessError` (403): the account does not belong to the domain
#   being queried, so every device of that domain fails identically.
#   Isolating it would turn a "reconfigure the integration" situation into
#   N stale devices and hide the only actionable error there is.
# - `APIRateLimitError` (429): the quota is per environment, not per device.
#   The instruction is to skip the whole cycle and back off, so continuing
#   the loop would spend the remaining devices' requests against a quota
#   that has already been exceeded - the retry pattern this card removes.
#
# Everything else in the taxonomy stays isolatable, exactly as S-13 left it:
# a 5xx (`APIServerError`) or a 404 (`APIDeviceNotFoundError`) really is
# about that one device or that one request.
_NON_ISOLATABLE_API_ERRORS = (APIDomainAccessError, APIRateLimitError)


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

        Card M-02 narrows that isolation: a 403 (`APIDomainAccessError`) and
        a 429 (`APIRateLimitError`) are re-raised instead of being recorded
        per device, since neither is about the device - see
        `_NON_ISOLATABLE_API_ERRORS`. It also keeps the endpoints of arch
        1.x (`search` plus one GET per device) while moving them onto the
        arch 2.0 host and base path: replacing them with the single
        `GET /data/devices?domain_prefix=...&page_size=200` call that carries
        telemetry inline is the next card of this migration, deliberately
        left out of this one.
        """
        devices_ok: list[RadoffDevice] = []
        device_errors: list[DeviceFetchError] = []

        url = self._url(DEVICES_SEARCH_PATH)
        post_obj = {"filter": {}, "take": 99}

        response = self.session.post(
            url,
            headers=self._get_headers(bearer_token=self._session.get_bearer()),
            params=self._domain_params(),
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
            except _NON_ISOLATABLE_API_ERRORS:
                # Card M-02: a 403 or a 429 is never about this one device -
                # see `_NON_ISOLATABLE_API_ERRORS` for why isolating either
                # would be wrong. Re-raised before the isolating clause
                # below can catch it as the `APIAuthError` subclass it is.
                raise
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

        url = self._url(DEVICE_DETAIL_PATH.format(device_id=device_id))
        response = self.session.get(
            url,
            headers=self._get_headers(bearer_token=self._session.get_bearer()),
            params=self._domain_params(),
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
