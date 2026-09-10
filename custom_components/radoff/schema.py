"""
The per-device-type measurement schema, as the API itself serves it (card M-04).

This module is the answer to the question every previous card had to work
around: where do units, labels and thresholds come from? Until now they
came from tables in the code - the field table of `properties.py` (deleted
by M-03) and, in `sensor.py`, the index table with its threshold tuples
(deleted by this one) - inherited from the original integration with no
known origin, and therefore silently wrong the moment the backend changed
its mind. Their names are left out deliberately: this card's acceptance
criterion is that grepping for them finds nothing.

In arch 2.0 the same information is served:
`GET /analytics/measures-ranges?device_type=<type>` returns, for every
measure of that type, `label`, `unit`, `dataType`, `ranges`, `acronym` and
`pos`. One call per *type*, not per device, made at setup and cached
(`coordinator.py::async_load_schemas`). T-02 verified the thresholds served
coincide with the ones we had, so this migration does not change a single
displayed value - it changes where the values come from.

Three properties of that response are load-bearing, and all three are easy
to get wrong (confirmed by the backend, T-08):

1. **`pos` is sparse.** It is an ordering key, never an index. On a `now`
   the values are 1, 3, 6, 10, 11, 12 - the gaps are the measures that type
   does not have. `build_specs` sorts by it and nothing indexes with it;
   `tests/test_schema.py` pins exactly that on the real fixtures.
2. **The band order is `excellent -> high -> good -> poor -> terrible`**,
   with `high` between `excellent` and `good`. That is intentional and it is
   *not* the `excellent -> good -> medium -> poor -> terrible` this
   integration used to hardcode. The served order is the order, both for the
   threshold walk and for an enum entity's `options`.
3. **`scaleFactor` is deliberately absent.** Values arrive already scaled
   and the client must apply nothing. There is no scaling hook in this
   module, and adding one would be inventing a conversion (the same
   reasoning M-03 applied when it dropped `normalize_fn`).

Two things this module deliberately does not do, both out of scope here:

- **Per-device overrides (T-08 D-12).** `measures-ranges` answers per
  *type*, so an individual device with one measure switched off in its
  `deviceConfig` still gets that measure's entity, permanently empty. The
  backend has no per-device schema endpoint yet; when it answers D-12, that
  filter belongs here, between the type schema and the device's entities.
- **A hardcoded fallback.** If `measures-ranges` does not answer there is no
  built-in table to fall back on, by decision: the endpoint is available on
  dev, and a fallback table would be the exact thing this card exists to
  delete, kept alive under another name.
"""

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
)

_LOGGER = logging.getLogger(__name__)

# Unit string served by the API -> unit Home Assistant understands.
#
# Home Assistant's units are a closed enumeration and the API's are another
# one; this is the whole of the translation between them, and M-01 captured
# every value the second one currently holds (`measures_ranges__all.json`,
# eight distinct strings). Two of them have no Home Assistant counterpart on
# purpose and map to `None`:
#
# - `""` (the AQI): a dimensionless index. Home Assistant renders a sensor
#   with no unit exactly right, and inventing one would be worse than none.
# - `V - Ix` (tvoc): not a unit at all - see `T-02 D-07`. The source is
#   incoherent here (values around 0.13 against thresholds of 100-400) and
#   this card's decision is to serve what the API serves and flag it, not to
#   invent a scale. This is also why `tvoc` gets no device class below:
#   `SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS` would force a µg/m³ that
#   the reading is not in.
#
#   One consequence lands on someone else's plate, and is written here so
#   that card does not have to rediscover it. On an installation upgrading
#   from the released version, the tvoc entity had unit µg/m³ and device
#   class `volatile_organic_compounds`; after this card it has neither.
#   Home Assistant treats a unit change on an existing entity as a break in
#   its long-term statistics - the recorder keeps the old series and starts
#   a new one, and the user sees a gap plus a repair issue about it. That
#   only bites where the entity survives the upgrade, which is precisely
#   what **M-07** (re-keying the `unique_id`s of existing entities) decides:
#   if M-07 can re-key tvoc, it inherits this case and should handle the
#   statistics reset deliberately (`async_update_entity` clearing the old
#   unit, or a documented one-off gap) rather than let it surprise anyone.
#   The README says the same thing in the user's own words.
#
# `Bq/m³` passes through as the literal string: Home Assistant has no
# constant for it and no radon device class, and an arbitrary unit string on
# a sensor with no device class is perfectly valid.
#
# A unit *outside* this map is not an error (see `resolve_ha_unit`): the
# entity is created without a unit and a WARNING names the measure, because
# a new unit appearing in the response is news, not a reason to leave the
# user without an integration.
HA_UNITS: dict[str, str | None] = {
    "Pa": UnitOfPressure.PA,
    "%": PERCENTAGE,
    "ppm": CONCENTRATION_PARTS_PER_MILLION,
    "µg/m³": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    "°C": UnitOfTemperature.CELSIUS,
    "Bq/m³": "Bq/m³",
    "": None,
    "V - Ix": None,
}

