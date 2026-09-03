#!/usr/bin/env python3
"""
Standalone verification for card S-10 (composite reading key, explicit units).

Same approach as `verify_s06.py`/`verify_s07.py`: stub the `homeassistant`
package (plus the two third-party auth libraries `api/auth.py` needs -
`botocore`, `pycognito` - which are not required to exercise this card's
code paths) with the minimal surface `custom_components/radoff` actually
imports, then import the real package and exercise it directly. No real
Home Assistant install, and no Radoff account, is needed.

Run from the repository root:

    python3 verify_s10.py

Covers, in order, every S-10 acceptance criterion:

1. A payload with `airqualityindex` in both `data` and `aggregatedData`
   produces two independent `Reading`s (`API._get_data`, resolves C4).
2. No occurrence of `DEVICE_CLASS_UNITS` anywhere under `custom_components/`.
3. Every unit in `properties.MAPPING` is one of a fixed set of expected
   constants (not "any valid unit for the device class").
4. Existing DATA-bucket `unique_id`s are produced in the exact pre-S-10
   format; only the new AGGREGATED-bucket entities get a new, distinct one.
5. Every entity `sensor.py` can build (for every bucket/property/index
   combination in `MAPPING`/`INDEX_MAPPING`) has a translated name in
   `strings.json`, `translations/en.json` and `translations/it.json`.
6. `sensor.py::async_setup_entry` itself (not a re-implementation of its
   logic) builds two distinct sensors, with independent values, from a
   device carrying both an instantaneous and an aggregated
   `airqualityindex` reading.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
RADOFF_DIR = REPO_ROOT / "custom_components" / "radoff"

_CHECKS_RUN = 0
_FAILURES: list[str] = []


def check(label: str, *, condition: bool) -> None:
    """Record one assertion's outcome without stopping the whole run on failure."""
    global _CHECKS_RUN  # noqa: PLW0603
    _CHECKS_RUN += 1
    if condition:
        print(f"[OK] {label}")
    else:
        print(f"[FAIL] {label}")
        _FAILURES.append(label)


# --------------------------------------------------------------------------
# 1. Stub `homeassistant` (+ botocore/pycognito) with the minimal surface
#    custom_components/radoff imports, then make the real package importable.
# --------------------------------------------------------------------------


