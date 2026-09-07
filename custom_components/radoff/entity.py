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
Only the device's *identity* (type, id, serial, name) is captured once at
creation time, for `device_info`/`unique_id`, which never change poll to
poll. Everything data-bearing - `_device`, `_reading`, `available` - is
looked up fresh from `self.coordinator.data` on every access: a device or
property that disappears from one poll is reflected immediately (S-07 AC:
"entro un ciclo di poll"), and one that reappears needs no reload to come
back (S-07 AC), because there is no stale cached object left to clear.

Card S-10 changes what `reading_key` identifies: it used to be the bare
property name (a `str`); it is now the composite `ReadingKey = (Bucket,
property_name)` `api/models.py` introduces to resolve finding C4 (a `data`
and an `aggregatedData` reading for the same property no longer collide in
`RadoffDevice.readings`). `unique_id` below is the one place in this class
that has to turn that tuple back into a single identifier string, and it
does so through `reading_key_slug()` - also used by
`sensor.py::RadoffSensor.translation_key` - so both stay in lockstep and
the DATA-bucket case keeps producing byte-for-byte the same string it did
before this card (S-10 AC: "Le entità esistenti del bucket DATA mantengono
unique_id e storico").
"""

import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import Bucket, RadoffDevice, Reading, ReadingKey
from .const import DOMAIN
from .coordinator import RadoffCoordinator

_LOGGER = logging.getLogger(__name__)

# Suffix appended to a property name to build its entity "slug" (used for
# both `unique_id` and `translation_key`), per bucket. DATA is the bucket
# every entity was built from before S-10 introduced any other one, so it
# keeps the empty suffix - the exact identifier a DATA-bucket reading always
# had - and every bucket added after it must pick its own non-empty suffix
# here so it can never collide with DATA's, or with another new bucket's.
_BUCKET_SLUG_SUFFIXES: dict[Bucket, str] = {
    Bucket.DATA: "",
    Bucket.AGGREGATED: "average",
}


def reading_key_slug(reading_key: ReadingKey) -> str:
    """
    Return the property-name-based slug identifying one reading's entities.

    `(Bucket.DATA, "airqualityindex")` -> `"airqualityindex"` (unchanged from
    the pre-S-10 bare-string key, by construction - see
    `_BUCKET_SLUG_SUFFIXES`). `(Bucket.AGGREGATED, "airqualityindex")` ->
    `"airqualityindex_average"`, a distinct slug so the two never collide in
    `unique_id` or `translation_key`.
    """
    bucket, property_name = reading_key
    suffix = _BUCKET_SLUG_SUFFIXES[bucket]
    return property_name if not suffix else f"{property_name}_{suffix}"


class RadoffEntity(CoordinatorEntity[RadoffCoordinator]):
    """Base class for Radoff entities backed by a single (device, reading) pair."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: RadoffCoordinator,
        device: RadoffDevice,
        reading_key: ReadingKey,
    ) -> None:
        """
        Initialize the entity from the device object seen when it was created.

        Only identity fields are copied out of `device` here; the coordinator
        context (`context=reading_key`) is what CoordinatorEntity uses to
        support selective updates in the future - it is set once, correctly,
        and never overwritten afterwards (unlike the pre-S-07 `sensor.py`,
        which reassigned `self.coordinator_context` to the coordinator object
        right after `super().__init__()`, clobbering it - see
        `analisi-codebase-radoff-ha-28ago.md`, D6).
        """
        super().__init__(coordinator, context=reading_key)
        self._device_type = device.device_type
        self._device_id = device.device_id
        self._device_serial = device.device_serial
        self._device_name = device.name
        self.reading_key = reading_key

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Metadata only (S-03 logging policy): reading key and device id
        # (UUID, allowed at DEBUG), never a reading's value.
        _LOGGER.debug("Refreshing %s for device %s", self.reading_key, self._device_id)
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
        of the search response makes its entities unavailable within the
        very next poll, with no special-casing needed elsewhere.
        """
        return self.coordinator.get_device_by_id(self._device_type, self._device_id)

    @property
    def _reading(self) -> Reading | None:
        """Return this entity's current Reading, or None if not in this poll."""
        device = self._device
        if device is None:
            return None
        return device.readings.get(self.reading_key)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_serial)},
            name=self._device_name,
            manufacturer="Radoff",
            model=self._device_type,
        )

    @property
    def unique_id(self) -> str:
        """
        Return unique id.

        Built from `reading_key_slug()`, not from `self.reading_key` directly
        (card S-10): `reading_key` is now `(Bucket, property_name)`, and the
        slug is what turns that back into the single identifier string this
        integration has always used, unchanged for the DATA bucket.
        """
        return f"{DOMAIN}-{self._device_id}-{reading_key_slug(self.reading_key)}"

    @staticmethod
    def _freshness_reference(reading: Reading, device: RadoffDevice) -> datetime | None:
        """
        Return the best available timestamp to judge this reading's age by.

        Prefers `reading.measured_at` (per-sample, most precise) and falls
        back to `device.last_data_received_at` (device-level, coarser but
        real - see `api/client.py`'s T-02 finding: the payload has no
        per-sample timestamp, but does have this one, one level up in the
        same response). `None` only if neither is known, which today is
        always true for the first and would only be true for the second on
        a payload shape this integration hasn't seen yet.
        """
        return reading.measured_at or device.last_data_received_at

    @property
    def available(self) -> bool:
        """
        Return whether this entity's reading is available.

        Three conditions, all required (card S-07, §"COSA FARE"):

        1. the coordinator's last update succeeded (`super().available`);
        2. the device is not marked `stale` (card S-13's signal; the field
           always reads `False` today - S-13 is what will start setting it,
           in that order deliberately, see this card's "OUT OF SCOPE");
        3. the reading exists and, when a freshness reference is known (see
           `_freshness_reference`), its age is below `coordinator.stale_after`.

        T-02 confirmed the payload has no per-sample timestamp
        (`Reading.measured_at` stays `None`), but does expose a device-level
        `lastDataReceivedAt` (`RadoffDevice.last_data_received_at`), which
        condition 3 uses instead. Only if that were ever unavailable too
        would condition 3 fully degrade to "the reading exists" - the
        fallback the card allows, and asks to be declared rather than
        silently hidden (hence this docstring, not a TODO buried in code).
        """
        if not super().available:
            return False

        device = self._device
        if device is None or device.stale:
            return False

        reading = device.readings.get(self.reading_key)
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
