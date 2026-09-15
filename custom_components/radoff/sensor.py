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

# Created but disabled: the AQI's temperature component is divided by 120 on
# a value already in °C, so the published index is systematically wrong.
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
    Yield every entity of one device, driven by its type's schema.

    The schema's order, not the payload's, so a declared measure absent from
    this poll still gets its entity - showing its last known value, or
    `unknown`. The schema is served per type, so a device with a measure
    switched off on the instance gets an entity that stays empty: from here
    that is indistinguishable from a field not sent yet.

    Then any telemetry field the schema does not declare, as a bare number
    with no unit, device class or bands.
    """
    specs = coordinator.schemas.get(device.device_type) or {}

    if not specs:
        # Unknown to the catalogue, or a device with no `type` at all. The
        # device and its data are kept; the warning is logged at load time.
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

    Unit, device class, thresholds and qualitative states all come from the
    `MeasureSpec` the API served for this device's type. `spec` is `None` for
    a telemetry field the schema does not describe: a bare number, named
    after its own field.

    This class keeps the one piece of state `RadoffEntity` refuses to: the
    last value published and the timestamp it arrived with. An available
    entity has to show something, and silence is not absence, so it shows the
    last known value rather than `unknown` and history stays continuous. The
    cost is a value that can be arbitrarily old while the state looks
    current; `last_measured_at` always carries the timestamp of the value
    actually shown.

    A measure is named through `translation_key`, so one the backend adds
    after this release arrives unnamed until its translation does.
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

        # The last value published, refreshed by every poll that carries a
        # reading and read by every poll that does not.
        self._last_native_value: int | float | str | None = None
        self._last_measured_at: datetime | None = None

        if spec is None:
            # A translation key with nothing behind it leaves the entity
            # nameless, so the spaced-out field name stands in.
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
        Register with the coordinator, then recover the last value.

        The order of the last two lines matters: restoring after remembering
        would reinstate a value from the previous run over one just received.
        """
        await super().async_added_to_hass()
        await self._restore_last_value()
        self._remember_current_value()

    async def _restore_last_value(self) -> None:
        """
        Seed the value cache from this entity's own state before the restart.

        The value comes back typed from `RestoreSensor`; the timestamp comes
        from the restored state's attributes, where it is published, so a
        restored value does not look measured at the moment of the restart.
        Everything a restore can be missing leaves the cache empty.
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

        Called before every state write rather than from `native_value`,
        which is read by the state machine, templates and diagnostics alike
        and must not mutate the entity. A poll with no reading is the case
        the cache exists for, not an erasure.
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
        this poll has nothing for this entity, never "the value is unknown".
        No scaling is applied - values arrive in the unit they declare.
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

        The fallback keeps history continuous across a quiet device, so
        automations do not have to special-case `unknown` on every gap.
        `None` is left only when there is no last value at all. It does not
        make the value look fresh: `measured_at` reports the timestamp of the
        value being shown.
        """
        live = self._live_native_value()
        return live if live is not None else self._last_native_value

    @property
    def measured_at(self) -> datetime | None:
        """
        Return the timestamp of the value this entity is actually showing.

        A remembered value keeps the timestamp it arrived with, so the
        attribute ages while the state does not and an old radon figure is
        visible for what it is. The device's own telemetry timestamp is never
        borrowed: it would stamp a fresh time on a value that has not moved.
        """
        if self._reading is not None:
            return super().measured_at
        return self._last_measured_at

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit the API declared, in Home Assistant's own terms."""
        if self._is_index or self._spec is None:
            return None
        return None if self._spec.ha_unit is None else str(self._spec.ha_unit)

    @property
    def state_class(self) -> str | None:
        """Return the state class: every numeric reading is a measurement."""
        if self._is_index:
            return None
        return SensorStateClass.MEASUREMENT

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """
        Add the measure's own labels to the attributes the base class exposes.

        `measure_label` and `measure_acronym` are what the API calls this
        measure. They are attributes, not the entity name: the name stays
        translated in the user's language, while these say what the backend
        called it - which is what a support case needs when the two disagree.
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
