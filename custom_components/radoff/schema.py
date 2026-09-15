"""
The per-device-type measurement schema, as the API itself serves it.

Units, labels and thresholds are read at setup from `measures-ranges`, one
call per type, and never hardcoded: there is no table to fall back on.
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

# API unit -> Home Assistant unit, whose own set is a closed enumeration. The
# AQI is dimensionless; `Bq/m³` and `V - Ix` pass through as literal strings.
HA_UNITS: dict[str, str | None] = {
    "Pa": UnitOfPressure.PA,
    "%": PERCENTAGE,
    "ppm": CONCENTRATION_PARTS_PER_MILLION,
    "µg/m³": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    "°C": UnitOfTemperature.CELSIUS,
    "Bq/m³": "Bq/m³",
    "V - Ix": "V - Ix",
    "": None,
}

# Measure -> device class, where one fits. Radon, ch4, tvoc and aqi_value get
# none: no such class exists, or its scale is not ours (AQI means EPA 0-500).
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
    """One qualitative band: a status and the value it reaches up to."""

    status: str
    upper_bound: float | None = None


@dataclass(frozen=True)
class MeasureSpec:
    """
    Everything the API declares about one measure of one device type.

    `unit` is what the API sent, `ha_unit` that string resolved against
    `HA_UNITS`. `ranges` holds the bands in the order served, which is the
    order the backend intends: `high` sits between `excellent` and `good`.
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
        """Return the qualitative status `value` falls in, or `None` if unbanded."""
        if not self.ranges:
            return None
        for band in self.ranges:
            if band.upper_bound is not None and value <= band.upper_bound:
                return band.status
        return self.ranges[-1].status


def resolve_ha_unit(unit: str | None, *, measure: str, device_type: str) -> str | None:
    """Map an API unit onto a Home Assistant one, warning about anything unknown."""
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
    """Order measures by `pos`, then by name; one without `pos` goes last."""
    if isinstance(spec.pos, int):
        return ((_POS_PRESENT, spec.pos), spec.name)
    return (_POS_LAST, spec.name)


def build_specs(payload: Any, *, device_type: str) -> dict[str, MeasureSpec]:
    """Turn a `measures-ranges` response into `{measure name: MeasureSpec}`."""
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
