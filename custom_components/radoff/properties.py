"""
MAPPING: property metadata for the Radoff API responses.

Card S-06b moved this table verbatim out of the former `api.py`. Card S-10
changes its *content*, not its location (`api/client.py` still does
`from .properties import MAPPING`):

1. `MAPPING` is now keyed by `Bucket` first, then property name - a flat
   `dict[str, dict[str, ...]]` keyed by bare property name used to make
   `data.airqualityindex` and `aggregatedData.airqualityindex` collide into
   the same `readings` dict entry (finding C4). Restructuring the table
   itself to mirror the `(bucket, property_name)` shape `api/client.py` now
   builds keys with keeps the two in sync by construction - there is no
   longer a single global namespace a new bucket's property name could
   collide into.
2. Every `unit` is now an explicit constant instead of picking, via
   `next(iter(...))`, an arbitrary element out of the `set` of units Home
   Assistant's sensor integration considers valid for a given device class
   (finding C10): that set's iteration order is not part of any
   compatibility guarantee HA makes across versions or even across
   interpreter runs (hash randomization). For a device class with more than
   one valid unit - PM1/PM10/PM2.5 allow both µg/m³ and µg/ft³ - the
   previous code had no control over which one HA would report as the
   entity's declared unit; a value keeps flowing to Home Assistant
   completely unconverted no matter which unit the label says, so the risk
   was silent mislabeling (µg/m³ readings reported as µg/ft³, or vice
   versa), not a crash. Every constant below was picked to match the actual
   semantics of the value the Radoff API returns for that property, not
   merely "a valid unit for this device class".

`properties.py` is still an intermediate step, not the final architecture:
per `architettura-target-sprint-m.md` §2, it stays a single global table
until L introduces per-device-profile descriptors under `devices/`. What
S-10 deliberately does NOT do (see the card's "OUT OF SCOPE"): change how
`internal_temperature`'s `normalize_fn` scales its raw value (the `0.00835`
factor - finding C11, needs a real-world answer first, tracked under T-02's
open question 3), and move `sensor.py::INDEX_MAPPING`'s qualitative
thresholds into this table - both are separate work.
"""

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
)

from .api.models import Bucket

MAPPING: dict[Bucket, dict[str, dict[str, Any]]] = {
    Bucket.DATA: {
        "tvoc": {
            "deviceClass": SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
            "friendlyName": "VOC",
            "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        },
        "eco2": {
            "deviceClass": SensorDeviceClass.CO2,
            "friendlyName": "Co2",
            "unit": CONCENTRATION_PARTS_PER_MILLION,
        },
        "pm10": {
            "deviceClass": SensorDeviceClass.PM10,
            "friendlyName": "PM10",
            "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        },
        "pm25": {
            "deviceClass": SensorDeviceClass.PM25,
            "friendlyName": "PM2.5",
            "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        },
        "pm1": {
            "deviceClass": SensorDeviceClass.PM1,
            "friendlyName": "PM1",
            "unit": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        },
        "internal_temperature": {
            "deviceClass": SensorDeviceClass.TEMPERATURE,
            "friendlyName": "Temperature",
            "unit": UnitOfTemperature.CELSIUS,
            "normalize_fn": lambda value: round(float(value) * 0.00835, 1),
        },
        "relative_humidity": {
            "deviceClass": SensorDeviceClass.HUMIDITY,
            "friendlyName": "Humidity",
            "unit": PERCENTAGE,
        },
        "pressure": {
            "deviceClass": SensorDeviceClass.PRESSURE,
            "friendlyName": "Pressure",
            "unit": UnitOfPressure.PA,
        },
        "airqualityindex": {
            "deviceClass": SensorDeviceClass.AQI,
            "friendlyName": "Air Quality",
            "unit": None,
        },
    },
    Bucket.AGGREGATED: {
        "airqualityindex": {
            "deviceClass": SensorDeviceClass.AQI,
            "friendlyName": "Air Quality",
            "unit": None,
        },
    },
}
