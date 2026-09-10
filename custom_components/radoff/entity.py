"""
Base entity for the radoff integration.

Introduced by card S-07 as the single place where `device_info`, `unique_id`
and `available` are implemented, instead of the duplicated implementation
`sensor.py::RadoffSensor` used to carry on its own (see
`architettura-target-sprint-m.md` §6-7, and the analysis's C1/C2 findings).
Every current and future Radoff entity type should inherit from this instead
of `CoordinatorEntity` directly.

Unlike the stopgap S-06 introduced in `sensor.py::_handle_coordinator_update`
(keep the last known `Device` when the coordinator no longer has one), this
entity does NOT cache the device object across updates - see
`s-06-implementazione.md`'s own follow-up note: "S-07 riprenderà questo
lavoro per l'availability strutturata [...] le guardie minime introdotte qui
[...] sono pensate per essere sostituite, non duplicate, da quella card."
Only the device's *identity* (serial and type) is captured once at creation
time, for `device_info`/`unique_id`, which never change poll to poll.
Everything data-bearing - `_device`, `_reading`, `available` - is looked up
fresh from `self.coordinator.data` on every access: a device or field that
disappears from one poll is reflected immediately (S-07 AC: "entro un ciclo
di poll"), and one that reappears needs no reload to come back (S-07 AC),
because there is no stale cached object left to clear.

Card M-03 removes the last layer of indirection between a reading and its
identifier. S-10 keyed readings by a (bucket, property name) pair and
needed `reading_key_slug()` to fold that tuple back into one string; arch
2.0 has no buckets, so the key is the field name and the slug *is* the
key. The
identifier itself changes shape with it - `radoff-{serial_number}-{field}`
instead of `radoff-{device_uuid}-{slug}` - because the UUID of arch 1.x does
not exist in 2.0 at all (T-02 D-02). Re-keying the entities users already
have onto the new form is card M-07, deliberately not this one: until it
runs, an existing installation gets new entities beside the old ones.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import RadoffDevice, Reading
from .const import DOMAIN
from .coordinator import RadoffCoordinator

_LOGGER = logging.getLogger(__name__)


class RadoffEntity(CoordinatorEntity[RadoffCoordinator]):
    """Base class for Radoff entities backed by a single (device, field) pair."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RadoffCoordinator,
        device: RadoffDevice,
        field: str,
    ) -> None:
        """
        Initialize the entity from the device object seen when it was created.

        Only identity fields are copied out of `device` here; the coordinator
        context (`context=field`) is what CoordinatorEntity uses to
        support selective updates in the future - it is set once, correctly,
        and never overwritten afterwards (unlike the pre-S-07 `sensor.py`,
        which reassigned `self.coordinator_context` to the coordinator object
        right after `super().__init__()`, clobbering it - see
        `analisi-codebase-radoff-ha-28ago.md`, D6).
        """
        super().__init__(coordinator, context=field)
        self._device_type = device.device_type
        self._serial_number = device.serial_number
        self._device_name = device.name
        self._firmware_version = device.firmware_version
        self.field = field

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Metadata only (S-03 logging policy): field name and device serial,
        # never a reading's value.
        _LOGGER.debug("Refreshing %s for device %s", self.field, self._serial_number)
        super()._handle_coordinator_update()

    @property
    def _device(self) -> RadoffDevice | None:
        """
        Look up this entity's device fresh from the coordinator's current data.

        Returns `None` when the device is absent from the current poll.
        Every property below treats that as "no data" rather than falling
        back to a previously seen value - the S-06 stopgap did the latter,
        which is what left `native_value` able to raise on a vanished device
        (C1) once its cached `Device` eventually went stale in a different
        way. This lookup is what makes S-07's AC1 hold: a device dropping out
        of the device list makes its entities unavailable within the very
        next poll, with no special-casing needed elsewhere.
        """
        return self.coordinator.get_device_by_serial(self._serial_number)

    @property
    def _reading(self) -> Reading | None:
        """Return this entity's current Reading, or None if not in this poll."""
        device = self._device
        if device is None:
            return None
        return device.readings.get(self.field)

    @property
    def device_info(self) -> DeviceInfo:
        """
        Return the device info.

        `identifiers` is unchanged by card M-03 and that is the point: the
        device registry has been keyed on the serial since S-07, so the
        devices users already have survive this migration even though every
        entity `unique_id` under them changes (M-07 re-keys those).

        `sw_version` is new here - arch 2.0 reports `firmware_version` on
        every device of the list response, where 1.x exposed nothing of the
        sort.
        """
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            name=self._device_name,
            manufacturer="Radoff",
            model=self._device_type,
            sw_version=self._firmware_version,
        )

    @property
    def unique_id(self) -> str:
        """
        Return unique id: `radoff-{serial_number}-{field}` (card M-03).

        Both halves changed at once. The identity is the serial, because
        arch 2.0 has no device UUID to use instead (T-02 D-02), and the
        suffix is the telemetry field name, because there is no bucket left
        to disambiguate and therefore no slug to build. Migrating the
        identifiers of existing entities onto this form is card M-07.
        """
        return f"{DOMAIN}-{self._serial_number}-{self.field}"

    @staticmethod
    def _freshness_reference(reading: Reading, device: RadoffDevice) -> datetime | None:
        """
        Return the best available timestamp to judge this reading's age by.

        Prefers `reading.measured_at` and falls back to the device-level
        `telemetry_timestamp`. In arch 2.0 the two are the *same value* -
        the whole telemetry block carries one timestamp, which every reading
        of that device copies (see `api/models.py::Reading`) - so the
        fallback only matters for a device with no readings at all. It is
        kept as two steps anyway: the day the API grows a per-field
        timestamp, this method already prefers it.

        Note what that identity implies, and what T-08 D-15 reserves on:
        this check cannot notice one slow field going stale while the rest
        of the block keeps refreshing. Radon is the case that will bite -
        see `api/models.py::Reading.measured_at`.
        """
        return reading.measured_at or device.telemetry_timestamp

    @property
    def available(self) -> bool:
        """
        Return whether this entity's reading is available.

        Three conditions, all required (card S-07, §"COSA FARE"):

        1. the coordinator's last update succeeded (`super().available`);
        2. the device is not marked `stale`;
        3. the reading exists and, when a freshness reference is known (see
           `_freshness_reference`), its age is below `coordinator.stale_after`.

        Condition 2 keeps its wording and changes its meaning with card
        M-03: `stale` no longer means "this device's own fetch failed" (an
        N+1 notion that died with the per-device call) but "this device
        reported no telemetry this cycle" - `telemetry: null`. The reaction
        is the same, and correct for both: no data means nothing to show.

        What condition 2 is deliberately NOT is a connection check. A device
        can be `connection_status: disconnected` and still have the API
        serve its last telemetry, in which case these entities stay
        available and condition 3 ages them out on its own schedule.
        Deciding availability from `connection_status` is card M-06.
        """
        if not super().available:
            return False

        device = self._device
        if device is None or device.stale:
            return False

        reading = device.readings.get(self.field)
        if reading is None:
            return False

        freshness_reference = self._freshness_reference(reading, device)
        if freshness_reference is None:
            return True

        age = datetime.now(UTC) - freshness_reference
        return age < self.coordinator.stale_after

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the reading's freshness reference, when known, for support."""
        device = self._device
        reading = self._reading
        if device is None or reading is None:
            return None
        freshness_reference = self._freshness_reference(reading, device)
        if freshness_reference is None:
            return None
        return {"last_measured_at": freshness_reference.isoformat()}
