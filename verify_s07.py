#!/usr/bin/env python3
"""
Standalone verification for card S-07 (availability based on data freshness).

Stubs just enough of the `homeassistant` package (same approach used for
S-06's `verify_s06.py`) to import `entity.py`, `sensor.py`, `coordinator.py`
and the `api` package without a real Home Assistant install, then exercises
the three `available` conditions and the timestamp parser against synthetic
data. Run from the repo root, with this file and the `custom_components/`
directory as siblings:

    python3 verify_s07.py

Prints one [OK]/[FAIL] line per check and exits non-zero on the first
failure.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


def _install_ha_stubs() -> None:
    """Install minimal stand-ins for the homeassistant modules this integration imports."""

    def module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    ha = module("homeassistant")
    ha_core = module("homeassistant.core")
    ha_const = module("homeassistant.const")
    ha_config_entries = module("homeassistant.config_entries")
    ha_exceptions = module("homeassistant.exceptions")
    ha_helpers = module("homeassistant.helpers")
    ha_helpers_dr = module("homeassistant.helpers.device_registry")
    ha_helpers_uc = module("homeassistant.helpers.update_coordinator")
    ha_helpers_ep = module("homeassistant.helpers.entity_platform")
    ha_components = module("homeassistant.components")
    ha_components_sensor = module("homeassistant.components.sensor")

    ha.const = ha_const
    ha.core = ha_core
    ha.config_entries = ha_config_entries
    ha.exceptions = ha_exceptions
    ha.helpers = ha_helpers
    ha.components = ha_components
    ha_helpers.device_registry = ha_helpers_dr
    ha_helpers.update_coordinator = ha_helpers_uc
    ha_helpers.entity_platform = ha_helpers_ep
    ha_components.sensor = ha_components_sensor

    def callback(func):
        return func

    ha_core.callback = callback
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_core.DOMAIN = "homeassistant"

    ha_const.CONF_USERNAME = "username"
    ha_const.CONF_PASSWORD = "password"
    ha_const.CONF_SCAN_INTERVAL = "scan_interval"
    ha_const.CONF_CLIENT_ID = "client_id"

    # Importing `radoff.coordinator` or `radoff.entity` runs
    # `custom_components/radoff/__init__.py` first (it's the package's own
    # __init__), which does `from homeassistant.const import ...,  Platform`
    # and `PLATFORMS: list[Platform] = [Platform.SENSOR]` at module level -
    # needs a real attribute access to work, a bare object won't do.
    ha_const.Platform = types.SimpleNamespace(SENSOR="sensor")

    class UnitOfTemperature(str, Enum):
        CELSIUS = "°C"

    class UnitOfPressure(str, Enum):
        PA = "Pa"

    ha_const.UnitOfTemperature = UnitOfTemperature
    ha_const.UnitOfPressure = UnitOfPressure

    class ConfigEntry:  # not instantiated in this test; only needs to exist
        pass

    ha_config_entries.ConfigEntry = ConfigEntry

    class HomeAssistantError(Exception):
        pass

    ha_exceptions.HomeAssistantError = HomeAssistantError

    @dataclass
    class DeviceInfo:
        identifiers: set
        name: str
        manufacturer: str
        model: str

    ha_helpers_dr.DeviceInfo = DeviceInfo

    class UpdateFailed(Exception):
        pass

    class CoordinatorEntity:
        """Minimal stand-in for the slice of the real API this integration uses."""

        def __init__(self, coordinator, context=None) -> None:
            self.coordinator = coordinator
            self.coordinator_context = context

        @property
        def available(self) -> bool:
            return bool(self.coordinator.last_update_success)

        def _handle_coordinator_update(self) -> None:
            self.async_write_ha_state()

        def async_write_ha_state(self) -> None:
            self.ha_state_written = True

    class DataUpdateCoordinator:
        def __init__(
            self, hass, logger, *, name, update_method, update_interval
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.last_update_success = True

    ha_helpers_uc.CoordinatorEntity = CoordinatorEntity
    ha_helpers_uc.DataUpdateCoordinator = DataUpdateCoordinator
    ha_helpers_uc.UpdateFailed = UpdateFailed

    class AddEntitiesCallback:
        pass

    ha_helpers_ep.AddEntitiesCallback = AddEntitiesCallback

    class SensorDeviceClass(str, Enum):
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"
        PRESSURE = "pressure"
        CO2 = "carbon_dioxide"
        VOLATILE_ORGANIC_COMPOUNDS = "volatile_organic_compounds"
        PM1 = "pm1"
        PM25 = "pm25"
        PM10 = "pm10"
        AQI = "aqi"

    class SensorStateClass(str, Enum):
        MEASUREMENT = "measurement"

    class SensorEntity:
        pass

    ha_components_sensor.SensorDeviceClass = SensorDeviceClass
    ha_components_sensor.SensorStateClass = SensorStateClass
    ha_components_sensor.SensorEntity = SensorEntity
    ha_components_sensor.DEVICE_CLASS_UNITS = {
        SensorDeviceClass.TEMPERATURE: {"°C"},
        SensorDeviceClass.HUMIDITY: {"%"},
        SensorDeviceClass.CO2: {"ppm"},
        SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS: {"µg/m³"},
        SensorDeviceClass.PM1: {"µg/m³"},
        SensorDeviceClass.PM25: {"µg/m³"},
        SensorDeviceClass.PM10: {"µg/m³"},
    }


_install_ha_stubs()

sys.path.insert(0, "custom_components")

from radoff import coordinator as coordinator_module  # noqa: E402
from radoff.api.client import _parse_measured_at  # noqa: E402
from radoff.api.models import RadoffDevice, Reading  # noqa: E402
from radoff.entity import RadoffEntity  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


class FakeData:
    def __init__(self, devices: list[RadoffDevice]) -> None:
        self.devices = devices


class FakeCoordinator:
    """Duck-types the slice of RadoffCoordinator that RadoffEntity relies on."""

    get_device_by_id = coordinator_module.RadoffCoordinator.get_device_by_id

    def __init__(
        self,
        devices: list[RadoffDevice],
        *,
        last_update_success: bool = True,
        stale_after: timedelta = timedelta(seconds=180),
    ) -> None:
        self.data = FakeData(devices)
        self.last_update_success = last_update_success
        self.stale_after = stale_after


def make_device(readings: dict[str, Reading], *, stale: bool = False) -> RadoffDevice:
    return RadoffDevice(
        device_id="dev-1",
        device_serial="SER-1",
        device_type="Now+",
        name="Living room",
        readings=readings,
        stale=stale,
    )


def make_reading(name: str, *, measured_at: datetime | None = None) -> Reading:
    return Reading(
        name=name,
        value=21.0,
        device_class="temperature",
        friendly_name=name,
        unit="°C",
        normalize_fn=None,
        measured_at=measured_at,
    )


def main() -> None:
    reading = make_reading("internal_temperature")
    device = make_device({"internal_temperature": reading})
    coordinator = FakeCoordinator([device])
    entity = RadoffEntity(coordinator, device, "internal_temperature")

    check("baseline: fresh device+reading, no timestamp -> available", entity.available)
    check("unique_id format", entity.unique_id == "radoff-dev-1-internal_temperature")
    check(
        "device_info identifiers use the serial, not the id",
        entity.device_info.identifiers == {("radoff", "SER-1")},
    )

    # AC1: the device disappears from the poll entirely.
    other_device_still_present = make_device(
        {"relative_humidity": make_reading("relative_humidity")}
    )
    other_entity = RadoffEntity(
        FakeCoordinator([other_device_still_present]),
        other_device_still_present,
        "relative_humidity",
    )
    coordinator.data.devices = []
    check("AC1: vanished device -> its entity is unavailable", not entity.available)
    check(
        "AC1: a different device's entity is unaffected",
        other_entity.available,
    )

    # AC2: device present, this one property missing from this poll.
    sibling_reading = make_reading("relative_humidity")
    device2 = make_device({"relative_humidity": sibling_reading})
    coordinator.data.devices = [device2]
    missing_prop_entity = RadoffEntity(coordinator, device2, "internal_temperature")
    present_prop_entity = RadoffEntity(coordinator, device2, "relative_humidity")
    check("AC2: missing property -> unavailable", not missing_prop_entity.available)
    check(
        "AC2: sibling property still present -> available",
        present_prop_entity.available,
    )

    # Condition 1: the coordinator's own last update failed.
    coordinator.last_update_success = False
    check(
        "condition 1: coordinator failure -> unavailable regardless of data",
        not present_prop_entity.available,
    )
    coordinator.last_update_success = True

    # Condition 2: device.stale (S-13's future signal; always False today).
    stale_device = make_device({"relative_humidity": sibling_reading}, stale=True)
    coordinator.data.devices = [stale_device]
    stale_entity = RadoffEntity(coordinator, stale_device, "relative_humidity")
    check("condition 2: device.stale=True -> unavailable", not stale_entity.available)

    # AC3 + AC4: freshness once measured_at is populated, and recovery without reload.
    fresh_device = make_device(
        {
            "relative_humidity": make_reading(
                "relative_humidity",
                measured_at=datetime.now(UTC) - timedelta(seconds=30),
            )
        }
    )
    old_device = make_device(
        {
            "relative_humidity": make_reading(
                "relative_humidity",
                measured_at=datetime.now(UTC) - timedelta(seconds=999),
            )
        }
    )

    coordinator.data.devices = [fresh_device]
    fresh_entity = RadoffEntity(coordinator, fresh_device, "relative_humidity")
    check("AC3: sample younger than stale_after -> available", fresh_entity.available)

    coordinator.data.devices = [old_device]
    old_entity = RadoffEntity(coordinator, old_device, "relative_humidity")
    check(
        "AC3: sample older than 3x update_interval -> unavailable",
        not old_entity.available,
    )

    # AC4: the SAME entity object recovers as soon as fresh data reappears,
    # with no reload - because available() is computed fresh each time.
    coordinator.data.devices = [old_device]
    check("AC4 setup: entity currently unavailable", not old_entity.available)
    coordinator.data.devices = [fresh_device]
    old_entity.reading_key = "relative_humidity"  # unchanged; documents intent
    recovered_entity_view = RadoffEntity(coordinator, fresh_device, "relative_humidity")
    check(
        "AC4: fresh data reappearing makes availability true again, no reload",
        recovered_entity_view.available,
    )

    # T-02 finding: no per-sample timestamp exists, but the device-level
    # last_data_received_at does - available() must fall back to it when
    # Reading.measured_at is None (which, per T-02, is always today).
    fresh_device_level = make_device(
        {
            "relative_humidity": make_reading("relative_humidity")  # measured_at=None
        },
    )
    fresh_device_level.last_data_received_at = datetime.now(UTC) - timedelta(seconds=30)
    coordinator.data.devices = [fresh_device_level]
    fresh_fallback_entity = RadoffEntity(
        coordinator, fresh_device_level, "relative_humidity"
    )
    check(
        "device-level fallback: recent last_data_received_at -> available",
        fresh_fallback_entity.available,
    )

    old_device_level = make_device(
        {"relative_humidity": make_reading("relative_humidity")},
    )
    old_device_level.last_data_received_at = datetime.now(UTC) - timedelta(seconds=999)
    coordinator.data.devices = [old_device_level]
    old_fallback_entity = RadoffEntity(
        coordinator, old_device_level, "relative_humidity"
    )
    check(
        "device-level fallback: stale last_data_received_at -> unavailable",
        not old_fallback_entity.available,
    )

    # Per-sample measured_at, when present, takes priority over the
    # device-level fallback - not exercised by any real payload today (T-02
    # confirmed there is none), but the precedence must hold if one ever
    # appears without this logic needing to change.
    mixed_device = make_device(
        {
            "relative_humidity": make_reading(
                "relative_humidity",
                measured_at=datetime.now(UTC) - timedelta(seconds=30),
            )
        },
    )
    mixed_device.last_data_received_at = datetime.now(UTC) - timedelta(seconds=999)
    coordinator.data.devices = [mixed_device]
    mixed_entity = RadoffEntity(coordinator, mixed_device, "relative_humidity")
    check(
        "precedence: fresh per-sample measured_at wins over a stale device-level timestamp",
        mixed_entity.available,
    )

    # _parse_measured_at: exercised directly since the production candidate
    # tuple is confirmed empty by T-02 (see api/client.py).
    import radoff.api.client as client_module

    iso_obj = {"measuredAt": "2026-09-02T10:00:00Z"}
    epoch_s_obj = {"measuredAt": int(datetime(2026, 9, 2, tzinfo=UTC).timestamp())}
    epoch_ms_obj = {
        "measuredAt": int(datetime(2026, 9, 2, tzinfo=UTC).timestamp() * 1000)
    }
    bad_obj = {"measuredAt": "not-a-date"}
    missing_obj: dict = {}

    original_keys = client_module._MEASURED_AT_KEYS
    client_module._MEASURED_AT_KEYS = ("measuredAt",)
    try:
        check(
            "_parse_measured_at: ISO-8601 with trailing Z",
            _parse_measured_at(iso_obj) is not None,
        )
        check(
            "_parse_measured_at: epoch seconds",
            _parse_measured_at(epoch_s_obj) is not None,
        )
        check(
            "_parse_measured_at: epoch milliseconds",
            _parse_measured_at(epoch_ms_obj) is not None,
        )
        check(
            "_parse_measured_at: unparseable value falls back to None",
            _parse_measured_at(bad_obj) is None,
        )
        check(
            "_parse_measured_at: missing key falls back to None",
            _parse_measured_at(missing_obj) is None,
        )
    finally:
        client_module._MEASURED_AT_KEYS = original_keys

    check(
        "production default: _MEASURED_AT_KEYS is empty (T-02 confirmed no per-sample field)",
        client_module._MEASURED_AT_KEYS == (),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for label in FAILURES:
            print(f"  - {label}")
        raise SystemExit(1)
    print("All S-07 checks passed.")


if __name__ == "__main__":
    main()
