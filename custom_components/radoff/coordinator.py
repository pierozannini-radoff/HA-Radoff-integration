"""Class which represent the Radoff Coordinator."""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    API,
    APIAuthError,
    APIDomainAccessError,
    APIRateLimitError,
    APIUnknownDeviceTypeError,
    AuthChallengeRequiredError,
    AuthExpiredError,
    AuthInvalidError,
    AuthUnavailableError,
    ConnectionState,
    RadoffDevice,
)
from .const import (
    CONF_BASE_URL,
    CONF_DOMAIN_ID,
    CONF_INDEX,
    CONNECTION_STATUS_STALE_WINDOW,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ERROR_DOMAIN_ACCESS_DENIED,
    ISSUE_MISSING_DOMAIN_ID,
    POLL_JITTER_FRACTION,
    UPDATE_TIMEOUT_FACTOR,
)
from .schema import MeasureSpec, build_specs

_LOGGER = logging.getLogger(__name__)


def _stable_fraction(seed: str) -> float:
    """
    Return a number in [0, 1) that is always the same for the same `seed`.

    Card M-05. Deliberately not `random.random()` and deliberately not the
    `hash()` builtin: the first gives a different answer every restart, the
    second a different answer every *process* (PYTHONHASHSEED is randomised
    per interpreter), and this value has to survive both - an installation
    that redraws its offset on every Home Assistant restart is back in
    lockstep with everyone else who just restarted, which is precisely the
    correlated event the offset exists to break up.

    SHA-256 over the entry_id, first four bytes as an integer, scaled to
    [0, 1). No cryptographic claim is being made here; what is needed is a
    well-spread, stable mapping from an opaque id to a number, and hashlib
    is the one hash in the stdlib that is guaranteed not to be re-seeded
    behind our back.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


@dataclass
class RadoffData:
    """Class to hold api data."""

    controller_name: str
    generate_index: bool
    devices: list[RadoffDevice]


class RadoffCoordinator(DataUpdateCoordinator[RadoffData]):
    """The implementation of the Radoff coordinator."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]

        # Card RT-2926 / finding T-06/F1: this was
        # `config_entry.data[CONF_DOMAIN_ID]`, and every entry created by
        # the released version reaches it without that key - a bare
        # `KeyError` that Home Assistant, unlike `ConfigEntryNotReady`,
        # never retries and cannot report as anything but an unexpected
        # crash. `__init__.py::async_setup_entry` now stops before ever
        # constructing this coordinator, with the same translated error and
        # a Repairs issue attached; the check is repeated here so that no
        # other caller (a test, a future service, a reload path that skips
        # the setup guard) can resurrect the `KeyError`.
        domain_id = config_entry.data.get(CONF_DOMAIN_ID)
        if not domain_id:
            msg = f"Config entry {config_entry.entry_id} has no {CONF_DOMAIN_ID}"
            raise ConfigEntryError(
                msg,
                translation_domain=DOMAIN,
                translation_key=ISSUE_MISSING_DOMAIN_ID,
            )
        self.domain_id = domain_id

        # generate_index and scan_interval both live in options, not data,
        # since config entry VERSION 2 (S-02): they are user preferences, not
        # connection data. Both are now actually settable from the UI via
        # RadoffOptionsFlow (card S-11, config_flow.py) - before that card,
        # CONF_SCAN_INTERVAL was read here but nothing could ever write it
        # (finding F6): every entry silently ran at DEFAULT_SCAN_INTERVAL.
        self.generate_index = config_entry.options.get(CONF_INDEX, True)

        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        # Card M-02: which environment this entry talks to. An advanced
        # option, absent from `options` for every normal installation, in
        # which case the client falls back to `DEFAULT_BASE_URL` (const.py) -
        # dev, for the duration of the migration. Read here rather than in
        # `API.__init__` so that changing it takes effect on the reload the
        # options flow already triggers (see `_async_update_listener`,
        # __init__.py), like the two options above.
        self.base_url = config_entry.options.get(CONF_BASE_URL, DEFAULT_BASE_URL)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{HOMEASSISTANT_DOMAIN} ({config_entry.unique_id})",
            update_interval=timedelta(seconds=self.poll_interval),
        )

        # Card M-05, the two pieces of scheduling state this coordinator
        # owns. Both are consumed by `_schedule_refresh` below, which is the
        # single place that decides when the next cycle starts.
        #
        # `_jitter_fraction` is this entry's own share of
        # `POLL_JITTER_FRACTION` (const.py), drawn once from the entry_id so
        # it is the same after every restart and different from the next
        # installation's. Note Home Assistant already staggers coordinators
        # by a random *microsecond* (`self._microsecond`, see
        # DataUpdateCoordinator.__init__): that avoids a thundering herd
        # inside one event loop, it does nothing about thousands of separate
        # installations arriving at the API on the same round minute.
        #
        # `_rate_limit_delay` is a one-shot: set by the 429 clause of
        # `_async_update_data`, spent by the next `_schedule_refresh`.
        self._jitter_fraction = (
            _stable_fraction(config_entry.entry_id) * POLL_JITTER_FRACTION
        )
        self._rate_limit_delay: float | None = None

        # Serials already reported for a `connection_status_updated_at`
        # older than `CONNECTION_STATUS_STALE_WINDOW` (card M-06). Cleared
        # per device the moment its timestamp comes back inside the window,
        # so the WARNING marks the start of an episode rather than repeating
        # every five minutes for as long as it lasts.
        self._connection_status_stale_warned: set[str] = set()

        # client_id/pool_id/pool_region are no longer read from the config entry
        # (see S-02): API() falls back to this integration's own Cognito app
        # client constants (const.py) unless explicitly overridden.
        #
        # scan_interval is passed through (card S-11) purely so the API's
        # HTTP 429 handling (`api/client.py::_check_response_status`) can
        # name the interval actually in effect for this entry instead of a
        # value that may not match what the user configured.
        self.api = API(
            username=self.username,
            password=self.password,
            domain_prefix=self.domain_id,
            scan_interval=self.poll_interval,
            base_url=self.base_url,
        )

        # The measurement schema, keyed by device type (card M-04). Filled
        # once at setup by `async_load_schemas` and read by `sensor.py`
        # while it builds entities; empty for the lifetime of a coordinator
        # nobody loads it for (a test that only drives a poll cycle, say),
        # which is why every read goes through `.get(...)`.
        #
        # It lives on the coordinator rather than in a dedicated runtime
        # data object because `config_entry.runtime_data` *is* this
        # coordinator (`type RadoffConfigEntry = ConfigEntry[RadoffCoordinator]`,
        # __init__.py) - so this is the entry's runtime data, reached the
        # same way every other consumer already reaches it, with no second
        # container to keep in sync.
        #
        # It is deliberately not refreshed per poll: a schema describes a
        # product, not a reading. A device type appearing *after* setup
        # therefore finds no schema here - and creates no entity either,
        # since the sensor platform only runs at setup; both are resolved by
        # the same reload, which is how a Home Assistant integration handles
        # a device that appears while it is running.
        self.schemas: dict[str, dict[str, MeasureSpec]] = {}

    async def async_load_schemas(self) -> None:
        """
        Fetch and cache the measurement schema of every device type seen (M-04).

        Called once by `__init__.py::async_setup_entry`, after the first
        refresh (which is what makes the device types known) and before the
        sensor platform runs (which is what consumes the result). One call
        per distinct type, not per device: `/analytics/measures-ranges`
        answers about a product.

        An unknown type is not fatal, and that is an acceptance criterion of
        this card rather than a defensive reflex. The backend answers 404
        with the valid types in `available` (`APIUnknownDeviceTypeError`,
        card M-02); this logs a WARNING naming both, caches an empty schema
        so the type is not asked about again, and lets the device through -
        `sensor.py` then builds what it can from the telemetry itself.

        Every other failure propagates. A schema that cannot be fetched at
        all is not a device-shaped problem but a "the backend is not
        answering right now" one, and the caller turns it into
        `ConfigEntryNotReady`, which Home Assistant retries - strictly
        better than setting up an integration whose entities would all be
        nameless and unitless for the rest of the session.
        """
        for device_type in sorted({device.device_type for device in self.data.devices}):
            if device_type in self.schemas:
                continue

            if not device_type:
                # `type` absent from the device entry (`_build_device` keeps
                # it as ""). There is nothing to ask the schema endpoint
                # about, and asking with an empty `device_type` would get
                # the merged all-types answer, which is worse than none.
                _LOGGER.warning(
                    "A Radoff device carries no `type`: no measurement "
                    "schema can be fetched for it, so its entities are "
                    "built from its telemetry alone"
                )
                self.schemas[device_type] = {}
                continue

            try:
                payload = await self.hass.async_add_executor_job(
                    self.api.get_measures_ranges, device_type
                )
            except APIUnknownDeviceTypeError as err:
                _LOGGER.warning(
                    "Radoff does not recognise device type '%s' (the API "
                    "lists %s): its devices are kept and their entities "
                    "built from telemetry alone, without units or "
                    "qualitative bands. %s",
                    device_type,
                    ", ".join(err.available) or "no type at all",
                    err,
                )
                self.schemas[device_type] = {}
                continue

            self.schemas[device_type] = build_specs(payload, device_type=device_type)
            _LOGGER.debug(
                "Radoff schema for device type '%s': %d measure(s)",
                device_type,
                len(self.schemas[device_type]),
            )

    @property
    def poll_jitter(self) -> timedelta:
        """
        Return this entry's fixed offset between one poll and the next (M-05).

        A share of `update_interval` in [0, POLL_JITTER_FRACTION), constant
        for this config entry: an entry at the 300s default polls every
        300..330s, always the same value, and two entries created in the
        same instant do not stay in step. Derived from `update_interval`
        rather than stored in seconds so that it follows an interval changed
        from the options flow.

        Expressed as a period offset, not as a phase offset applied once at
        startup: aligning the *first* poll only would let a shared restart -
        a Home Assistant upgrade, a host reboot, an outage everyone recovers
        from together - re-align every installation that shares it.
        """
        return self.update_interval * self._jitter_fraction

    def _schedule_refresh(self) -> None:
        """
        Schedule the next cycle, honouring jitter and any pending 429 (M-05).

        Home Assistant's own implementation schedules `update_interval` from
        now, reading it through `self._update_interval_seconds`. Rather than
        reimplement it - it also handles the debouncer, `pref_disable_polling`
        and the unsubscribe bookkeeping - this swaps in the delay this cycle
        should actually use, delegates, and puts the nominal interval back.

        Restoring it matters: `update_interval` is what the cycle timeout
        budget (S-13) and this entry's jitter share are derived from, and
        neither should move because one cycle was rate limited. (Until card
        M-06 the freshness threshold of `available` hung off it too, which
        made the point sharper and was itself the problem - see
        `CONNECTION_STATUS_STALE_WINDOW`, const.py.) The only thing that
        moves is when the next refresh fires.

        A pending 429 backoff *lengthens* the wait, it never shortens it.
        The backoff starts at ~5s (RATE_LIMIT_BACKOFF_START, const.py) and
        only reaches the poll interval after a streak of them, so honouring
        it literally would have the perverse effect of polling a
        rate-limited backend every few seconds instead of every five
        minutes - the opposite of what the 429 asked for. Whichever delay is
        longer is the one that wins.
        """
        if self.update_interval is None:
            super()._schedule_refresh()
            return

        backoff = self._rate_limit_delay
        self._rate_limit_delay = None

        nominal = self.update_interval
        delay = nominal + self.poll_jitter
        if backoff is not None:
            delay = max(delay, timedelta(seconds=backoff))

        self.update_interval = delay
        try:
            super()._schedule_refresh()
        finally:
            self.update_interval = nominal

    def _skip_cycle_for_rate_limit(self, err: APIRateLimitError) -> RadoffData:
        """
        Skip a cycle the API asked us to skip, keeping the entities alive.

        Card M-02 classified the 429 and computed the backoff
        (`err.retry_after`; API Gateway sends no `Retry-After` header, so
        the delay is ours - exponential from RATE_LIMIT_BACKOFF_START,
        jittered, reset on the first 200). Card M-05 is the half M-02 left
        open: what the *scheduler* does with it.

        Two departures from how `DataUpdateCoordinator` treats a failed
        cycle, both deliberate:

        1. This returns the previous cycle's data instead of raising
           `UpdateFailed`. A 429 says "not now, you are one client among
           many on a shared quota" - it says nothing about the devices,
           which are still online and still emitting once a minute. Marking
           every entity `unavailable` because someone else's traffic peaked
           would be a lie, and one that propagates into automations and
           history.
        2. The delay is honoured as a floor: `_schedule_refresh` waits at
           least `err.retry_after` before the next cycle, so a streak of
           429s - where the backoff doubles past the poll interval - backs
           this integration off instead of polling through it. Below the
           interval the backoff changes nothing, which is the common case:
           one 429 asks for ~5s, and the next cycle was five minutes away
           anyway.

        This is a *skip*, not a suppression - and card M-06 is what makes
        the distinction hold all the way to the entity. When S-13 wrote
        this, a rate-limited stretch longer than 3x the interval still took
        the entities unavailable on its own, through the freshness check
        `available` used to run: the skip only delayed the verdict. That
        check is gone. Availability is the device's `connection_status`
        now, which a 429 does not touch, so a rate-limited stretch of any
        length leaves the entities available with the readings of the last
        successful cycle and their true `last_measured_at` - a rate limit
        on our side is never reported as a device going offline (M-06 AC:
        "un 429 o un ciclo fallito non vengono confusi con un device
        offline").

        What a 429 does *not* survive is a cycle that fails outright:
        `UpdateFailed` still clears `last_update_success` and takes every
        entity unavailable through `available`'s first condition. That is
        the honest reading of "we do not know", and it is the difference
        between skipping a cycle and losing one.
        """
        if self.data is None:
            # First refresh of the entry: there is no previous cycle to
            # return, so this is the one case where a 429 has to fail.
            # `async_config_entry_first_refresh` turns it into
            # `ConfigEntryNotReady` and Home Assistant retries the setup
            # with its own backoff - see the matching clause around
            # `async_load_schemas` in __init__.py.
            _LOGGER.warning(
                "Radoff rate limit reached on the first poll of this entry; "
                "setup will be retried: %s",
                err,
            )
            msg = f"Rate limited by the Radoff API: {err}"
            raise UpdateFailed(msg) from err

        self._rate_limit_delay = err.retry_after
        _LOGGER.warning(
            "Radoff rate limit reached; this cycle is skipped, entities keep "
            "the previous poll's readings and the next attempt waits at "
            "least ~%.1fs: %s",
            err.retry_after,
            err,
        )
        return self.data

    async def _async_update_data(self) -> RadoffData:
        """
        Fetch data from API endpoint.

        One deliberately flat `try`/`except` chain, one clause per failure
        this integration knows how to react to, each carrying the card
        history of why it reacts that way. Splitting it into helpers would
        scatter that chain without shortening it - the length is the
        exhaustiveness. (It sat over ruff's statement limit from M-02 until
        card M-03 took the merge step out of the `else` branch; the
        exemption is gone with it.)

        Card S-12: no longer checks `self.api.connected` or calls
        `self.api.connect()` before fetching - `API.get_devices()`
        authenticates lazily via `CognitoSession.get_bearer()` (api/auth.py),
        preferring a Cognito token refresh over a full SRP handshake, the
        first time it actually needs a bearer token. This coordinator never
        calls `connect()`/`disconnect()` directly any more (card S-12,
        "COSA FARE": "coordinator.py: non chiama più connect/disconnect
        direttamente").

        Card S-13 adds two things:

        1. An overall wall-clock budget for this whole cycle:
           `asyncio.timeout(update_interval * UPDATE_TIMEOUT_FACTOR)` (see
           const.py). `asyncio.timeout` (stdlib since Python 3.11, same
           `async with ...timeout(seconds):` shape the card's own wording
           - "async_timeout.timeout(...)" - describes) is used instead of
           adding the third-party `async_timeout` package as a new
           dependency: this integration's minimum supported Home Assistant
           version (`hacs.json`: 2025.1.4) already requires Python ≥3.12, so
           the stdlib primitive is available with no manifest.json/
           requirements.txt change. If the budget is exceeded, `UpdateFailed`
           is raised with an explicit message (S-13 AC: "allo scadere il log
           riporta il timeout e il ciclo successivo parte regolarmente") -
           `DataUpdateCoordinator` treats that exactly like any other failed
           cycle: entities go `unavailable` (via S-07's `available`,
           condition 1) and the next cycle is scheduled normally, it does
           not wait on this one. Note this bounds how long this coordinator
           *waits* for the cycle, not the underlying blocking HTTP call
           itself - `API.get_devices()` is still synchronous `requests` code
           run in the executor (the N+1 pattern noted as out of scope for
           this card, see its own "OUT OF SCOPE"), so a hung request keeps
           its executor thread occupied until it finishes or its own
           per-request timeout fires; only migrating to aiohttp (tracked
           separately as an "L" item) can actually cancel it.
        2. `API.get_devices()` returns the cycle's devices, or raises.

        Card M-03 removes the second half of point 2 as S-13 wrote it.
        `get_devices()` used to return `(devices_ok, device_errors)` and
        `_merge_device_errors` folded the failures back in with
        `stale=True`; both are gone, because the fetch is no longer N+1.
        There is one request per page now, so a failure is the cycle's and
        is raised - handled by the clauses below - and `stale` describes a
        device the API answered *about*, saying it has no telemetry
        (`telemetry: null`), not a device we failed to ask about.
        """
        _LOGGER.debug("Radoff _async_update_data starting")
        timeout_seconds = self.update_interval.total_seconds() * UPDATE_TIMEOUT_FACTOR

        try:
            async with asyncio.timeout(timeout_seconds):
                devices = await self.hass.async_add_executor_job(self.api.get_devices)

        except TimeoutError as err:
            _LOGGER.warning(
                "Radoff update cycle exceeded its %.1fs budget "
                "(%.0f%% of the %.1fs update_interval); aborting this cycle",
                timeout_seconds,
                UPDATE_TIMEOUT_FACTOR * 100,
                self.update_interval.total_seconds(),
            )
            msg = (
                f"Radoff update cycle timed out after {timeout_seconds:.1f}s "
                f"({UPDATE_TIMEOUT_FACTOR} x update_interval)"
            )
            raise UpdateFailed(msg) from err

        except AuthChallengeRequiredError as err:
            # Card S-09: a config entry can only exist for an account whose
            # initial setup completed with a full Cognito login (no config
            # entry is ever created for an account still on a challenge -
            # see config_flow.py's validate_input). If the periodic reconnect
            # this method performs (see `get_devices()`, via
            # `CognitoSession.get_bearer()`'s SRP fallback) ever hits a
            # challenge on an *already configured* account - e.g. Cognito
            # starts requiring MFA, or an admin resets the password on the
            # Radoff side and the account lands back on NEW_PASSWORD_REQUIRED
            # - that is functionally identical to "the stored credentials no
            # longer let this integration authenticate" (the same situation
            # `AuthInvalidError` below covers). Reusing `ConfigEntryAuthFailed`
            # here opens the exact same re-auth flow (`async_step_reauth_confirm`),
            # which calls `validate_input` again and, since it still hits the
            # same challenge, aborts cleanly with `unsupported_challenge`
            # instead of asking for a new password that would not help. This
            # closes the same class of defect C7/S-08 fixed for AuthInvalidError:
            # without this clause, a challenge here fell through to the
            # generic `except Exception` below and retried forever, logging
            # "Unexpected error" on every poll with no actionable prompt.
            _LOGGER.warning(
                "Radoff now requires a challenge this integration cannot "
                "complete (%s), starting re-auth: %s",
                err.challenge_name,
                err,
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        except AuthInvalidError as err:
            # Card S-08: the credentials themselves are no longer valid (wrong
            # password, or the Cognito user was disabled/deleted) - raised by
            # api/auth.py::authenticate_user when a Cognito login (initial or
            # a fallback after a rejected refresh, see that module's
            # `CognitoSession.get_bearer`) replays the stored password and
            # Cognito rejects it outright. Card S-12 adds a second source: two
            # consecutive rejected `REFRESH_TOKEN_AUTH` attempts, raised
            # directly by `CognitoSession.get_bearer` without a further SRP
            # attempt. Either way this is deliberately NOT logged with
            # `_LOGGER.exception` (no traceback): it is an expected end-user
            # situation, not a bug, and is exactly the "no more infinite
            # auth-error log loops" this card asks for (a single
            # ConfigEntryAuthFailed here stops the coordinator's periodic
            # refresh until re-auth completes, instead of retrying and
            # logging every poll interval).
            _LOGGER.warning(
                "Radoff credentials are no longer valid, starting re-auth: %s", err
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        except AuthExpiredError as err:
            # Card S-08: a 401 on an authenticated call - the current session
            # is invalid but the configured credentials have not (yet) been
            # proven wrong. Stays UpdateFailed on purpose: api/client.py has
            # already invalidated the current tokens (card S-12:
            # `CognitoSession.invalidate()`, which keeps the RefreshToken), so
            # the next poll's `get_bearer()` call will attempt a refresh
            # first, and *that* is what can turn into AuthInvalidError above
            # if it keeps getting rejected. Card S-13: this can now come
            # either from the initial `search` call or from a per-device GET
            # - `API.get_devices()` never isolates it either way (see that
            # method's docstring), so it always aborts the whole cycle here,
            # same as before this card.
            _LOGGER.debug("Radoff authentication token expired, will retry: %s", err)
            msg = f"Authentication token expired: {err}"
            raise UpdateFailed(msg) from err

        except AuthUnavailableError as err:
            # Card S-09: Cognito was throttling the request, or could not be
            # reached at all (EndpointConnectionError). Says nothing about
            # whether the stored credentials are still correct, so this must
            # never open the re-auth flow - stays UpdateFailed, logged at
            # DEBUG rather than with a traceback, same reasoning as
            # AuthExpiredError above: this is an expected transient
            # condition, not a bug in this integration.
            _LOGGER.debug(
                "Radoff authentication service temporarily unavailable, "
                "will retry: %s",
                err,
            )
            msg = f"Authentication service temporarily unavailable: {err}"
            raise UpdateFailed(msg) from err

        except APIDomainAccessError as err:
            # Card M-02: the API answered 403 - this account does not belong
            # to the domain persisted on this entry (in arch 1.x the same
            # situation arrived as a 401 and was indistinguishable from an
            # expired session; see `api/client.py::_check_response_status`).
            #
            # `ConfigEntryError`, not `UpdateFailed`: retrying re-sends a
            # request that will be refused identically, so the entry stops
            # with a translated, actionable error instead of failing a poll
            # every interval forever. And not `ConfigEntryAuthFailed`
            # either: the credentials are valid, so the re-auth flow would
            # ask for a password that is not the problem. Home Assistant's
            # DataUpdateCoordinator handles `ConfigEntryError` from an update
            # by stopping, which is exactly the intent.
            # Logged at WARNING, without a traceback: this is an expected
            # end-user situation and not a bug, same reasoning as
            # `AuthInvalidError` above. The one ERROR line about it is
            # emitted where the status is classified, with the domain and
            # the backend's own detail (`api/client.py`); repeating it at
            # ERROR here would only double the noise.
            _LOGGER.warning(
                "Radoff denied access to the configured domain, "
                "reconfiguration needed: %s",
                err,
            )
            raise ConfigEntryError(
                str(err),
                translation_domain=DOMAIN,
                translation_key=ERROR_DOMAIN_ACCESS_DENIED,
            ) from err

        except APIRateLimitError as err:
            # Card M-05: a rate-limited cycle is skipped, not failed. The
            # whole decision lives in `_skip_cycle_for_rate_limit` below -
            # inline it made this method exceed ruff's branch and statement
            # limits, and it is the one clause here that does not end in a
            # raise, so it reads better named than buried in the chain.
            return self._skip_cycle_for_rate_limit(err)

        except APIAuthError as err:
            # The catch-all of the M-02 taxonomy, for a status nobody has
            # characterised yet. Card M-03: S-13 used to isolate this per
            # device when it came from one device's own GET, so only the
            # `search` failure reached here; with a single call per cycle
            # every occurrence reaches here and costs the cycle - which is
            # the honest outcome, since one call failing means no data at
            # all, not one device's worth of it.
            _LOGGER.exception("Authentication error")
            msg = f"Authentication error: {err}"
            raise UpdateFailed(msg) from err

        except requests.exceptions.Timeout as err:
            # Same reasoning as APIAuthError above: since card M-03 there is
            # one request per page, so a timeout is the cycle's.
            _LOGGER.exception(
                "Request timeout after %s seconds", self.api.DEFAULT_TIMEOUT
            )
            msg = f"Request timeout: {err}"
            raise UpdateFailed(msg) from err

        except requests.exceptions.ConnectionError as err:
            _LOGGER.exception("Connection error")
            msg = f"Connection error: {err}"
            raise UpdateFailed(msg) from err

        except Exception as err:
            _LOGGER.exception("Unexpected error")
            msg = f"Unexpected error: {err}"
            raise UpdateFailed(msg) from err

        else:
            _LOGGER.debug(
                "Radoff poll finished: %d device(s), %d of them without "
                "telemetry this cycle",
                len(devices),
                sum(1 for device in devices if device.stale),
            )
            self._check_connection_status_freshness(devices)

            return RadoffData(
                controller_name=self.api.controller_name,
                devices=devices,
                generate_index=self.generate_index,
            )

    def _check_connection_status_freshness(self, devices: list[RadoffDevice]) -> None:
        """
        Report a `connection_status` that has stopped being evidence (card M-06).

        The safety net the card asks for, and only as far as the card lets
        it go: a WARNING, never a verdict. Availability comes from
        `connection_status`, which means the client now trusts a single
        field it does not own; if that field freezes - the DynamoDB sync
        behind it stalls, a device is deleted from the sync but not from the
        list - every entity of the device stays available on the strength of
        a value that stopped being updated. This is what makes that visible.

        `CONNECTION_STATUS_STALE_WINDOW` (const.py) is the backend's own
        6-hour list window (T-02 D-16), used here as a reference rather than
        as a threshold of our own: what it says is "no fact newer than the
        window the API itself looks at", which is a defensible thing to warn
        about. It is deliberately not compared against the poll interval,
        the mistake this card exists to undo.

        Once per device per episode, and only for a device claiming to be
        connected: a `disconnected` device with an old timestamp is not
        anomalous at all - it went offline then, and the timestamp saying so
        is supposed to stay put.
        """
        now = datetime.now(UTC)

        for device in devices:
            updated_at = device.connection_status_updated_at
            age = None if updated_at is None else now - updated_at
            if (
                device.connection_state is not ConnectionState.CONNECTED
                or age is None
                or age < CONNECTION_STATUS_STALE_WINDOW
            ):
                self._connection_status_stale_warned.discard(device.serial_number)
                continue

            if device.serial_number in self._connection_status_stale_warned:
                continue

            self._connection_status_stale_warned.add(device.serial_number)
            _LOGGER.warning(
                "Device %s still reports connection_status 'connected', but "
                "that status was last updated %.1f hours ago - older than the "
                "%.0f-hour window the device list itself looks at. Its "
                "entities stay available; if this persists, the connection "
                "status may have stopped being refreshed rather than the "
                "device staying online (cadence of that refresh is the open "
                "request T-08 D-17)",
                device.serial_number,
                age.total_seconds() / 3600,
                CONNECTION_STATUS_STALE_WINDOW.total_seconds() / 3600,
            )

    def get_device_by_serial(self, serial_number: str) -> RadoffDevice | None:
        """
        Return the device with this serial in the current poll, or None.

        Card M-03: the lookup used to take `(device_type, device_id)`,
        because the arch 1.x UUID was only unique per type. In arch 2.0 the
        serial is the identity outright (T-02 D-02) - `deviceId`,
        `serial_number` and `deviceSerial` are the same value - so the type
        is no longer part of the key and asking for it would only let a
        caller build a lookup that fails when a device's type changes
        spelling.
        """
        # A `for` loop over a list cannot raise IndexError (see card S-06,
        # C17): the previous `except IndexError` here was dead code.
        for device in self.data.devices:
            if device.serial_number == serial_number:
                return device
        return None
