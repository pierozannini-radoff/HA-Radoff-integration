"""Class which represent the Radoff sensor entity."""

import logging
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RadoffConfigEntry
from .api import RadoffDevice
from .coordinator import RadoffCoordinator
from .entity import RadoffEntity
from .schema import MeasureSpec

_LOGGER = logging.getLogger(__name__)

# Measures whose entity is created but left disabled in the entity registry.
#
# `aqi_value` is here for one reason, and it is a backend bug rather than a
# preference: the AQI's temperature component is computed with a divisor of
# 120 that does not belong there (`aqi_calculator.py`, faithful to a legacy
# implementation, while `internal_temperature` already arrives in °C), so
# the published index is systematically wrong - T-08 D-08, still open.
#
# Disabled is the right shape for "wrong but not absent": the entity exists,
# a user who wants it can enable it in one click, and nothing about it is
# invented. When the backend fixes the divisor we flip this default back and
# every installation picks the change up on upgrade - no migration, no
# re-keying, no entity appearing or disappearing under anyone.
#
# The qualitative sibling goes with it: `aqi_value_index` bands the very
# same wrong number, so leaving it enabled would surface the defect through
# the back door, phrased as a reassuring word.
DISABLED_BY_DEFAULT = frozenset({"aqi_value"})


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: RadoffConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Sensors."""
    _LOGGER.debug("Radoff async_setup_entry")

    coordinator: RadoffCoordinator = config_entry.runtime_data

    sensors: list[RadoffSensor] = []
    for device in coordinator.data.devices:
        sensors.extend(_device_sensors(coordinator, device))

    # Create the sensors.
    async_add_entities(sensors)


def _device_sensors(
    coordinator: RadoffCoordinator, device: RadoffDevice
) -> Iterator["RadoffSensor"]:
    """
    Yield every entity of one device, driven by its type's schema (card M-04).

    The order of business is the schema's, not the payload's: the measures
    `/analytics/measures-ranges` declares for this device *type*, already
    sorted by `pos` (`schema.py::build_specs`). Two consequences, both
    intended:

    1. A declared measure absent from this poll still gets its entity. Card
       M-06 changed what that entity shows: it stays *available* as long as
       the device reports itself connected, and shows the last value it
       knew (restored across restarts, see `RadoffSensor`) or `unknown` if
       it never knew one - where before M-06 it was `unavailable`. A device
       that is briefly quiet therefore keeps both a stable set of entities
       and a continuous history.
    2. A device whose *instance* has a measure switched off in its
       `deviceConfig` gets an entity that is always empty. That is the known
       cost of a schema served per type (T-08 D-12, out of scope for this
       card): there is no per-device schema endpoint yet, and when there is,
       the filter belongs right here.

       M-06 makes the cost concrete and permanent, which is worth stating
       plainly rather than leaving for someone to discover: from inside
       this integration a switched-off measure is indistinguishable from a
       device that has simply not sent that field yet, so whatever we show
       for "no value right now" is what these entities will show forever.
       That is `unknown`, on an available entity - never a value, since
       there is no last value to remember. An always-`unknown` entity is
       the honest rendering of "the type declares this measure and this
       unit does not produce it", and it is visible in the UI, which is
       what makes the eventual per-device filter easy to justify.

    Then, after them, any telemetry field the schema does *not* declare -
    read as a bare number with no unit, no device class and no qualitative
    band. `radon_status` is the field this exists for: T-08 D-10 is still
    open, so the enumeration behind its value is unknown, and the honest
    thing is to publish the raw state and say so rather than invent labels
    for it. (It arrives only if it is numeric at all: `_build_readings`
    keeps numeric telemetry, and the swagger declares `radon_status` a
    string while the sample payload shows `2` - the other half of D-10.)
    """
    specs = coordinator.schemas.get(device.device_type) or {}

    if not specs:
        # No schema for this type: unknown to the catalogue, or a device
        # entry with no `type` at all. The device is kept and so is its
        # data - see `coordinator.py::async_load_schemas`, which already
        # logged the WARNING naming the type and the valid ones.
        _LOGGER.debug(
            "No measurement schema for device %s (type '%s'): building its "
            "entities from telemetry alone",
            device.serial_number,
            device.device_type,
        )

    for spec in specs.values():
        yield RadoffSensor(coordinator=coordinator, device=device, spec=spec)

        if coordinator.data.generate_index and spec.ranges:
            yield RadoffSensor(
                coordinator=coordinator, device=device, spec=spec, is_index=True
            )

    for field in device.readings:
        if field in specs:
            continue
        _LOGGER.debug(
            "Telemetry field %s of device %s is not declared by the schema "
            "of type '%s': exposed as a raw value, without unit or bands",
            field,
            device.serial_number,
            device.device_type,
        )
        yield RadoffSensor(
            coordinator=coordinator, device=device, spec=None, field=field
        )


class RadoffSensor(RadoffEntity, RestoreSensor):
    """
    A sensor representing a Radoff reading (raw value or qualitative index).

    `device_info`, `unique_id`, `available` and the coordinator update
    handling all now live in `RadoffEntity` (card S-07): this class only adds
    the sensor-specific value/unit/state-class semantics and the "-index"
    variant on top.

    Card M-04 makes it schema-driven. Everything it used to read from a
    table in this module - the unit, the device class, the thresholds, the
    set of qualitative states - now comes from the `MeasureSpec` the API
    served for this device's type, and the only thing left hardcoded is the
    mapping between the API's vocabulary and Home Assistant's
    (`schema.py::HA_UNITS`, `schema.py::DEVICE_CLASSES`), which is a
    translation, not a table of values.

    `spec` is `None` for a telemetry field the schema does not describe (see
    `_device_sensors`): a bare number, named after its own field.

    Card M-06 gives this class one piece of state, and it is the deliberate
    exception to `RadoffEntity`'s "nothing is cached" rule (see that
    module's docstring): the last value this entity published, with the
    timestamp that came with it. It exists because availability and the
    presence of a value came apart. A connected device with
    `telemetry: null` now has available entities - the API's device list
    looks at a 6-hour window and does not widen it on a miss (T-02 D-16),
    so silence is not absence - and an available entity has to show
    something. Decided with Piero: it shows the last known value rather
    than `unknown`, so history stays continuous and automations that read
    the state keep reading a number across a gap.

    The cost is real and is not hidden: a value can be arbitrarily old
    while the state looks current, and radon - slower than its siblings by
    design (T-08 D-15) - is where that will show first. The mitigation is
    the `last_measured_at` attribute, which always carries the timestamp of
    the value actually being shown and never borrows a fresher one from
    another field (see `measured_at` below). It is an attribute, so a
    threshold automation will not see it; that is the trade this card
    accepted knowingly, and the reason the alternative (`unknown` on every
    gap) is written down here rather than only in the card.

    One seam is left, and it is worth naming rather than discovering. A
    measure *is* named through `translation_key`, so a measure the backend
    adds after this release gets an entity whose translation does not exist
    yet: Home Assistant logs that and leaves the entity unnamed (it falls
    back to the device's own name). It cannot be papered over from here -
    setting `_attr_name` as a fallback would override the translation for
    every measure, not just the missing one - and it is caught early on
    purpose instead: `test_translations.py` fails the moment a schema
    fixture declares a measure the three translation files do not cover.
    """

    def __init__(
        self,
        coordinator: RadoffCoordinator,
        device: RadoffDevice,
        spec: MeasureSpec | None,
        field: str | None = None,
        *,
        is_index: bool = False,
    ) -> None:
        """Initialize the sensor."""
        name = spec.name if spec is not None else field
        if name is None:
            msg = "RadoffSensor needs either a MeasureSpec or a field name"
            raise ValueError(msg)

        super().__init__(coordinator, device, name)
        self._spec = spec
        self._is_index = is_index

        # Card M-06: the last value published by this entity and the
        # timestamp it arrived with. Seeded from the entity's own restored
        # state in `async_added_to_hass`, refreshed on every poll that
        # carries a reading, and read by `native_value` on every poll that
        # does not.
        self._last_native_value: int | float | str | None = None
        self._last_measured_at: datetime | None = None

        if spec is None:
            # Nothing describes this field, so there is nothing to translate
            # it with: a translation key with no entry behind it would leave
            # the entity nameless. The field name, spaced out, is a worse
            # label than a real translation and a much better one than none.
            self._attr_translation_key = None
            self._attr_name = name.replace("_", " ").capitalize()
        else:
            self._attr_translation_key = f"{name}_index" if is_index else name

        if is_index:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(spec.statuses) if spec is not None else []
        elif spec is not None:
            self._attr_device_class = spec.device_class

        if name in DISABLED_BY_DEFAULT:
            self._attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self) -> None:
        """
        Register with the coordinator, then recover the last value (card M-06).

        `super()` is what wires the coordinator listener (`CoordinatorEntity`)
        and the restore machinery (`RestoreSensor`); both run because every
        class in the chain calls it in turn.

        Order matters on the two lines that follow. The stored value is
        recovered first and the current poll is remembered second, so a
        restart that lands on a device already reporting a value keeps the
        fresh one and only an entity with nothing in this poll falls back to
        what it had before the restart. Restoring after the poll would
        reinstate a value from the previous run over one just received.
        """
        await super().async_added_to_hass()
        await self._restore_last_value()
        self._remember_current_value()

    async def _restore_last_value(self) -> None:
        """
        Seed the value cache from this entity's own state before the restart.

        Card M-06. `RestoreSensor` gives back the typed native value, which
        is what the cache holds - a number for a measure, the qualitative
        string for an index - so nothing has to be re-derived or re-parsed
        into the right type. The timestamp comes from the restored *state's*
        attributes instead, because it is published as an attribute
        (`last_measured_at`) rather than as part of the sensor's stored
        data: recovering both is what keeps a restored value from looking
        like it was measured at the moment of the restart.

        Silently tolerant of everything a restore can be missing: a first
        run, a new entity, a purged recorder database. The cache simply
        stays empty and the entity shows `unknown` until a value arrives.
        """
        stored = await self.async_get_last_sensor_data()
        if (
            stored is not None
            and isinstance(stored.native_value, int | float | str)
            and not isinstance(stored.native_value, bool)
        ):
            self._last_native_value = stored.native_value

        last_state = await self.async_get_last_state()
        if last_state is None:
            return

        restored_measured_at = last_state.attributes.get("last_measured_at")
        if isinstance(restored_measured_at, str):
            self._last_measured_at = dt_util.parse_datetime(restored_measured_at)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Remember this poll's value, if it brought one, then write the state."""
        self._remember_current_value()
        super()._handle_coordinator_update()

    @callback
    def _remember_current_value(self) -> None:
        """
        Copy this poll's value into the cache, or leave the cache alone.

        Card M-06. Called before every state write rather than from inside
        `native_value`: a property that mutates the entity on read is a
        property that behaves differently depending on who looked at it,
        and this one is read by the state machine, by templates and by
        diagnostics.

        A poll with no reading for this entity is not an erasure - it is
        the case the cache exists for - so the previous value and its
        timestamp stay exactly as they are.
        """
        value = self._live_native_value()
        if value is None:
            return
        self._last_native_value = value
        self._last_measured_at = super().measured_at

    def _live_native_value(self) -> int | float | str | None:
        """
        Return the value this poll carries for this entity, or `None`.

        The reading-to-state conversion, with no memory in it: `None` means
        this poll has nothing for this entity, never "the value is
        unknown".

        Card M-03 removed the `normalize_fn` hook this used to apply: arch
        2.0 sends each value in the unit it declares, so the raw value is
        the value. Card M-04 confirms it from the other end - the schema
        carries no `scaleFactor`, deliberately (see `schema.py`).
        """
        reading = self._reading
        if reading is None:
            return None

        raw_val = reading.value
        val = int(raw_val) if isinstance(raw_val, int) else float(raw_val)

        if self._is_index:
            return None if self._spec is None else self._spec.status_for(val)
        return val

    @property
    def native_value(self) -> int | float | str | None:
        """
        Return this poll's value, or the last one this entity published.

        Card M-06, and the fallback is the card's decision rather than a
        defensive habit. Until M-06 a missing reading meant the entity was
        `unavailable` (S-07) and this return value was only there to avoid
        raising; now a connected device with no telemetry has available
        entities, because the API's device list looks at a 6-hour window
        and answers `telemetry: null` for anything quieter (T-02 D-16), and
        the question of what those entities show had to be answered.
        Decided with Piero: the last known value, restored across restarts,
        so that history stays continuous and automations reading the state
        do not have to special-case `unknown` on every gap.

        `None` - rendered as `unknown` by Home Assistant - is what is left
        when there is no last value at all: a brand new entity, or one for
        a measure the device's type declares and this particular unit never
        produces (T-08 D-12, see `_device_sensors`), which will show
        `unknown` for as long as that stays true.

        What this deliberately does not do is make the value look fresh:
        `measured_at` below reports the timestamp of the value being shown,
        not of the poll that failed to update it.
        """
        live = self._live_native_value()
        return live if live is not None else self._last_native_value

    @property
    def measured_at(self) -> datetime | None:
        """
        Return the timestamp of the value this entity is actually showing.

        Card M-06. With a reading in this poll it is that reading's
        timestamp, straight from the base class. Without one - the entity
        is showing a remembered value - it is the timestamp that value
        arrived with, which is the entire mitigation for showing an old
        value as current state: `last_measured_at` ages while the state
        does not, so a support case can see a three-day-old radon figure
        for what it is.

        Never borrows the device's `telemetry_timestamp` for a field this
        poll did not carry (the base class dropped that fallback for the
        same reason): on a device that reported other measures, that would
        stamp a fresh timestamp on a value that has not moved.
        """
        if self._reading is not None:
            return super().measured_at
        return self._last_measured_at

    @property
    def native_unit_of_measurement(self) -> str | None:
        """
        Return the unit the API declared for this measure, in Home Assistant terms.

        `None` for an index entity (an enum has no unit), for a field with no
        schema, and for a unit that has no Home Assistant equivalent - the
        AQI's empty string, tvoc's `V - Ix`, or a unit this integration has
        never seen, which `schema.py::resolve_ha_unit` has already warned
        about by the time this is read.
        """
        if self._is_index or self._spec is None:
            return None
        return None if self._spec.ha_unit is None else str(self._spec.ha_unit)

    @property
    def state_class(self) -> str | None:
        """
        Return state class.

        `measurement` for every numeric reading, including the ones with no
        device class (`radon_bqm3`, `aqi_value`, `ch4`, `tvoc`): the state
        class is what makes a value a measurement Home Assistant can record
        statistics for, and it is independent of whether a device class
        fits. An index entity is an enum and has none.
        """
        if self._is_index:
            return None
        return SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """
        Add the measure's own labels to the attributes the base class exposes.

        The base class contributes the four diagnostic fields of card M-06
        (`last_measured_at`, `connection_status`,
        `connection_status_updated_at`, `status`, plus
        `connection_status_stale` when it applies); these two are the
        sensor-specific half.

        `measure_label`/`measure_acronym` are what `/analytics/measures-
        ranges` calls this measure ("Volatile organic compounds", "TVOC").
        They are attributes rather than the entity name on purpose: the name
        stays translated through `translation_key`, in the user's own
        language, while these two say what the backend called it - which is
        what a support conversation needs when the two disagree.
        """
        attributes = super().extra_state_attributes or {}
        if self._spec is None:
            return attributes or None

        extra = {
            key: value
            for key, value in (
                ("measure_label", self._spec.label),
                ("measure_acronym", self._spec.acronym),
            )
            if value
        }
        merged = {**attributes, **extra}
        return merged or None

    @property
    def unique_id(self) -> str:
        """Return unique id."""
        base = super().unique_id
        return base if not self._is_index else f"{base}-index"
