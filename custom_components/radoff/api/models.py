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
  failed. Used to always be `False` unconditionally - card S-13 is what
  actually sets it now (see `api/client.py::get_devices` and
  `coordinator.py::RadoffCoordinator._merge_device_errors`); S-07 already
  consumed the flag correctly in `RadoffEntity.available` before S-13
  existed to produce it.

Card S-13 adds `DeviceFetchError`, described below.
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


@dataclass
class DeviceFetchError:
    """
    One device's identity, paired with why its per-device fetch failed (S-13).

    `api/client.py::get_devices()` builds one of these, instead of raising,
    whenever the `search` call itself succeeded (so the device's identity -
    id, serial, type, name - is known from that response) but the follow-up
    per-device `GET /data/devices/{id}` failed with an isolatable error (see
    `get_devices()`'s own docstring for exactly which exceptions qualify).

    `coordinator.py::RadoffCoordinator._merge_device_errors` is what turns
    this into a `RadoffDevice` the rest of the integration can use: it looks
    up the previous poll's device with the same `(device_type, device_id)`
    and, if found, keeps its `readings`/`last_data_received_at` and only
    flips `stale` to `True`; if this device has no previous data at all
    (e.g. its very first poll already failed), it is still included, with
    empty `readings` and `stale=True`, rather than silently dropped - the
    card's own "COSA FARE" step 2 asks for exactly this fallback.

    `error` is a short, human-readable description of the failure (built
    from `str(exception)`), used only for the DEBUG log line the card asks
    for (step 5) - it carries no meaning to `RadoffDevice`/`RadoffEntity`,
    which only ever see the resulting `stale` flag.
    """

    device_id: str
    device_serial: str
    device_type: str
    name: str
    error: str
