"""Class which represent the Radoff entity."""

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
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Device
from .const import DOMAIN
from .coordinator import RadoffCoordinator

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
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Sensors."""
    _LOGGER.debug("Radoff async_setup_entry")

    coordinator: RadoffCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ].coordinator

    sensors = []
    for device in coordinator.data.devices:
        for sensor in device.sensors.values():
            sensors.append(
                RadoffSensor(
                    sensor_key=sensor.name,
                    coordinator_context=coordinator,
                    device=device,
                    device_class=sensor.device_class,
                    friendly_name=sensor.friendly_name,
                    unit=sensor.unit,
                    normalize_fn=sensor.normalize_fn,
                    is_index=False,
                    index_fn=None,
                )
            )
            if coordinator.data.generate_index and sensor.name in INDEX_MAPPING:
                index_obj = INDEX_MAPPING[sensor.name]
                sensors.append(
                    RadoffSensor(
                        sensor_key=sensor.name,
                        coordinator_context=coordinator,
                        device=device,
                        device_class=None,
                        friendly_name=sensor.friendly_name,
                        unit=None,
                        normalize_fn=sensor.normalize_fn,
                        is_index=True,
                        index_fn=index_obj["index"],
                    )
                )

    # Create the sensors.
    async_add_entities(sensors)


class RadoffSensor(CoordinatorEntity, SensorEntity):
    """A sensor representing the radoff sensor entity."""

    _attr_has_entity_name = True

    def __init__(  # noqa: PLR0913
        self,
        sensor_key: str,
        device: Device,
        coordinator_context: RadoffCoordinator,
        device_class: SensorDeviceClass | None,
        friendly_name: str,
        normalize_fn: Callable[[Number], float | int],
        unit: type[StrEnum] | str | None,
        index_fn: Callable[[Number], str] | None,
        *,
        is_index: bool | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator_context, context=sensor_key)
        self.device = device
        self.sensor_key = sensor_key
        self.friendly_name = friendly_name
        self.unit = unit
        self._attr_device_class = device_class
        self._normalize_fn = normalize_fn
        self.coordinator_context = coordinator_context
        self._is_index = is_index
        self._index_fn = index_fn

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update sensor with latest data from coordinator."""
        # Metadata only: sensor key and device id (UUID, allowed at DEBUG per
        # S-03 policy), never the Device repr - it embeds every RadoffSensor
        # value for this device (see card S-03, sensor.py finding).
        _LOGGER.debug(
            "Refreshing sensor %s for device %s",
            self.sensor_key,
            self.device.device_id,
        )
        # `get_device_by_id` can return None (device gone from this poll's
        # payload: offline, transient error, filtered out - see card S-06,
        # C1). Only replace `self.device` when a match is found, so the
        # entity keeps serving its last known device rather than crashing
        # with an AttributeError on the next `native_value`/`device_info`
        # access. Proper availability handling is S-07; this is the minimal
        # guard for the hot path.
        device = self.coordinator_context.get_device_by_id(
            self.device.device_type, self.device.device_id
        )
        if device is not None:
            self.device = device
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_serial)},
            name=self.device.name,
            manufacturer="Radoff",
            model=self.device.device_type,
        )

    @property
    def translation_key(self) -> str:
        """Return the translation key to translate the entity's name and states."""
        if not self._is_index:
            return self.sensor_key
        return f"{self.sensor_key}_index"

    @property
    def native_value(self) -> int | float | None:
        """Return the state of the entity."""
        # `.get()` instead of `[...]` (see card S-06, C2): a single poll can
        # legitimately be missing a property (e.g. a physical sensor hasn't
        # produced a valid sample yet). Returning None here makes the entity
        # report no value for this cycle instead of raising KeyError; full
        # availability semantics land in S-07.
        sensor = self.device.sensors.get(self.sensor_key)
        if sensor is None:
            return None

        if self._normalize_fn is not None:
            val = float(self._normalize_fn(sensor.value))
        else:
            raw_val = sensor.value
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
        sensor_name = self.device.sensors[self.sensor_key].name
        unique_id = f"{DOMAIN}-{self.device.device_id}-{sensor_name}"
        if not self._is_index:
            return unique_id
        return f"{unique_id}-index"