# Measure name -> Home Assistant device class, where one actually fits.
#
# A device class is a promise about what the number means, and Home
# Assistant enforces it (it constrains the allowed units and drives unit
# conversion in the UI). So it is set only where the promise is true, and
# four measures deliberately have none:
#
# - `radon_bqm3`: Home Assistant has no radon device class.
# - `ch4`: no methane device class either (the gas classes it has are
#   CO, CO2, NO, NO2, N2O, O3, SO2 and the two VOC ones).
# - `tvoc`: see `HA_UNITS` - the served unit is not a VOC concentration.
# - `aqi_value`: `SensorDeviceClass.AQI` presupposes the EPA 0-500 scale,
#   while this index runs 1-5 (T-02 D-08). A 1.15 on an EPA scale reads as
#   "perfect air"; here it means something else entirely, and the device
#   class would make every dashboard and voice assistant say the wrong
#   thing. M-03 kept `AQI` provisionally with the schema not yet in hand -
#   this is the card that had the decision to make, and makes it.
#
# `pressure` is declared in Pa, which is what the API sends. Converting to
# hPa/mbar is the Home Assistant UI's job, not this integration's: with
# `SensorDeviceClass.PRESSURE` set, a user picks their preferred unit per
# entity and Home Assistant converts, which is strictly better than a
# conversion baked in here that nobody can undo.
DEVICE_CLASSES: dict[str, SensorDeviceClass] = {
    "internal_temperature": SensorDeviceClass.TEMPERATURE,
    "relative_humidity": SensorDeviceClass.HUMIDITY,
    "pressure": SensorDeviceClass.PRESSURE,
    "eco2": SensorDeviceClass.CO2,
    "pm1": SensorDeviceClass.PM1,
    "pm25": SensorDeviceClass.PM25,
    "pm10": SensorDeviceClass.PM10,
    "co": SensorDeviceClass.CO,
}

# Sorting key for a measure whose `pos` is missing or not an integer: after
# every measure that has one, then alphabetically (see `build_specs`).
_POS_LAST = (1, 0)
_POS_PRESENT = 0


@dataclass(frozen=True)
class Band:
    """
    One qualitative band of a measure: a status and the value it reaches up to.

    `upper_bound` is `None` on the last band of the list, which is
    open-ended ("everything above the previous bound"). The API expresses it
    by omitting `upperBound` rather than by sending a sentinel.
    """

    status: str
    upper_bound: float | None = None


@dataclass(frozen=True)
class MeasureSpec:
    """
    Everything the API declares about one measure of one device type.

    `unit` is the string the API sent, kept verbatim; `ha_unit` is that
    string resolved against `HA_UNITS` (`None` when there is no Home
    Assistant equivalent, or when the string was outside the map). Both are
    kept because they answer different questions: what the backend said, and
    what this integration can do with it.

    `ranges` holds the bands **in the order served**, which is the order the
    backend intends (see this module's docstring, point 2) - not sorted, not
    normalised, not reordered into the shape the old hardcoded tables had.
    """

    name: str
    label: str | None = None
    unit: str | None = None
    ha_unit: str | None = None
    data_type: str | None = None
    ranges: tuple[Band, ...] = ()
    pos: int | None = None
    acronym: str | None = None

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """Return the Home Assistant device class of this measure, if one fits."""
        return DEVICE_CLASSES.get(self.name)

    @property
    def statuses(self) -> tuple[str, ...]:
        """Return the band statuses in the order the API served them."""
        return tuple(band.status for band in self.ranges)

    def status_for(self, value: float) -> str | None:
        """
        Return the qualitative status `value` falls in, or `None` if unbanded.

        The first band whose `upper_bound` the value does not exceed wins,
        walking the list in served order; the last band, being open-ended,
        catches everything left. This is the same "<= threshold" semantic the
        deleted `sensor.py::_threshold_index` had - the thresholds simply
        come from the response now instead of from a tuple in the source.
        """
        if not self.ranges:
            return None
        for band in self.ranges:
            if band.upper_bound is not None and value <= band.upper_bound:
                return band.status
        return self.ranges[-1].status