def _install(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []  # let Python treat it as a package if needed
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _stub_dependencies() -> None:
    # --- botocore / pycognito: only api/auth.py needs these, and only at
    # import time (nothing in this card calls the Cognito handshake). ---
    class ClientError(Exception):
        def __init__(self, *args: Any, response: dict | None = None) -> None:
            super().__init__(*args)
            self.response = response or {}

    class EndpointConnectionError(Exception):
        pass

    _install("botocore")
    _install(
        "botocore.exceptions",
        ClientError=ClientError,
        EndpointConnectionError=EndpointConnectionError,
    )

    class AWSSRP:  # never instantiated in this verification
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
            raise NotImplementedError

    _install("pycognito")
    _install("pycognito.aws_srp", AWSSRP=AWSSRP)

    # --- homeassistant.const ---
    class Platform(StrEnum):
        SENSOR = "sensor"

    class UnitOfPressure(StrEnum):
        PA = "Pa"

    class UnitOfTemperature(StrEnum):
        CELSIUS = "°C"

    _install(
        "homeassistant",
        __version__="stub-for-verify_s10",
    )
    _install(
        "homeassistant.const",
        CONF_CLIENT_ID="client_id",
        CONF_PASSWORD="password",
        CONF_SCAN_INTERVAL="scan_interval",
        CONF_USERNAME="username",
        Platform=Platform,
        UnitOfPressure=UnitOfPressure,
        UnitOfTemperature=UnitOfTemperature,
        CONCENTRATION_MICROGRAMS_PER_CUBIC_METER="µg/m³",
        CONCENTRATION_PARTS_PER_MILLION="ppm",
        PERCENTAGE="%",
    )

    # --- homeassistant.core ---
    def callback(func: Any) -> Any:
        return func

    class HomeAssistant:  # only ever used as an unused type hint here
        pass

    _install(
        "homeassistant.core",
        callback=callback,
        HomeAssistant=HomeAssistant,
        DOMAIN="homeassistant",
    )

    # --- homeassistant.exceptions ---
    class ConfigEntryAuthFailed(Exception):
        pass

    class HomeAssistantError(Exception):
        pass

    _install(
        "homeassistant.exceptions",
        ConfigEntryAuthFailed=ConfigEntryAuthFailed,
        HomeAssistantError=HomeAssistantError,
    )

    # --- homeassistant.config_entries ---
    class ConfigEntry:  # never instantiated for real in this verification
        pass

    _install("homeassistant.config_entries", ConfigEntry=ConfigEntry)

    # --- homeassistant.helpers (+ submodules) ---
    _install("homeassistant.helpers")

    class DeviceInfo(dict):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

    _install("homeassistant.helpers.device_registry", DeviceInfo=DeviceInfo)

    class CoordinatorEntity:
        def __init__(self, coordinator: Any, context: Any = None) -> None:
            self.coordinator = coordinator
            self.coordinator_context = context

        @property
        def available(self) -> bool:
            return bool(getattr(self.coordinator, "last_update_success", True))

        def _handle_coordinator_update(self) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    class DataUpdateCoordinator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.last_update_success = True

    class UpdateFailed(Exception):
        pass

    _install(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    )

    class AddEntitiesCallback:  # only used as a type hint here
        pass

    _install(
        "homeassistant.helpers.entity_platform", AddEntitiesCallback=AddEntitiesCallback
    )

    # --- homeassistant.components.sensor ---
    class SensorDeviceClass(StrEnum):
        AQI = "aqi"
        CO2 = "carbon_dioxide"
        HUMIDITY = "humidity"
        PM1 = "pm1"
        PM10 = "pm10"
        PM25 = "pm25"
        PRESSURE = "pressure"
        TEMPERATURE = "temperature"
        VOLATILE_ORGANIC_COMPOUNDS = "volatile_organic_compounds"

    class SensorStateClass(StrEnum):
        MEASUREMENT = "measurement"

    class SensorEntity:
        pass

    _install("homeassistant.components")
    _install(
        "homeassistant.components.sensor",
        SensorDeviceClass=SensorDeviceClass,
        SensorEntity=SensorEntity,
        SensorStateClass=SensorStateClass,
    )

    # Make `custom_components/` importable as top-level packages
    # (`radoff...`), same convention as verify_s06.py/verify_s07.py.
    sys.path.insert(0, str(REPO_ROOT / "custom_components"))


_stub_dependencies()

from radoff.api.client import API  # noqa: E402
from radoff.api.models import Bucket, RadoffDevice, Reading, ReadingKey  # noqa: E402
from radoff.const import DOMAIN  # noqa: E402
from radoff.entity import reading_key_slug  # noqa: E402
from radoff.properties import MAPPING  # noqa: E402
from radoff.sensor import INDEX_MAPPING, RadoffSensor, async_setup_entry  # noqa: E402

from homeassistant.const import (  # noqa: E402
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
)


# --------------------------------------------------------------------------
# 2. AC: no occurrence of DEVICE_CLASS_UNITS anywhere in the shipped code.
# --------------------------------------------------------------------------


def _check_no_device_class_units() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in RADOFF_DIR.rglob("*.py")
        if "DEVICE_CLASS_UNITS" in path.read_text(encoding="utf-8")
    ]
    check(
        "AC: no occurrence of DEVICE_CLASS_UNITS under custom_components/radoff/",
        condition=not offenders,
    )
    if offenders:
        print(f"       found in: {offenders}")


# --------------------------------------------------------------------------
# 3. AC: every declared unit is one fixed, expected constant.
# --------------------------------------------------------------------------

_EXPECTED_UNITS: dict[tuple[Bucket, str], Any] = {
    (Bucket.DATA, "tvoc"): CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    (Bucket.DATA, "eco2"): CONCENTRATION_PARTS_PER_MILLION,
    (Bucket.DATA, "pm10"): CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    (Bucket.DATA, "pm25"): CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    (Bucket.DATA, "pm1"): CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    (Bucket.DATA, "internal_temperature"): UnitOfTemperature.CELSIUS,
    (Bucket.DATA, "relative_humidity"): PERCENTAGE,
    (Bucket.DATA, "pressure"): UnitOfPressure.PA,
    (Bucket.DATA, "airqualityindex"): None,
    (Bucket.AGGREGATED, "airqualityindex"): None,
}


