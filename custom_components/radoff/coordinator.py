"""Class which represent the Radoff Coordinator."""

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import timedelta

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import DOMAIN as HOMEASSISTANT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    API,
    APIAuthError,
    AuthChallengeRequiredError,
    AuthExpiredError,
    AuthInvalidError,
    AuthUnavailableError,
    DeviceFetchError,
    RadoffDevice,
)
from .const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_MULTIPLIER,
    UPDATE_TIMEOUT_FACTOR,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class APIData:
    """Class to hold api data."""

    controller_name: str
    generate_index: bool
    devices: list[RadoffDevice]


class RadoffCoordinator(DataUpdateCoordinator):
    """The implementation of the Radoff coordinator."""

    data: APIData

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.username = config_entry.data[CONF_USERNAME]
        self.password = config_entry.data[CONF_PASSWORD]
        self.domain_id = config_entry.data[CONF_DOMAIN_ID]

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

        super().__init__(
            hass,
            _LOGGER,
            name=f"{HOMEASSISTANT_DOMAIN} ({config_entry.unique_id})",
            update_method=self.async_update_data,
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
            domain_id=self.domain_id,
            scan_interval=self.poll_interval,
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

    def _merge_device_errors(
        self,
        devices_ok: list[RadoffDevice],
        device_errors: list[DeviceFetchError],
    ) -> list[RadoffDevice]:
        """
        Fold this cycle's per-device fetch failures back into the device list.

        Card S-13, "COSA FARE" step 2. For each `DeviceFetchError` (a device
        `API.get_devices()` could not fetch this cycle, see that method's
        docstring for which errors qualify): look up the previous poll's
        `RadoffDevice` with the same `(device_type, device_id)`, if any -

        - found: reuse it as-is (same `readings`, same
          `last_data_received_at` - "conserva le sue letture precedenti"),
          only flipping `stale` to `True`. `dataclasses.replace` is used
          instead of mutating the previous instance in place, so the
          previous poll's `APIData.devices` (which HA entities may still be
          reading from concurrently) is never touched.
        - not found (this device has never been seen with a successful
          fetch - e.g. its very first poll already failed): include it
          anyway, with empty `readings` and `stale=True`, rather than
          dropping it from this cycle's device list entirely (card's own
          instruction: "se non esiste un dato precedente, includerlo con
          readings vuoto e stale=True").

        `self.data` does not exist yet before this coordinator's first
        successful refresh (`DataUpdateCoordinator.__init__` sets it to
        `None`); `getattr` guards that first-poll case explicitly rather
        than relying on `self.data` always being set.
        """
        previous_devices: dict[tuple[str, str], RadoffDevice] = {}
        previous_data = getattr(self, "data", None)
        if previous_data is not None:
            previous_devices = {
                (device.device_type, device.device_id): device
                for device in previous_data.devices
            }

        merged = list(devices_ok)
        for error in device_errors:
            previous = previous_devices.get((error.device_type, error.device_id))
            if previous is not None:
                merged.append(replace(previous, stale=True))
            else:
                merged.append(
                    RadoffDevice(
                        device_id=error.device_id,
                        device_serial=error.device_serial,
                        device_type=error.device_type,
                        name=error.name,
                        readings={},
                        stale=True,
                        last_data_received_at=None,
                    )
                )
            _LOGGER.debug(
                "Device %s (%s) marked stale after a per-device fetch error: %s",
                error.device_id,
                error.device_type,
                error.error,
            )
        return merged

    async def async_update_data(self) -> APIData:
        """
        Fetch data from API endpoint.

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
           version (`hacs.json`: 2024.6.0) already requires Python ≥3.11, so
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
        2. `API.get_devices()` now returns `(devices_ok, device_errors)`
           instead of raising on a single bad device (card S-13, see that
           method's docstring); `_merge_device_errors` above folds
           `device_errors` back into the device list actually stored in
           `self.data`, and the DEBUG line below reports how many devices
           updated cleanly vs. went stale this cycle (S-13 "COSA FARE" step
           5).
        """
        _LOGGER.debug("Radoff async_update_data starting")
        timeout_seconds = self.update_interval.total_seconds() * UPDATE_TIMEOUT_FACTOR

        try:
            async with asyncio.timeout(timeout_seconds):
                devices_ok, device_errors = await self.hass.async_add_executor_job(
                    self.api.get_devices
                )

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

        except APIAuthError as err:
            # Card S-13: per-device APIAuthError (403/429/5xx on a single
            # device's GET) is now isolated inside `API.get_devices()` and
            # never reaches this point - only the `search` call's own
            # failure (which is never isolated, see that method's docstring)
            # still surfaces here, same as before this card (S-13 AC: "Un
            # fallimento della search porta tutte le entità a unavailable,
            # come oggi").
            _LOGGER.exception("Authentication error")
            msg = f"Authentication error: {err}"
            raise UpdateFailed(msg) from err

        except requests.exceptions.Timeout as err:
            # Card S-13: same reasoning as APIAuthError above - a per-device
            # timeout is isolated inside `get_devices()`; only a `search`-level
            # timeout still reaches here.
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
            devices = self._merge_device_errors(devices_ok, device_errors)

            _LOGGER.debug(
                "Radoff poll finished: %d device(s) updated, %d stale",
                len(devices_ok),
                len(device_errors),
            )

            return APIData(
                controller_name=self.api.controller_name,
                devices=devices,
                generate_index=self.generate_index,
            )

    def get_device_by_id(self, device_type: str, device_id: str) -> RadoffDevice | None:
        """Return device by device id."""
        _LOGGER.debug("Radoff get_device_by_id")
        # A `for` loop over a list cannot raise IndexError (see card S-06,
        # C17): the previous `except IndexError` here was dead code.
        for device in self.data.devices:
            if device.device_type == device_type and device.device_id == device_id:
                return device
        return None
