"""
Domain dataclasses for the Radoff API.

Pure move from the former `api.py` (see S-06b): `RadoffSensor` and `Device`
unchanged, field for field. `APIData` is not here on purpose - it lives in
coordinator.py, changes shape in S-07/S-13, and moves to this module then,
not as part of this move.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from numbers import Number

from homeassistant.components.sensor import SensorDeviceClass


@dataclass
class RadoffSensor:
    """Dataclass to store the entity data."""

    name: str
    value: Number
    device_class: SensorDeviceClass
    friendly_name: str
    unit: type[StrEnum] | str | None
    normalize_fn: Callable[[Number], float | int]


@dataclass
class Device:
    """API device."""

    device_id: str
    device_serial: str
    device_type: str
    name: str
    sensors: dict[str, RadoffSensor]