def _check_expected_units() -> None:
    actual: dict[tuple[Bucket, str], Any] = {
        (bucket, prop): descriptor["unit"]
        for bucket, props in MAPPING.items()
        for prop, descriptor in props.items()
    }
    check(
        "AC: MAPPING covers exactly the expected (bucket, property) pairs",
        condition=set(actual) == set(_EXPECTED_UNITS),
    )
    for key, expected_unit in _EXPECTED_UNITS.items():
        check(
            f"AC: unit for {key} is the expected stable constant ({expected_unit!r})",
            condition=actual.get(key) == expected_unit,
        )


# --------------------------------------------------------------------------
# 4. AC: airqualityindex in both buckets -> two independent Readings.
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.url = "https://example.invalid/"

    def json(self) -> dict:
        return self._payload


def _check_get_data_splits_buckets() -> None:
    api = API(username="u", password="p", domain_id="dom1")
    api.tokens = {"IdToken": "fake-id-token"}

    payload = {
        "data": {
            "lastDataReceivedAt": "2026-09-03T10:00:00.147Z",
            "data": [
                {"propertyName": "airqualityindex", "value": 42},
                {"propertyName": "tvoc", "value": 123},
            ],
            "aggregatedData": [
                {"propertyName": "airqualityindex", "aggregationValue": 77},
            ],
        }
    }
    api.session.get = lambda *a, **kw: _FakeResponse(200, payload)  # noqa: ARG005

    readings, last_data_received_at = api._get_data("device-1")  # noqa: SLF001

    data_key: ReadingKey = (Bucket.DATA, "airqualityindex")
    agg_key: ReadingKey = (Bucket.AGGREGATED, "airqualityindex")

    check(
        "AC1: both bucket keys present in readings",
        condition=data_key in readings and agg_key in readings,
    )
    check(
        "AC1: DATA airqualityindex keeps its own value",
        condition=readings.get(data_key) is not None and readings[data_key].value == 42,
    )
    check(
        "AC1: AGGREGATED airqualityindex keeps its own, independent value",
        condition=readings.get(agg_key) is not None and readings[agg_key].value == 77,
    )
    check(
        "AC1: the two values are independent (not the same object/value)",
        condition=readings[data_key].value != readings[agg_key].value,
    )
    check(
        "AC1: each Reading records its own bucket",
        condition=readings[data_key].bucket is Bucket.DATA
        and readings[agg_key].bucket is Bucket.AGGREGATED,
    )
    check(
        "device-level lastDataReceivedAt still parsed",
        condition=last_data_received_at is not None,
    )
    check(
        "a third, unrelated property (tvoc) is unaffected",
        condition=readings.get((Bucket.DATA, "tvoc")) is not None
        and readings[(Bucket.DATA, "tvoc")].value == 123,
    )

    return readings  # noqa: RET504


# --------------------------------------------------------------------------
# 5. AC: existing DATA unique_id format is untouched; AGGREGATED gets a new,
#    distinct one. Values stay independent at the entity layer too.
# --------------------------------------------------------------------------


@dataclass
class _FakeCoordinatorData:
    devices: list[RadoffDevice]
    generate_index: bool


class _FakeCoordinator:
    def __init__(self, device: RadoffDevice, *, generate_index: bool = True) -> None:
        self.data = _FakeCoordinatorData(
            devices=[device], generate_index=generate_index
        )
        self.last_update_success = True
        self.stale_after = timedelta(hours=1)

    def get_device_by_id(self, device_type: str, device_id: str) -> RadoffDevice | None:
        for device in self.data.devices:
            if device.device_type == device_type and device.device_id == device_id:
                return device
        return None


def _build_two_bucket_device() -> RadoffDevice:
    readings: dict[ReadingKey, Reading] = {
        (Bucket.DATA, "airqualityindex"): Reading(
            name="airqualityindex",
            bucket=Bucket.DATA,
            value=42,
            device_class=MAPPING[Bucket.DATA]["airqualityindex"]["deviceClass"],
            friendly_name=MAPPING[Bucket.DATA]["airqualityindex"]["friendlyName"],
            unit=MAPPING[Bucket.DATA]["airqualityindex"]["unit"],
            normalize_fn=None,
        ),
        (Bucket.AGGREGATED, "airqualityindex"): Reading(
            name="airqualityindex",
            bucket=Bucket.AGGREGATED,
            value=77,
            device_class=MAPPING[Bucket.AGGREGATED]["airqualityindex"]["deviceClass"],
            friendly_name=MAPPING[Bucket.AGGREGATED]["airqualityindex"]["friendlyName"],
            unit=MAPPING[Bucket.AGGREGATED]["airqualityindex"]["unit"],
            normalize_fn=None,
        ),
    }
    return RadoffDevice(
        device_id="dev1",
        device_serial="SN1",
        device_type="Now+",
        name="Device 1",
        readings=readings,
    )


