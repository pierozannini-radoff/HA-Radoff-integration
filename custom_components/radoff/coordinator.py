"""Class which represent the Radoff Coordinator."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

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
    AuthChallengeRequiredError,
    AuthExpiredError,
    AuthInvalidError,
    AuthUnavailableError,
    RadoffDevice,
)
from .const import (
    CONF_BASE_URL,
    CONF_DOMAIN_ID,
    CONF_INDEX,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_MULTIPLIER,
    DOMAIN,
    ERROR_DOMAIN_ACCESS_DENIED,
    ISSUE_MISSING_DOMAIN_ID,
    UPDATE_TIMEOUT_FACTOR,
)

_LOGGER = logging.getLogger(__name__)


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

    @property
    def stale_after(self) -> timedelta:
        """
        Return the age above which a reading is considered stale (card S-07).

        Derived from `update_interval` rather than stored separately, so it
        always tracks the poll interval actually in effect (including one
        changed later via the options flow, S-11) without needing its own
        update listener. `DEFAULT_STALE_MULTIPLIER` (const.py) stays an
        internal constant, not a user-facing option: S-11 considered exposing
        it and decided against it (see const.py's comment on that constant).
        """
        return self.update_interval * DEFAULT_STALE_MULTIPLIER

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
            # Card M-02: HTTP 429 on the shared per-environment quota. Stays
            # `UpdateFailed` - the cycle is lost - but is logged as the
            # expected, transient, not-our-fault condition it is, without a
            # traceback, and carries the backoff the client computed
            # (`err.retry_after`; API Gateway sends no `Retry-After`).
            #
            # What this clause does NOT do yet is honour that delay by
            # actually rescheduling the next refresh, nor keep the entities
            # available across the skipped cycle - a 429 is not a device
            # going offline. Both are scheduling decisions and belong to the
            # polling-interval card of this migration (decided with Piero);
            # this card's job is to stop retrying immediately and to make
            # the delay available to whoever will apply it.
            _LOGGER.warning(
                "Radoff rate limit reached; this cycle is skipped and the "
                "next attempt should wait ~%.1fs: %s",
                err.retry_after,
                err,
            )
            msg = f"Rate limited by the Radoff API: {err}"
            raise UpdateFailed(msg) from err

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

            return RadoffData(
                controller_name=self.api.controller_name,
                devices=devices,
                generate_index=self.generate_index,
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
