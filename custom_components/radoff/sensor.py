"""Class which represent the Radoff sensor entity."""

import logging
from collections.abc import Iterator
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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

    1. A declared measure absent from this poll still gets its entity,
       `unavailable` until a value arrives (`RadoffEntity.available`), which
       is what makes a device that is briefly quiet keep a stable set of
       entities instead of losing and regaining them.
    2. A device whose *instance* has a measure switched off in its
       `deviceConfig` gets an entity that is always empty. That is the known
       cost of a schema served per type (T-08 D-12, out of scope for this
       card): there is no per-device schema endpoint yet, and when there is,
       the filter belongs right here.

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


class RadoffSensor(RadoffEntity, SensorEntity):
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

    @property
    def native_value(self) -> int | float | str | None:
        """
        Return the state of the entity.

        `None` here is only a defensive fallback (S-06, C2 origin): a missing
        or stale reading already makes the entity `unavailable` via
        `RadoffEntity.available` (S-07), so HA does not rely on this return
        value to hide a bad sample any more - it just avoids ever raising.

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
        Expose the reading's freshness, plus the label the API gave the measure.

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
