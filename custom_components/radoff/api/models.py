"""
Domain dataclasses for the Radoff API.

`RadoffSensor` and `Device` (S-06b move from the former `api.py`) were
renamed here to `Reading` and `RadoffDevice` (card S-07), matching the
target architecture in `architettura-target-sprint-m.md` §3.

Card S-10 adds the composite reading key described in that same section and
resolves finding C4 with it:

- `Bucket`: which response bucket a reading was read from. Its two members'
  *values* are deliberately the exact JSON keys the Radoff API uses for
  those buckets (`"data"`, `"aggregatedData"`) rather than arbitrary names -
  a `StrEnum` member compares equal to, and hashes the same as, its string
  value, so `Bucket.DATA` can be used directly as a `dict` key against a
  payload keyed by plain strings (see `api/client.py::_get_data`) with no
  translation table in between, and `properties.py::MAPPING` can be keyed by
  `Bucket` the same way. `recalculatedData` - a third bucket the API exposes
  (see `s-07-implementazione.md`, "Scoperta collaterale") - deliberately has
  no member here yet: nothing in `properties.py::MAPPING` describes it, so
  it is silently skipped by `_get_data`'s "iterate `MAPPING`, not the raw
  payload" loop, same as before this card. Mapping it is separate work.
- `ReadingKey = tuple[Bucket, str]`: `(bucket, property_name)`. Before this
  card, `RadoffDevice.readings` was keyed by the bare property name, so
  `data.airqualityindex` and `aggregatedData.airqualityindex` wrote the same
  dict key - the second one silently won, and which one that was depended on
  `MAPPING`'s iteration order (finding C4, high severity). The composite key
  lets both live in the same dict as two independent entries.
- `Reading.bucket`: which bucket this particular sample came from, so a
  `Reading` carries its own key component instead of the caller having to
  remember which `readings[...]` lookup it came out of.

The other two additions from S-07 are unchanged by this card:

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
  always. Format and timezone CONFIRMED (ISO-8601, millisecond precision,
  explicit trailing "Z", i.e. already UTC) - see `api/client.py::_parse_timestamp`.
- `RadoffDevice.stale`: whether the last per-device fetch for this device
  failed. Always `False` for now - card S-13 is what actually sets it; S-07
  only consumes the flag in `RadoffEntity.available`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from numbers import Number

from homeassistant.components.sensor import SensorDeviceClass


class Bucket(StrEnum):
    """
    Which response bucket a reading was read from.

    Member values are the literal JSON bucket keys the Radoff API uses
    (`GET /data/devices/{id}` -> `data.data`, `data.aggregatedData`), not
    arbitrary labels - see this module's docstring for why that equivalence
    is deliberate.
    """

    DATA = "data"
    AGGREGATED = "aggregatedData"


# (bucket, property_name): the composite key that replaces the bare
# property-name string `RadoffDevice.readings` used to be keyed by. Resolves
# C4 - see this module's docstring.
ReadingKey = tuple[Bucket, str]


@dataclass
class Reading:
    """A single sample for one (bucket, property) pair of one device."""

    name: str
    bucket: Bucket
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
    readings: dict[ReadingKey, Reading]
    stale: bool = False
    last_data_received_at: datetime | None = None