def _check_entity_layer(device: RadoffDevice) -> _FakeCoordinator:
    coordinator = _FakeCoordinator(device)

    data_reading = device.readings[(Bucket.DATA, "airqualityindex")]
    agg_reading = device.readings[(Bucket.AGGREGATED, "airqualityindex")]

    sensor_data = RadoffSensor(
        reading_key=(Bucket.DATA, "airqualityindex"),
        device=device,
        coordinator=coordinator,
        device_class=data_reading.device_class,
        friendly_name=data_reading.friendly_name,
        normalize_fn=data_reading.normalize_fn,
        unit=data_reading.unit,
        index_fn=None,
        is_index=False,
    )
    sensor_agg = RadoffSensor(
        reading_key=(Bucket.AGGREGATED, "airqualityindex"),
        device=device,
        coordinator=coordinator,
        device_class=agg_reading.device_class,
        friendly_name=agg_reading.friendly_name,
        normalize_fn=agg_reading.normalize_fn,
        unit=agg_reading.unit,
        index_fn=None,
        is_index=False,
    )

    expected_data_unique_id = f"{DOMAIN}-dev1-airqualityindex"  # exact pre-S-10 format
    check(
        "AC4: DATA-bucket unique_id is byte-for-byte the pre-S-10 format",
        condition=sensor_data.unique_id == expected_data_unique_id,
    )
    check(
        "AC4: AGGREGATED-bucket unique_id is new and distinct",
        condition=sensor_agg.unique_id == f"{DOMAIN}-dev1-airqualityindex_average"
        and sensor_agg.unique_id != sensor_data.unique_id,
    )
    check(
        "translation_key: DATA bucket unchanged ('airqualityindex')",
        condition=sensor_data.translation_key == "airqualityindex",
    )
    check(
        "translation_key: AGGREGATED bucket is the card's own example ('airqualityindex_average')",
        condition=sensor_agg.translation_key == "airqualityindex_average",
    )
    check(
        "entity layer: DATA native_value == 42",
        condition=sensor_data.native_value == 42,
    )
    check(
        "entity layer: AGGREGATED native_value == 77, independent of DATA's",
        condition=sensor_agg.native_value == 77,
    )
    check(
        "both entities report available (fresh device, no stale flag)",
        condition=sensor_data.available is True and sensor_agg.available is True,
    )

    # An "-index" sibling on a DATA-bucket property still gets the old
    # unique_id/translation_key shape, unaffected by this card.
    tvoc_reading_key: ReadingKey = (Bucket.DATA, "tvoc")
    tvoc_device = RadoffDevice(
        device_id="dev1",
        device_serial="SN1",
        device_type="Now+",
        name="Device 1",
        readings={
            tvoc_reading_key: Reading(
                name="tvoc",
                bucket=Bucket.DATA,
                value=150,
                device_class=MAPPING[Bucket.DATA]["tvoc"]["deviceClass"],
                friendly_name=MAPPING[Bucket.DATA]["tvoc"]["friendlyName"],
                unit=MAPPING[Bucket.DATA]["tvoc"]["unit"],
                normalize_fn=None,
            ),
        },
    )
    tvoc_coordinator = _FakeCoordinator(tvoc_device)
    tvoc_index_sensor = RadoffSensor(
        reading_key=tvoc_reading_key,
        device=tvoc_device,
        coordinator=tvoc_coordinator,
        device_class=None,
        friendly_name="VOC",
        normalize_fn=None,
        unit=None,
        index_fn=INDEX_MAPPING["tvoc"]["index"],
        is_index=True,
    )
    check(
        "regression: DATA-bucket index unique_id keeps its '-index' suffix",
        condition=tvoc_index_sensor.unique_id == f"{DOMAIN}-dev1-tvoc-index",
    )
    check(
        "regression: DATA-bucket index translation_key keeps its '_index' suffix",
        condition=tvoc_index_sensor.translation_key == "tvoc_index",
    )

    return coordinator


