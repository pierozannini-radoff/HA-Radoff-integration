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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RadoffConfigEntry
from .api import RadoffDevice
from .api.models import ReadingKey
from .coordinator import RadoffCoordinator
from .entity import RadoffEntity, reading_key_slug

_LOGGER = logging.getLogger(__name__)

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


# Keyed by bare property name, independent of which bucket(s) a reading of
# that name exists in (card S-10 does not move this table - see
# `properties.py`'s docstring, "OUT OF SCOPE"). Today only DATA-bucket
# readings ever match a key here: `airqualityindex`, the only property the
# AGGREGATED bucket carries, is not one of them, so it never gets an "-index"
# sibling entity, in either bucket.
INDEX_MAPPING: dict[str, dict[str, Any]] = {
    "tvoc": {"index": _threshold_index((100, 200, 300, 400), FIVE_LEVELS)},
    "eco2": {"index": _threshold_index((500, 1000, 1500, 2000), FIVE_LEVELS)},
    "pm10": {"index": _threshold_index((20, 30, 40, 50), FIVE_LEVELS)},
    "pm25": {"index": _threshold_index((16, 21, 26, 32), FIVE_LEVELS)},
    "pm1": {"index": _threshold_index((6, 9, 12, 15), FIVE_LEVELS)},
    "internal_temperature": {"index": _threshold_index((18, 27), THREE_LEVELS)},
    "relative_humidity": {"index": _threshold_index((40, 60), THREE_LEVELS)},
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
        for reading_key, reading in device.readings.items():
            sensors.append(
                RadoffSensor(
                    reading_key=reading_key,
                    coordinator=coordinator,
                    device=device,
                    device_class=reading.device_class,
                    friendly_name=reading.friendly_name,
                    unit=reading.unit,
                    normalize_fn=reading.normalize_fn,
                    is_index=False,
                    index_fn=None,
                )
            )
            if coordinator.data.generate_index and reading.name in INDEX_MAPPING:
                index_obj = INDEX_MAPPING[reading.name]
                sensors.append(
                    RadoffSensor(
                        reading_key=reading_key,
                        coordinator=coordinator,
                        device=device,
                        device_class=None,
                        friendly_name=reading.friendly_name,
                        unit=None,
                        normalize_fn=reading.normalize_fn,
                        is_index=True,
                        index_fn=index_obj["index"],
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
        reading_key: ReadingKey,
        device: RadoffDevice,
        coordinator: RadoffCoordinator,
        device_class: SensorDeviceClass | None,
        friendly_name: str,
        normalize_fn: Callable[[Number], float | int] | None,
        unit: type[StrEnum] | str | None,
        index_fn: Callable[[Number], str] | None,
        *,
        is_index: bool | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device, reading_key)
        self.friendly_name = friendly_name
        self.unit = unit
        self._attr_device_class = device_class
        self._normalize_fn = normalize_fn
        self._is_index = is_index
        self._index_fn = index_fn

    @property
    def translation_key(self) -> str:
        """
        Return the translation key to translate the entity's name and states.

        Built from `reading_key_slug()` (card S-10), the same helper
        `RadoffEntity.unique_id` uses: a DATA-bucket reading resolves to
        exactly the bare property name it always has (e.g. `"tvoc"` /
        `"tvoc_index"`), and any other bucket gets its own suffixed slug
        (e.g. `"airqualityindex_average"`) so it never collides with, or
        shadows, the DATA-bucket entity for the same property.
        """
        slug = reading_key_slug(self.reading_key)
        return slug if not self._is_index else f"{slug}_index"

    @property
    def native_value(self) -> int | float | str | None:
        """
        Return the state of the entity.

        `None` here is only a defensive fallback (S-06, C2 origin): a missing
        or stale reading already makes the entity `unavailable` via
        `RadoffEntity.available` (S-07), so HA does not rely on this return
        value to hide a bad sample any more - it just avoids ever raising.
        """
        reading = self._reading
        if reading is None:
            return None

        if self._normalize_fn is not None:
            val = float(self._normalize_fn(reading.value))
        else:
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
