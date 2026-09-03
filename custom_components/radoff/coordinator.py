"""Class which represent the Radoff Coordinator."""

import logging
from dataclasses import dataclass
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
    RadoffDevice,
)
from .const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STALE_MULTIPLIER,
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

    async def async_update_data(self) -> APIData:
        """Fetch data from API endpoint."""
        _LOGGER.debug("Radoff async_update_data starting")
        try:
            if not self.api.connected:
                _LOGGER.info("API not connected, attempting to connect...")
                await self.hass.async_add_executor_job(self.api.connect)
                _LOGGER.info("API connection established")

            devices = await self.hass.async_add_executor_job(self.api.get_devices)

            _LOGGER.debug("Successfully fetched data for %d device", len(devices))
            return APIData(
                controller_name=self.api.controller_name,
                devices=devices,
                generate_index=self.generate_index,
            )

        except AuthChallengeRequiredError as err:
            # Card S-09: a config entry can only exist for an account whose
            # initial setup completed with a full Cognito login (no config
            # entry is ever created for an account still on a challenge -
            # see config_flow.py's validate_input). If the periodic reconnect
            # this method performs (see `get_devices()`) ever hits a
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
            # api/auth.py::authenticate_user when connect() (initial or
            # periodic reconnect, see that module's docstring for the timing
            # caveat) replays the stored password and Cognito rejects it
            # outright. This is deliberately NOT logged with `_LOGGER.exception`
            # (no traceback): it is an expected end-user situation, not a bug,
            # and is exactly the "no more infinite auth-error log loops" this
            # card asks for (a single ConfigEntryAuthFailed here stops the
            # coordinator's periodic refresh until re-auth completes, instead
            # of retrying and logging every poll interval).
            _LOGGER.warning(
                "Radoff credentials are no longer valid, starting re-auth: %s", err
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        except AuthExpiredError as err:
            # Card S-08: a 401 on an authenticated call - the current session
            # is invalid but the configured credentials have not (yet) been
            # proven wrong. Stays UpdateFailed on purpose: api/client.py has
            # already disconnected, so the next poll will attempt a fresh
            # connect() and *that* is what can turn into AuthInvalidError above if
            # the password truly changed.
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
            _LOGGER.exception("Authentication error")
            msg = f"Authentication error: {err}"
            raise UpdateFailed(msg) from err

        except requests.exceptions.Timeout as err:
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

    def get_device_by_id(self, device_type: str, device_id: str) -> RadoffDevice | None:
        """Return device by device id."""
        _LOGGER.debug("Radoff get_device_by_id")
        # A `for` loop over a list cannot raise IndexError (see card S-06,
        # C17): the previous `except IndexError` here was dead code.
        for device in self.data.devices:
            if device.device_type == device_type and device.device_id == device_id:
                return device
        return None