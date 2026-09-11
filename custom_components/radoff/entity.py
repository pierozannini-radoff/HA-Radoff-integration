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

Card M-06 qualifies the paragraph above, and the qualification is worth
reading before trusting it. What is not cached here is still not cached: the
device object, the reading and `available` are all looked up fresh, so a
device that leaves the poll takes its entities unavailable within one cycle
exactly as before. What *is* now remembered, one level down in
`sensor.py::RadoffSensor`, is the last *value* an entity published - because
availability no longer implies that a value exists (a connected device can
answer `telemetry: null` for hours by the API's own 6-hour window, T-02
D-16), and an available entity has to show something. That cache is a value
and its timestamp, never a `Device`, which is what makes it unable to
resurrect the C1 defect S-07 removed: no code path reads identity or
availability from it.

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
from datetime import datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import ConnectionState, RadoffDevice, Reading
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

    @property
    def measured_at(self) -> datetime | None:
        """
        Return the timestamp the API attached to this entity's own reading.

        `None` when this entity has no reading in the current poll, which
        since card M-06 is a normal state rather than an unavailable one:
        the device may be connected and simply have nothing to say (T-02
        D-16 - the list looks at a 6-hour window and does not widen it).

        Card M-06 changed what this is for, which is why it is no longer
        called `_freshness_reference`. It used to be the input to an
        availability decision; it is now published, as the
        `last_measured_at` attribute, and read by nothing else. The
        distinction matters because the value is coarse in a known way -
        one timestamp for the whole telemetry block, the maximum over its
        fields (T-08 D-15) - and a coarse number is a poor judge and a
        perfectly good witness.

        The device-level fallback S-07 had here (`telemetry_timestamp` when
        the reading carried none) is gone with the same change, and its
        removal is deliberate. That fallback answered "when did this
        *device* last speak", which is a different question, and after
        M-04 - entities come from the type's schema, so an entity exists
        for a measure this poll never carried - answering it here would
        stamp another field's timestamp on a value this entity does not
        have. `RadoffSensor` overrides this to report the timestamp that
        came with the value it is actually showing, including a remembered
        one (see `sensor.py`).
        """
        reading = self._reading
        return None if reading is None else reading.measured_at

    @property
    def available(self) -> bool:
        """
        Return whether this entity's device is reachable (card M-06).

        Three conditions of card S-07 become two, and the one that leaves
        is the one that was wrong:

        1. the coordinator's last update succeeded (`super().available`);
        2. the device is present in the current poll;
        3. the device's `connection_state` is not `DISCONNECTED`.

        What is gone is any judgement about *this entity's reading*. S-07
        required the reading to exist and to be younger than
        `update_interval * 3`, and both halves of that were the wrong
        question asked of the wrong data. The backend told us so twice
        (T-02 D-16, D-21): `GET /data/devices` looks at a 6-hour window and
        does not widen it on a miss, so a device silent for longer answers
        `telemetry: null` *while connected*, and the field to decide
        availability from is `connection_status`. A missing reading now
        means "no value to show right now", not "this device is gone" - and
        since card M-04 that distinction is visible on every entity of a
        silent device instead of on none, because the entities come from
        the type's schema rather than from the payload.

        Condition 1 is what keeps a failed cycle from being read as a
        device going offline, and it is why the 429 handling of card M-05
        *skips* rather than fails: a skipped cycle keeps
        `last_update_success` true and the previous cycle's data, so
        entities stay available; a genuinely failed cycle sets it false and
        takes them all unavailable, which is the honest "we do not know".

        Condition 3 asks `connection_state`, not `connection_status`, and
        that is the whole defence against the enumeration we do not have
        yet (T-08 D-17): an unrecognised value classifies as
        `INDETERMINATE` and leaves the entity available, with a WARNING
        emitted once where the value enters the model
        (`api/client.py::_build_device`). Only an explicit `disconnected`
        takes entities down. A `status` of `active`/anything else plays no
        part: it is administrative, coexists with this field, and is
        exposed as an attribute precisely so nobody has to guess which of
        the two means what.
        """
        if not super().available:
            return False

        device = self._device
        if device is None:
            return False

        return device.connection_state is not ConnectionState.DISCONNECTED

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """
        Expose the two timestamps and the two statuses a support case needs.

        Card M-06's own acceptance criterion ("gli attributi diagnostici
        espongono entrambi i timestamp"), and the reason it is a criterion
        is that the two answer different questions and are routinely
        confused: `last_measured_at` says when the device last produced
        data, `connection_status_updated_at` when the backend last changed
        its mind about the device being connected. A device reported
        offline with a recent measurement, or connected with a measurement
        from last week, are two different faults - and telling them apart
        from a screenshot requires both numbers on the entity, not one in
        the entity and one in a diagnostics download.

        `status` rides along for the same reason: it is the administrative
        status (`active`), a different field from `connection_status`, and
        no decision in this integration reads it. Publishing it is how we
        avoid having to choose between the two before T-08 D-17 tells us
        what each one enumerates.

        `connection_status_updated_at` is published raw, and reading it
        needs one fact the live pass of this card established on dev (T-02
        D-17 (e)): it is the moment the status last *changed*, not the
        moment it was last *checked*. A healthy device transmitting right
        now carried a value two days old, while a device offline for two
        months carried one 64 days old. So an old timestamp beside
        `connected` means "connected since then", which is the normal state
        of a stable installation - not evidence that anything has frozen.
        An earlier pass of this card added a `connection_status_stale`
        attribute and a matching WARNING built on the opposite reading, and
        the live pass removed both before the card closed: against a
        last-change timestamp no age threshold can separate a stable device
        from a frozen field, so the check could only ever have fired on
        every healthy device. Whether that field is refreshed on any
        cadence at all is the remaining half of D-17 (b), still open.

        Keys whose value is unknown are omitted rather than published as
        `None`, so an attribute that is present is always a fact.
        """
        device = self._device
        if device is None:
            return None

        attributes: dict[str, Any] = {}
        measured_at = self.measured_at
        if measured_at is not None:
            attributes["last_measured_at"] = measured_at.isoformat()
        if device.connection_status is not None:
            attributes["connection_status"] = device.connection_status
        if device.connection_status_updated_at is not None:
            attributes["connection_status_updated_at"] = (
                device.connection_status_updated_at.isoformat()
            )
        if device.status is not None:
            attributes["status"] = device.status

        return attributes or None
