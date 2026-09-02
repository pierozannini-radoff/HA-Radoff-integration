"""
MAPPING: property metadata for the Radoff API responses.

Pure move from the former `api.py` (see S-06b): keyed by response bucket
(`data`, `aggregatedData`) and property name, each entry carries the device
class, friendly name, unit and (where needed) `normalize_fn` used by
`api/client.py` to build a `RadoffSensor`. Unchanged in form from the
`MAPPING` table that used to live in `api.py` - restructuring it into
per-bucket, explicit-unit descriptors is S-10, not this move.
"""

from typing import Any

from homeassistant.components.sensor import DEVICE_CLASS_UNITS, SensorDeviceClass
from homeassistant.const import UnitOfPressure, UnitOfTemperature

MAPPING: dict[str, dict[str, dict[str, Any]]] = {
    "data": {
        "tvoc": {
            "deviceClass": SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
            "friendlyName": "VOC",
            "unit": next(
                iter(DEVICE_CLASS_UNITS[SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS])
            ),
        },
        "eco2": {
            "deviceClass": SensorDeviceClass.CO2,
            "friendlyName": "Co2",
            "unit": next(iter(DEVICE_CLASS_UNITS[SensorDeviceClass.CO2])),
        },
        "pm10": {
            "deviceClass": SensorDeviceClass.PM10,
            "friendlyName": "PM10",
            "unit": next(iter(DEVICE_CLASS_UNITS[SensorDeviceClass.PM10])),
        },
        "pm25": {
            "deviceClass": SensorDeviceClass.PM25,
            "friendlyName": "PM2.5",
            "unit": next(iter(DEVICE_CLASS_UNITS[SensorDeviceClass.PM25])),
        },
        "pm1": {
            "deviceClass": SensorDeviceClass.PM1,
            "friendlyName": "PM1",
            "unit": next(iter(DEVICE_CLASS_UNITS[SensorDeviceClass.PM1])),
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
            "unit": next(iter(DEVICE_CLASS_UNITS[SensorDeviceClass.HUMIDITY])),
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
    "aggregatedData": {
        "airqualityindex": {
            "deviceClass": SensorDeviceClass.AQI,
            "friendlyName": "Air Quality",
            "unit": None,
        },
    },
}