def resolve_ha_unit(unit: str | None, *, measure: str, device_type: str) -> str | None:
    """
    Map an API unit onto a Home Assistant one, warning about anything unknown.

    Returns `None` both for a unit that has no Home Assistant counterpart
    (`""`, `V - Ix` - a deliberate mapping, no warning) and for one this
    integration has never seen (a WARNING, naming the measure and the type).
    Never raises: an unmappable unit costs that entity its unit, never the
    setup - which is the point, since the alternative is an integration that
    stops working the day the backend adds a measure.
    """
    if unit is None:
        return None
    if unit in HA_UNITS:
        return HA_UNITS[unit]

    _LOGGER.warning(
        "Radoff measure '%s' (device type '%s') declares unit '%s', which "
        "this integration cannot map onto a Home Assistant unit: the entity "
        "is created without one. If the unit is real, it belongs in "
        "`schema.py::HA_UNITS`",
        measure,
        device_type,
        unit,
    )
    return None


def _build_bands(raw_ranges: Any) -> tuple[Band, ...]:
    """Build the band tuple of one measure, preserving the order served."""
    if not isinstance(raw_ranges, list):
        return ()

    bands: list[Band] = []
    for raw_band in raw_ranges:
        if not isinstance(raw_band, dict):
            continue
        status = raw_band.get("status")
        if not isinstance(status, str) or not status:
            continue
        upper_bound = raw_band.get("upperBound")
        bands.append(
            Band(
                status=status,
                upper_bound=(
                    float(upper_bound)
                    if isinstance(upper_bound, int | float)
                    and not isinstance(upper_bound, bool)
                    else None
                ),
            )
        )
    return tuple(bands)


def _sort_key(spec: MeasureSpec) -> tuple[tuple[int, int], str]:
    """
    Order measures by `pos`, then by name; a measure without `pos` goes last.

    `pos` is sparse by design (this module's docstring, point 1), so the key
    is the value itself and never its position in any sequence. A response
    that repeated a `pos`, or dropped it, would still produce a total order
    here rather than an exception or a lost measure.
    """
    if isinstance(spec.pos, int):
        return ((_POS_PRESENT, spec.pos), spec.name)
    return (_POS_LAST, spec.name)


def build_specs(payload: Any, *, device_type: str) -> dict[str, MeasureSpec]:
    """
    Turn a `measures-ranges` response into `{measure name: MeasureSpec}`.

    The returned mapping is ordered by `pos` (see `_sort_key`), which is what
    gives a device's entities the order the backend intends without anyone
    downstream having to sort again - `dict` preserves insertion order and
    `sensor.py` iterates it as-is.

    Anything the response holds that is not a measure object is skipped
    rather than fatal: this is a schema read at setup, and one malformed
    entry must not cost the user every entity of that device type.
    """
    if not isinstance(payload, dict):
        _LOGGER.warning(
            "Radoff measures-ranges for device type '%s' is not an object "
            "(%s): no schema for this type",
            device_type,
            type(payload).__name__,
        )
        return {}

    specs: list[MeasureSpec] = []
    for name, raw_measure in payload.items():
        if not isinstance(name, str) or not isinstance(raw_measure, dict):
            continue

        unit = raw_measure.get("unit")
        pos = raw_measure.get("pos")
        specs.append(
            MeasureSpec(
                name=name,
                label=raw_measure.get("label"),
                unit=unit if isinstance(unit, str) else None,
                ha_unit=resolve_ha_unit(
                    unit if isinstance(unit, str) else None,
                    measure=name,
                    device_type=device_type,
                ),
                data_type=raw_measure.get("dataType"),
                ranges=_build_bands(raw_measure.get("ranges")),
                pos=pos if isinstance(pos, int) and not isinstance(pos, bool) else None,
                acronym=raw_measure.get("acronym"),
            )
        )

    return {spec.name: spec for spec in sorted(specs, key=_sort_key)}
