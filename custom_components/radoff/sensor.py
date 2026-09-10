"""Class which represent the Radoff sensor entity."""

import logging
from collections.abc import Callable
from enum import StrEnum
from numbers import Number
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadoffConfigEntry
from .api import RadoffDevice
from .coordinator import RadoffCoordinator
from .entity import RadoffEntity

_LOGGER = logging.getLogger(__name__)

# Provisional field metadata, replacing `properties.py::MAPPING` which card
# M-03 deletes. THIS TABLE IS TEMPORARY BY DESIGN: card M-04 replaces it
# with the per-device-type schema the API serves on
# `/analytics/measures-ranges`, which is the authoritative source for units,
# labels and thresholds. It lives here, next to the platform that consumes
# it, precisely so that M-04 has one call site to redirect.
#
# Two things are deliberately different from the table it replaces:
#
# 1. There is no `normalize_fn`, and in particular no scaling factor on
#    `internal_temperature` (the one S-10 carried, finding C11). Arch 2.0
#    delivers values in the unit it declares - M-01 observed
#    `internal_temperature: 26.0583` and `pressure: 100488.0` on a real
#    device (V7/V8) - so scaling them here would be inventing a
#    conversion.
# 2. The keys are arch 2.0 field names, taken from the flat `telemetry`
#    block. Most carry over unchanged from 1.x; the AQI does not - the
#    aggregated `airqualityindex` of 1.x is `telemetry.aqi_value` now, a
#    different name for the value the same entity has always shown. Its
#    `unique_id` therefore changes twice over in this card (serial *and*
#    field name); re-keying the entity users already have is M-07.
#
# A telemetry field absent from this table still becomes a `Reading` (see
# `api/client.py::_build_readings`) and still reaches diagnostics - it just
# gets no entity, because there is nothing yet to label it with.
_PROVISIONAL_FIELDS: dict[str, dict[str, Any]] = {
    "tvoc": {
        "device_class": SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
        "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    },
    "eco2": {
        "device_class": SensorDeviceClass.CO2,
        "unit": CONCENTRATION_PARTS_PER_MILLION,
    },
    "pm10": {
        "device_class": SensorDeviceClass.PM10,
        "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    },
    "pm25": {
        "device_class": SensorDeviceClass.PM25,
        "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    },
    "pm1": {
        "device_class": SensorDeviceClass.PM1,
        "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    },
    "internal_temperature": {
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
    },
    "relative_humidity": {
        "device_class": SensorDeviceClass.HUMIDITY,
        "unit": PERCENTAGE,
    },
    "pressure": {
        "device_class": SensorDeviceClass.PRESSURE,
        "unit": UnitOfPressure.PA,
    },
    # The AQI runs on a 0-5 scale (confirmed by Piero, 2026-09-10), not on
    # the 0-500 one `SensorDeviceClass.AQI` will make most people assume:
    # the real device M-01 sampled reports 1.15, which on an EPA-style
    # scale would read as "essentially perfect air" and here means
    # something quite different. The device class is kept anyway - it is
    # the only one Home Assistant has for an air quality index, it forces
    # no unit and applies no conversion - but M-04 has a decision to make
    # with the schema in hand: a 0-5 index may belong with the qualitative
    # `_index` entities below rather than as a bare number.
    "aqi_value": {
        "device_class": SensorDeviceClass.AQI,
        "unit": None,
    },
}

FIVE_LEVELS = ("excellent", "good", "medium", "poor", "terrible")
THREE_LEVELS = ("excellent", "good", "terrible")


def _threshold_index(
    thresholds: tuple[float, ...], levels: tuple[str, ...]
) -> Callable[[Number], str]:
    """
    Build the index function of a property from its thresholds.

    The first threshold the value is lower than or equal to selects the level of
    the same position. `levels` holds exactly one entry more than `thresholds`:
    the last one is returned when no threshold matches.
    """

    def _index(val: Number) -> str:
        for threshold, level in zip(thresholds, levels, strict=False):
            if val <= threshold:
                return level
        return levels[-1]

    return _index


# Thresholds named so tests can probe boundary values from the same source
# the index functions below are built from, instead of duplicating them.
_TVOC_THRESHOLDS = (100.0, 200.0, 300.0, 400.0)
_ECO2_THRESHOLDS = (500.0, 1000.0, 1500.0, 2000.0)
_PM10_THRESHOLDS = (20.0, 30.0, 40.0, 50.0)
_PM25_THRESHOLDS = (16.0, 21.0, 26.0, 32.0)
_PM1_THRESHOLDS = (6.0, 9.0, 12.0, 15.0)
_TEMPERATURE_THRESHOLDS = (18.0, 27.0)
_HUMIDITY_THRESHOLDS = (40.0, 60.0)

# Keyed by telemetry field name, like `_PROVISIONAL_FIELDS` above - the two
# tables happen to agree on their key spelling in arch 2.0 because the
# fields kept their 1.x names, `aqi_value` aside, which has no qualitative
# sibling here. These thresholds are hardcoded and should not be: moving
# them onto the schema the API serves is card M-04, together with the units
# above.
INDEX_MAPPING: dict[str, dict[str, Any]] = {
    "tvoc": {
        "index": _threshold_index(_TVOC_THRESHOLDS, FIVE_LEVELS),
        "states": FIVE_LEVELS,
        "thresholds": _TVOC_THRESHOLDS,
    },
    "eco2": {
        "index": _threshold_index(_ECO2_THRESHOLDS, FIVE_LEVELS),
        "states": FIVE_LEVELS,
        "thresholds": _ECO2_THRESHOLDS,
    },
    "pm10": {
        "index": _threshold_index(_PM10_THRESHOLDS, FIVE_LEVELS),
        "states": FIVE_LEVELS,
        "thresholds": _PM10_THRESHOLDS,
    },
    "pm25": {
        "index": _threshold_index(_PM25_THRESHOLDS, FIVE_LEVELS),
        "states": FIVE_LEVELS,
        "thresholds": _PM25_THRESHOLDS,
    },
    "pm1": {
        "index": _threshold_index(_PM1_THRESHOLDS, FIVE_LEVELS),
        "states": FIVE_LEVELS,
        "thresholds": _PM1_THRESHOLDS,
    },
    "internal_temperature": {
        "index": _threshold_index(_TEMPERATURE_THRESHOLDS, THREE_LEVELS),
        "states": THREE_LEVELS,
        "thresholds": _TEMPERATURE_THRESHOLDS,
    },
    "relative_humidity": {
        "index": _threshold_index(_HUMIDITY_THRESHOLDS, THREE_LEVELS),
        "states": THREE_LEVELS,
        "thresholds": _HUMIDITY_THRESHOLDS,
    },
}


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: RadoffConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Sensors."""
    _LOGGER.debug("Radoff async_setup_entry")

    coordinator: RadoffCoordinator = config_entry.runtime_data

    sensors = []
    for device in coordinator.data.devices:
        for field in device.readings:
            descriptor = _PROVISIONAL_FIELDS.get(field)
            if descriptor is None:
                # A telemetry field this version cannot label yet - see
                # `_PROVISIONAL_FIELDS`. Logged so a field the backend adds
                # is visible rather than silently dropped, at DEBUG because
                # on a device type outside this release's scope it would
                # otherwise be a line per field per poll.
                _LOGGER.debug(
                    "No provisional descriptor for telemetry field %s on "
                    "device %s: no entity created (card M-04 replaces this "
                    "table with the API's own schema)",
                    field,
                    device.serial_number,
                )
                continue

            sensors.append(
                RadoffSensor(
                    field=field,
                    coordinator=coordinator,
                    device=device,
                    device_class=descriptor["device_class"],
                    unit=descriptor["unit"],
                    is_index=False,
                    index_fn=None,
                    index_states=None,
                )
            )
            if coordinator.data.generate_index and field in INDEX_MAPPING:
                index_obj = INDEX_MAPPING[field]
                sensors.append(
                    RadoffSensor(
                        field=field,
                        coordinator=coordinator,
                        device=device,
                        device_class=SensorDeviceClass.ENUM,
                        unit=None,
                        is_index=True,
                        index_fn=index_obj["index"],
                        index_states=index_obj["states"],
                    )
                )

    # Create the sensors.
    async_add_entities(sensors)


class RadoffSensor(RadoffEntity, SensorEntity):
    """
    A sensor representing a Radoff reading (raw value or qualitative index).

    `device_info`, `unique_id`, `available` and the coordinator update
    handling all now live in `RadoffEntity` (card S-07): this class only adds
    the sensor-specific value/unit/state-class semantics and the "-index"
    variant on top.
    """

    def __init__(  # noqa: PLR0913
        self,
        field: str,
        device: RadoffDevice,
        coordinator: RadoffCoordinator,
        device_class: SensorDeviceClass | None,
        unit: type[StrEnum] | str | None,
        index_fn: Callable[[Number], str] | None,
        index_states: tuple[str, ...] | None,
        *,
        is_index: bool | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device, field)
        self.unit = unit
        self._attr_device_class = device_class
        self._is_index = is_index
        self._index_fn = index_fn
        if index_states is not None:
            self._attr_options = list(index_states)

    @property
    def translation_key(self) -> str:
        """
        Return the translation key to translate the entity's name and states.

        The telemetry field name, or `{field}_index` for the qualitative
        sibling. Card M-03: `reading_key_slug()` is gone with the buckets it
        existed to flatten, so the key is the field itself - and the AQI's
        key moves with the payload, from `airqualityindex_average` to
        `aqi_value`.
        """
        return self.field if not self._is_index else f"{self.field}_index"

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
        the value (see `_PROVISIONAL_FIELDS`).
        """
        reading = self._reading
        if reading is None:
            return None

        raw_val = reading.value
        val = int(raw_val) if isinstance(raw_val, int) else float(raw_val)

        if self._is_index:
            return self._index_fn(val)
        return val

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return unit."""
        return None if self.unit is None else str(self.unit)

    @property
    def state_class(self) -> str | None:
        """Return state class."""
        if self._is_index:
            return None
        return SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        """Return unique id."""
        base = super().unique_id
        return base if not self._is_index else f"{base}-index"
