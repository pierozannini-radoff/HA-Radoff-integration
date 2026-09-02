"""
Domain dataclasses for the Radoff API.

`RadoffSensor` and `Device` (S-06b move from the former `api.py`) are renamed
here to `Reading` and `RadoffDevice` (card S-07), matching the target
architecture in `architettura-target-sprint-m.md` §3: this is the model that
`entity.py` is built against. Field-for-field the same two dataclasses, plus
the two additions this card introduces:

- `Reading.measured_at`: when the sample was taken, if the API ever exposes
  it. T-02 CONFIRMED (probe run 2026-09-02) that it does not today - `data`/
  `aggregatedData`/`recalculatedData` objects carry only `propertyName` and
  `value`/`aggregationValue` - so this stays `None` by design, not "pending".
  See `api/client.py::_MEASURED_AT_KEYS`.
- `RadoffDevice.last_data_received_at`: the same probe found this one level
  up, as a sibling field of the same `GET /data/devices/{id}` response -
  "when this device last reported anything", device-level rather than
  per-sample. `RadoffEntity.available` uses it as the freshness signal in
  place of `Reading.measured_at` when the latter is `None`, which today is
  always. Format and timezone CONFIRMED by a second probe run (2026-09-02):
  ISO-8601 with millisecond precision and an explicit trailing "Z"
  (`"2026-09-02T14:26:40.147Z"`), i.e. already UTC - see
  `api/client.py::_parse_timestamp`.
- `RadoffDevice.stale`: whether the last per-device fetch for this device
  failed. Always `False` for now - card S-13 is what actually sets it; S-07
  only consumes the flag in `RadoffEntity.available`.

The composite `(bucket, property_name)` reading key described in the
architecture doc for the `data` vs `aggregatedData` collision (C4) is
deliberately NOT introduced here: `readings` stays keyed by plain property
name, same as the `sensors` dict it replaces. That key change is card S-08's
job, not this one - see the S-07 implementation notes for why.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from numbers import Number

from homeassistant.components.sensor import SensorDeviceClass


@dataclass
class Reading:
    """A single sample for one property of one device, with its own age."""

    name: str
    value: Number
    device_class: SensorDeviceClass
    friendly_name: str
    unit: type[StrEnum] | str | None
    normalize_fn: Callable[[Number], float | int] | None
    measured_at: datetime | None = None


@dataclass
class RadoffDevice:
    """API device."""

    device_id: str
    device_serial: str
    device_type: str
    name: str
    readings: dict[str, Reading]
    stale: bool = False
    last_data_received_at: datetime | None = None