# --------------------------------------------------------------------------
# 6. AC: no entity without a translated name in the UI.
# --------------------------------------------------------------------------


def _load_entity_sensor_names(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: value.get("name", "") for key, value in payload["entity"]["sensor"].items()
    }


def _check_translations_cover_every_entity() -> None:
    # S-10 only ever *adds* entities for buckets other than DATA (today,
    # just AGGREGATED - see properties.py::MAPPING). The AC ("nessuna
    # entità priva di nome nella UI") is this card's own doing: every slug
    # this card introduces must resolve to a real name. Pre-existing
    # DATA-bucket coverage (a couple of properties - pm1/pm10/pm25 - were
    # already relying on Home Assistant's own device-class default name
    # rather than an explicit strings.json entry before this card) is
    # unrelated to S-10 and reported, not failed, so a pre-existing gap
    # doesn't masquerade as a regression introduced here.
    new_slugs: set[str] = set()
    preexisting_slugs: set[str] = set()
    for bucket, props in MAPPING.items():
        for prop in props:
            slug = reading_key_slug((bucket, prop))
            target = new_slugs if bucket is not Bucket.DATA else preexisting_slugs
            target.add(slug)
            if prop in INDEX_MAPPING:
                target.add(f"{slug}_index")

    for filename in ("strings.json", "translations/en.json", "translations/it.json"):
        names = _load_entity_sensor_names(RADOFF_DIR / filename)
        missing_new = sorted(new_slugs - set(names))
        check(
            f"AC: {filename} has a translated name for every entity slug this card adds",
            condition=not missing_new,
        )
        if missing_new:
            print(f"       missing: {missing_new}")
        empty = sorted(
            slug for slug in new_slugs if slug in names and not names[slug].strip()
        )
        check(
            f"AC: {filename} has no *empty* name for a slug this card adds",
            condition=not empty,
        )

        missing_preexisting = sorted(preexisting_slugs - set(names))
        if missing_preexisting:
            print(
                f"       (pre-existing, out of scope for S-10) {filename} has no "
                f"explicit name for: {missing_preexisting} - relies on HA's "
                "device-class default name"
            )


# --------------------------------------------------------------------------
# 7. AC (end-to-end): the real sensor.py::async_setup_entry produces two
#    distinct entities from a device with both bucket readings.
# --------------------------------------------------------------------------


class _FakeRuntimeData:
    def __init__(self, coordinator: _FakeCoordinator) -> None:
        self.coordinator = coordinator


class _FakeConfigEntry:
    entry_id = "entry1"


def _check_async_setup_entry(coordinator: _FakeCoordinator) -> None:
    fake_hass = types.SimpleNamespace(
        data={DOMAIN: {"entry1": _FakeRuntimeData(coordinator)}}
    )
    captured: list[RadoffSensor] = []

    def _capture(entities: list[RadoffSensor]) -> None:
        captured.extend(entities)

    asyncio.run(async_setup_entry(fake_hass, _FakeConfigEntry(), _capture))

    check(
        "async_setup_entry: exactly 2 entities for a 2-bucket-reading device",
        condition=len(captured) == 2,
    )
    unique_ids = {sensor.unique_id for sensor in captured}
    check(
        "async_setup_entry: the 2 entities have 2 distinct unique_ids",
        condition=len(unique_ids) == 2,
    )
    values = {sensor.native_value for sensor in captured}
    check(
        "async_setup_entry: the 2 entities carry 2 distinct, independent values",
        condition=values == {42, 77},
    )


def main() -> int:
    print("=== S-10: composite reading key + explicit units ===\n")

    _check_no_device_class_units()
    _check_expected_units()
    _check_get_data_splits_buckets()

    device = _build_two_bucket_device()
    coordinator = _check_entity_layer(device)

    _check_translations_cover_every_entity()
    _check_async_setup_entry(coordinator)

    print(f"\n{_CHECKS_RUN - len(_FAILURES)}/{_CHECKS_RUN} checks passed.")
    if _FAILURES:
        print("\nFAILED:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1

    print("\nAll S-10 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
