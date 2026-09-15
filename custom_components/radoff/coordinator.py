"""Class which represent the Radoff Coordinator."""

import asyncio
import hashlib
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
    APIUnknownDeviceTypeError,
    AuthChallengeRequiredError,
    AuthExpiredError,
    AuthInvalidError,
    AuthUnavailableError,
    RadoffDevice,
)
from .const import (
    CONF_BASE_URL,
    CONF_DOMAIN_PREFIX,
    CONF_INDEX,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ERROR_DOMAIN_ACCESS_DENIED,
    ISSUE_MISSING_DOMAIN_PREFIX,
    POLL_JITTER_FRACTION,
    UPDATE_TIMEOUT_FACTOR,
)
from .issues import async_create_domain_access_denied_issue
from .schema import MeasureSpec, build_specs

_LOGGER = logging.getLogger(__name__)


def _stable_fraction(seed: str) -> float:
    """
    Return a number in [0, 1) that is always the same for the same `seed`.

    Neither `random` nor `hash()` would do: one redraws every restart, the
    other every process, and an offset that moves on restart puts every
    installation that just restarted back in lockstep. No cryptographic claim
    is made here.
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

        # Repeated from setup so no other caller can reach the bare
        # `KeyError` this replaces, which Home Assistant never retries.
        domain_prefix = config_entry.data.get(CONF_DOMAIN_PREFIX)
        if not domain_prefix:
            msg = f"Config entry {config_entry.entry_id} has no {CONF_DOMAIN_PREFIX}"
            raise ConfigEntryError(
                msg,
                translation_domain=DOMAIN,
                translation_key=ISSUE_MISSING_DOMAIN_PREFIX,
            )
        self.domain_prefix = domain_prefix

        # generate_index and scan_interval live in options, not data: they are
        # user preferences, not connection data.
        self.generate_index = config_entry.options.get(CONF_INDEX, True)

        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        # Read here rather than in the client so a change takes effect on the
        # reload the options flow triggers.
        self.base_url = config_entry.options.get(CONF_BASE_URL, DEFAULT_BASE_URL)

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{HOMEASSISTANT_DOMAIN} ({config_entry.unique_id})",
            update_interval=timedelta(seconds=self.poll_interval),
        )

        # Drawn once from the entry_id, so it survives restarts and differs
        # between installations. The delay below is a one-shot set by a 429.
        self._jitter_fraction = (
            _stable_fraction(config_entry.entry_id) * POLL_JITTER_FRACTION
        )
        self._rate_limit_delay: float | None = None

        # `scan_interval` is passed through only so a 429 can name the
        # interval actually in effect for this entry.
        self.api = API(
            username=self.username,
            password=self.password,
            domain_prefix=self.domain_prefix,
            scan_interval=self.poll_interval,
            base_url=self.base_url,
        )

        # Filled once at setup, never per poll: a schema describes a product,
        # not a reading. It can be empty, so every read goes through `.get`.
        self.schemas: dict[str, dict[str, MeasureSpec]] = {}

    async def async_load_schemas(self) -> None:
        """
        Fetch and cache the measurement schema of every device type seen.

        Called once at setup, after the first refresh makes the types known
        and before the sensor platform consumes the result. One call per
        type, not per device. An unknown type is not fatal: it caches an
        empty schema and the device's entities are built from telemetry
        alone. Every other failure propagates, so setup is retried rather
        than completed with nameless, unitless entities.
        """
        for device_type in sorted({device.device_type for device in self.data.devices}):
            if device_type in self.schemas:
                continue

            if not device_type:
                # An empty `device_type` returns the merged all-types answer,
                # which is worse than none.
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
        Return this entry's fixed offset between one poll and the next.

        A constant share of `update_interval`, so it follows an interval
        changed from the options flow. Applied to every period rather than
        once at startup, since a shared restart would otherwise re-align
        every installation that went through it.
        """
        return self.update_interval * self._jitter_fraction

    def _schedule_refresh(self) -> None:
        """
        Schedule the next cycle, honouring jitter and any pending 429.

        Swaps in the delay this one cycle should use, delegates to Home
        Assistant's own scheduling, and restores the nominal interval: the
        timeout budget and the jitter share are derived from it and must not
        move because one cycle was rate limited. A pending backoff only ever
        lengthens the wait - honouring a 5s backoff literally would poll a
        rate-limited backend harder than a healthy one.
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

        Returns the previous cycle's data rather than raising `UpdateFailed`:
        a 429 is about a quota shared with other clients and says nothing
        about the devices, which are still emitting. The backoff is honoured
        as a floor, so a streak of them backs this integration off instead of
        polling through it. A cycle that fails outright still takes every
        entity unavailable, which is the honest "we do not know".
        """
        if self.data is None:
            # No previous cycle to return, so this is the one case where a
            # 429 has to fail: setup is retried with its own backoff.
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

    def _async_raise_domain_access_issue(self) -> None:
        """Raise the 403 repair for this entry."""
        if self.config_entry is not None:
            async_create_domain_access_denied_issue(self.hass, self.config_entry)

    async def _async_update_data(self) -> RadoffData:
        """
        Fetch data from API endpoint.

        One flat `try`/`except` chain, one clause per failure this
        integration reacts to differently; the length is the exhaustiveness.
        Authentication happens lazily inside `get_devices()`.

        The wall-clock budget bounds how long this coordinator *waits*, not
        the blocking HTTP call itself: the fetch runs in the executor, so a
        hung request keeps its thread until its own per-request timeout
        fires.
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
            # On an already-configured account this means the credentials no
            # longer authenticate; re-auth then aborts with a clear reason.
            _LOGGER.warning(
                "Radoff now requires a challenge this integration cannot "
                "complete (%s), starting re-auth: %s",
                err.challenge_name,
                err,
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        except AuthInvalidError as err:
            # No traceback: an expected end-user situation, not a bug.
            # Raising stops the refresh until re-auth completes.
            _LOGGER.warning(
                "Radoff credentials are no longer valid, starting re-auth: %s", err
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        except AuthExpiredError as err:
            # The session is invalid but the credentials are not proven wrong,
            # so only a rejected refresh later becomes `AuthInvalidError`.
            _LOGGER.debug("Radoff authentication token expired, will retry: %s", err)
            msg = f"Authentication token expired: {err}"
            raise UpdateFailed(msg) from err

        except AuthUnavailableError as err:
            # Says nothing about whether the credentials are still correct,
            # so it must never open the re-auth flow.
            _LOGGER.debug(
                "Radoff authentication service temporarily unavailable, "
                "will retry: %s",
                err,
            )
            msg = f"Authentication service temporarily unavailable: {err}"
            raise UpdateFailed(msg) from err

        except APIDomainAccessError as err:
            # Stops the entry rather than failing a poll for ever, and not as
            # an auth failure: the credentials are valid, the domain is not.
            _LOGGER.warning(
                "Radoff denied access to the configured domain, "
                "reconfiguration needed: %s",
                err,
            )
            self._async_raise_domain_access_issue()
            raise ConfigEntryError(
                str(err),
                translation_domain=DOMAIN,
                translation_key=ERROR_DOMAIN_ACCESS_DENIED,
            ) from err

        except APIRateLimitError as err:
            # A rate-limited cycle is skipped, not failed: the only clause
            # here that does not end in a raise.
            return self._skip_cycle_for_rate_limit(err)

        except APIAuthError as err:
            # The catch-all, for a status nobody has characterised yet. One
            # call per cycle means one failure costs the whole cycle.
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
        """Return the device with this serial in the current poll, or None."""
        for device in self.data.devices:
            if device.serial_number == serial_number:
                return device
        return None
