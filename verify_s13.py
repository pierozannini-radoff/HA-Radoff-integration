#!/usr/bin/env python3
"""
Standalone verification script for card S-13 (per-device error isolation +
overall update timeout).

Run with `python3 verify_s13.py` from the repository root. Same convention
as `verify_s07.py`/`verify_s08.py`/.../`verify_s12.py`: a real Home
Assistant install is not needed - `homeassistant` is stubbed below the same
way those earlier scripts stubbed it (per `s-07-implementazione.md`'s own
account, importing `radoff` runs the real `custom_components/radoff/__init__.py`
too, which is why the stub below also covers `Platform`/`CONF_CLIENT_ID`,
the two extra names that module needs at import time - it defines
functions and dataclasses only, nothing in it actually runs against a real
`hass`). `boto3`/`botocore`/`pycognito` are used for real (they are regular
pip dependencies per `requirements.txt`, unlike `homeassistant` itself).

What this checks, mapped to the card's acceptance criteria:

- AC "un 500 forzato su un solo device -> l'altro continua ad aggiornarsi":
  `API.get_devices()` isolates an HTTP error (APIAuthError) and a transport
  error (requests.exceptions.RequestException) on one device without
  affecting the other.
- "COSA FARE" step 6 ("un errore auth su un device NON va isolato"):
  a 401 on a single device's GET propagates out of `get_devices()` instead
  of being isolated.
- AC "un fallimento della search porta tutte le entità a unavailable, come
  oggi": a `search`-level failure raises immediately, before any per-device
  isolation logic runs.
- "COSA FARE" step 2 (coordinator merge): a device with an isolated error
  is folded back with `stale=True`, reusing the previous poll's readings
  when available, or with empty readings when there is no previous poll for
  that device.
- AC "Un ciclo di update non supera mai 0,8 x update_interval": a slow
  `get_devices()` call trips `asyncio.timeout` and `_async_update_data`
  raises `UpdateFailed` with an explicit, timeout-specific message.
- AC "Il dispositivo in errore torna disponibile al primo poll riuscito,
  senza reload": a device present in a later successful poll simply
  replaces its previous stale entry - nothing about it is "sticky".
- Card step 4 (Retry total=2): the configured `requests.Session`'s
  `HTTPAdapter` actually uses `total=2`.

Not covered here (deliberately, same reasoning as `verify_s07.py`'s own
scope note): actual Home Assistant entity `unavailable` rendering - that is
`RadoffEntity.available` (S-07), already exercised by `verify_s07.py`
against the very `stale` flag this script proves S-13 now sets correctly.
"Test di poll degradato in S-18 verdi" (this card's own dependency) is out
of scope until S-18 exists.
"""

import asyncio
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Generic, TypeVar

_DataT = TypeVar("_DataT")

REPO_ROOT = Path(__file__).resolve().parent

_FAILURES: list[str] = []


def check(label: str, condition: bool) -> None:  # noqa: FBT001
    """Record a single pass/fail check and print it immediately."""
    if condition:
        print(f"[OK] {label}")
    else:
        print(f"[FAIL] {label}")
        _FAILURES.append(label)


# --------------------------------------------------------------------------
# Minimal `homeassistant` stub - only the names actually imported by
# const.py, properties.py, api/models.py and coordinator.py (this card's
# "FILE COINVOLTI"). Same technique earlier verify_sXX.py scripts used.
# --------------------------------------------------------------------------


def _install_homeassistant_stub() -> None:
    ha = ModuleType("homeassistant")
    ha.__path__ = []  # mark as a package

    ha_const = ModuleType("homeassistant.const")
    ha_const.CONF_PASSWORD = "password"
    ha_const.CONF_SCAN_INTERVAL = "scan_interval"
    ha_const.CONF_USERNAME = "username"
    ha_const.PERCENTAGE = "%"
    ha_const.CONCENTRATION_MICROGRAMS_PER_CUBIC_METER = "µg/m³"
    ha_const.CONCENTRATION_PARTS_PER_MILLION = "ppm"
    ha_const.CONF_CLIENT_ID = "client_id"

    class Platform(str, Enum):
        SENSOR = "sensor"

    ha_const.Platform = Platform

    class UnitOfPressure:
        PA = "Pa"

    class UnitOfTemperature:
        CELSIUS = "°C"

    ha_const.UnitOfPressure = UnitOfPressure
    ha_const.UnitOfTemperature = UnitOfTemperature

    ha_core = ModuleType("homeassistant.core")
    ha_core.DOMAIN = "homeassistant"

    class HomeAssistant:
        """Stand-in for homeassistant.core.HomeAssistant."""

    ha_core.HomeAssistant = HomeAssistant

    def callback(func):  # noqa: ANN001, ANN201
        return func

    ha_core.callback = callback

    ha_config_entries = ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        """Stand-in for homeassistant.config_entries.ConfigEntry."""

        def __init__(self, data=None, options=None, unique_id=None, entry_id=None):  # noqa: ANN001
            self.data = data or {}
            self.options = options or {}
            self.unique_id = unique_id
            self.entry_id = entry_id

    ha_config_entries.ConfigEntry = ConfigEntry

    ha_exceptions = ModuleType("homeassistant.exceptions")

    class ConfigEntryAuthFailed(Exception):
        """Stand-in for homeassistant.exceptions.ConfigEntryAuthFailed."""

    class HomeAssistantError(Exception):
        """Stand-in for homeassistant.exceptions.HomeAssistantError."""

    ha_exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    ha_exceptions.HomeAssistantError = HomeAssistantError

    ha_helpers = ModuleType("homeassistant.helpers")
    ha_helpers.__path__ = []

    ha_helpers_update_coordinator = ModuleType(
        "homeassistant.helpers.update_coordinator"
    )

    class UpdateFailed(Exception):
        """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""

    class DataUpdateCoordinator(Generic[_DataT]):
        """
        Minimal stand-in for DataUpdateCoordinator.

        Only what RadoffCoordinator.__init__ and _async_update_data actually
        use: stores update_interval/config_entry, and starts `self.data`
        at `None` exactly like the real base class does before the first
        successful refresh - RadoffCoordinator._merge_device_errors relies
        on that. `Generic[_DataT]` is what lets card S-14's
        `DataUpdateCoordinator[RadoffData]` subscript work here too.
        """

        def __init__(
            self,
            hass,  # noqa: ANN001
            logger,  # noqa: ANN001
            *,
            config_entry=None,  # noqa: ANN001
            name,  # noqa: ANN001
            update_interval=None,  # noqa: ANN001
        ):
            self.hass = hass
            self.logger = logger
            self.config_entry = config_entry
            self.name = name
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True

    ha_helpers_update_coordinator.UpdateFailed = UpdateFailed
    ha_helpers_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_helpers.update_coordinator = ha_helpers_update_coordinator

    ha_components = ModuleType("homeassistant.components")
    ha_components.__path__ = []

    ha_components_sensor = ModuleType("homeassistant.components.sensor")

    class SensorDeviceClass(str, Enum):
        VOLATILE_ORGANIC_COMPOUNDS = "volatile_organic_compounds"
        CO2 = "carbon_dioxide"
        PM10 = "pm10"
        PM25 = "pm25"
        PM1 = "pm1"
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"
        PRESSURE = "pressure"
        AQI = "aqi"

    ha_components_sensor.SensorDeviceClass = SensorDeviceClass
    ha_components.sensor = ha_components_sensor

    ha.core = ha_core
    ha.const = ha_const
    ha.config_entries = ha_config_entries
    ha.exceptions = ha_exceptions
    ha.helpers = ha_helpers
    ha.components = ha_components

    for name, module in {
        "homeassistant": ha,
        "homeassistant.core": ha_core,
        "homeassistant.const": ha_const,
        "homeassistant.config_entries": ha_config_entries,
        "homeassistant.exceptions": ha_exceptions,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.update_coordinator": ha_helpers_update_coordinator,
        "homeassistant.components": ha_components,
        "homeassistant.components.sensor": ha_components_sensor,
    }.items():
        sys.modules[name] = module


_install_homeassistant_stub()
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from custom_components.radoff import coordinator as coordinator_mod  # noqa: E402
from custom_components.radoff.api import client as client_mod  # noqa: E402
from custom_components.radoff.api.exceptions import APIAuthError  # noqa: E402
from custom_components.radoff.api.models import (  # noqa: E402
    Bucket,
    DeviceFetchError,
    RadoffDevice,
    Reading,
)
from custom_components.radoff.const import UPDATE_TIMEOUT_FACTOR  # noqa: E402

API = client_mod.API
RadoffCoordinator = coordinator_mod.RadoffCoordinator
RadoffData = coordinator_mod.RadoffData
ConfigEntryAuthFailed = sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed
UpdateFailed = sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed
ConfigEntry = sys.modules["homeassistant.config_entries"].ConfigEntry
HomeAssistant = sys.modules["homeassistant.core"].HomeAssistant


class FakeResponse:
    """Minimal stand-in for requests.Response, enough for _check_response_status."""

    def __init__(
        self, status_code: int, payload: dict | None = None, url: str = "https://x"
    ):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers: dict[str, str] = {}
        self.url = url

    def json(self):  # noqa: ANN201
        return self._payload


def _make_api() -> API:
    api = API(username="user@example.com", password="pw", domain_id="dom-1")
    api._session.get_bearer = lambda: "fake-bearer-token"  # noqa: SLF001
    return api


def _device_payload(device_id: str, serial: str, name: str = "Now+ device") -> dict:
    return {"id": device_id, "serial": serial, "deviceTypeName": "Now+", "name": name}


def _search_response(devices: list[dict]) -> FakeResponse:
    return FakeResponse(200, {"devices": devices})


def _device_data_response(temperature_raw: float = 2500.0) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "data": {
                "lastDataReceivedAt": "2026-09-07T10:00:00.000Z",
                "data": [
                    {"propertyName": "internal_temperature", "value": temperature_raw},
                ],
            }
        },
    )


# --------------------------------------------------------------------------
# Group A: API.get_devices() per-device isolation
# --------------------------------------------------------------------------


def test_isolates_http_error_on_one_device() -> None:
    api = _make_api()
    devices = [_device_payload("d1", "s1"), _device_payload("d2", "s2")]

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        return _search_response(devices)

    def fake_get(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        if "d1" in url:
            return _device_data_response()
        return FakeResponse(500, {}, url=url)

    api.session.post = fake_post
    api.session.get = fake_get

    devices_ok, device_errors = api.get_devices()

    check("AC: healthy device still returns readings", len(devices_ok) == 1)
    check("AC: healthy device is d1", devices_ok and devices_ok[0].device_id == "d1")
    check("AC: failing device is isolated, not raised", len(device_errors) == 1)
    check(
        "AC: isolated error is for d2",
        device_errors and device_errors[0].device_id == "d2",
    )
    check(
        "Isolated device carries identity for the fallback RadoffDevice",
        device_errors and device_errors[0].device_serial == "s2",
    )


def test_isolates_transport_error_on_one_device() -> None:
    api = _make_api()
    devices = [_device_payload("d1", "s1"), _device_payload("d2", "s2")]

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        return _search_response(devices)

    def fake_get(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        if "d1" in url:
            return _device_data_response()
        raise requests.exceptions.ConnectionError("simulated network failure")

    api.session.post = fake_post
    api.session.get = fake_get

    devices_ok, device_errors = api.get_devices()

    check("Transport error (ConnectionError) is isolated too", len(device_errors) == 1)
    check("The other device still updates", len(devices_ok) == 1)


def test_auth_error_on_one_device_is_not_isolated() -> None:
    """Card S-13 step 6: an auth error on a device must propagate, not isolate."""
    api = _make_api()
    devices = [_device_payload("d1", "s1"), _device_payload("d2", "s2")]

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        return _search_response(devices)

    def fake_get(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        if "d1" in url:
            return _device_data_response()
        return FakeResponse(401, {}, url=url)

    api.session.post = fake_post
    api.session.get = fake_get

    from custom_components.radoff.api.auth import AuthExpiredError

    raised = False
    try:
        api.get_devices()
    except AuthExpiredError:
        raised = True

    check("A 401 on one device raises AuthExpiredError, unisolated", raised)


def test_search_failure_is_never_isolated() -> None:
    api = _make_api()

    def fake_post(url, **kwargs):  # noqa: ANN001, ANN201, ARG001
        return FakeResponse(500, {}, url=url)

    api.session.post = fake_post

    raised = False
    try:
        api.get_devices()
    except APIAuthError:
        raised = True

    check("AC: a search failure raises immediately, before any device loop", raised)


def test_retry_total_is_two() -> None:
    api = _make_api()
    adapter = api.session.get_adapter("https://api.iot.radoff.life/anything")
    check("Card step 4: Retry(total=2)", adapter.max_retries.total == 2)


# --------------------------------------------------------------------------
# Group B: RadoffCoordinator._merge_device_errors
# --------------------------------------------------------------------------


def _make_coordinator(update_interval_seconds: float = 60) -> RadoffCoordinator:
    hass = HomeAssistant()
    entry = ConfigEntry(
        data={
            "username": "user@example.com",
            "password": "pw",
            "domain_id": "dom-1",
        },
        options={},
        unique_id="user@example.com",
        entry_id="entry-1",
    )
    coordinator = RadoffCoordinator(hass, entry)
    coordinator.update_interval = timedelta(seconds=update_interval_seconds)
    return coordinator


def test_merge_reuses_previous_readings_and_marks_stale() -> None:
    coordinator = _make_coordinator()
    reading_key = (Bucket.DATA, "internal_temperature")
    previous_reading = Reading(
        name="internal_temperature",
        bucket=Bucket.DATA,
        value=20.5,
        device_class=None,
        friendly_name="Temperature",
        unit="°C",
        normalize_fn=None,
    )
    previous_device = RadoffDevice(
        device_id="d2",
        device_serial="s2",
        device_type="Now+",
        name="Now+ device",
        readings={reading_key: previous_reading},
        stale=False,
        last_data_received_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
    )
    coordinator.data = RadoffData(
        controller_name="cloud_poller",
        generate_index=True,
        devices=[previous_device],
    )

    error = DeviceFetchError(
        device_id="d2",
        device_serial="s2",
        device_type="Now+",
        name="Now+ device",
        error="500",
    )
    merged = coordinator._merge_device_errors([], [error])  # noqa: SLF001

    check("Merge produces exactly one device", len(merged) == 1)
    merged_device = merged[0]
    check("Merged device is marked stale", merged_device.stale is True)
    check(
        "Merged device keeps the previous readings",
        merged_device.readings == {reading_key: previous_reading},
    )
    check(
        "Merged device keeps last_data_received_at",
        merged_device.last_data_received_at == previous_device.last_data_received_at,
    )
    check(
        "The previous poll's own device object is untouched (dataclasses.replace, not mutation)",
        previous_device.stale is False,
    )


def test_merge_creates_empty_stale_device_with_no_previous_data() -> None:
    coordinator = _make_coordinator()
    coordinator.data = None  # first poll ever

    error = DeviceFetchError(
        device_id="d1",
        device_serial="s1",
        device_type="Now+",
        name="Brand new device",
        error="500",
    )
    merged = coordinator._merge_device_errors([], [error])  # noqa: SLF001

    check("A never-seen-before failing device is still included", len(merged) == 1)
    check("Its readings are empty", merged[0].readings == {})
    check("It is marked stale", merged[0].stale is True)
    check(
        "Its identity is preserved from the search response",
        merged[0].device_id == "d1",
    )


def test_merge_preserves_devices_ok() -> None:
    coordinator = _make_coordinator()
    coordinator.data = None
    healthy = RadoffDevice(
        device_id="d1",
        device_serial="s1",
        device_type="Now+",
        name="Now+ device",
        readings={},
    )
    merged = coordinator._merge_device_errors([healthy], [])  # noqa: SLF001
    check("A healthy device passes through untouched", merged == [healthy])


# --------------------------------------------------------------------------
# Group C: _async_update_data - overall timeout
# --------------------------------------------------------------------------


class FakeHass:
    """Runs `async_add_executor_job` on a real thread pool, like HA does."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=4)

    async def async_add_executor_job(self, func, *args):  # noqa: ANN001, ANN201
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, func, *args)


def test_update_cycle_times_out() -> None:
    coordinator = _make_coordinator(update_interval_seconds=1)
    coordinator.hass = FakeHass()

    def slow_get_devices():  # noqa: ANN201
        time.sleep(2)
        return [], []

    coordinator.api.get_devices = slow_get_devices

    raised_msg = None
    try:
        asyncio.run(coordinator._async_update_data())
    except UpdateFailed as err:
        raised_msg = str(err)

    check(
        "AC: a cycle exceeding 0.8 x update_interval raises UpdateFailed",
        raised_msg is not None,
    )
    check(
        "The UpdateFailed message explicitly mentions the timeout",
        raised_msg is not None and "timed out" in raised_msg,
    )


def test_update_cycle_within_budget_succeeds() -> None:
    coordinator = _make_coordinator(update_interval_seconds=60)
    coordinator.hass = FakeHass()

    healthy = RadoffDevice(
        device_id="d1",
        device_serial="s1",
        device_type="Now+",
        name="Now+ device",
        readings={},
    )
    error = DeviceFetchError(
        device_id="d2",
        device_serial="s2",
        device_type="Now+",
        name="Now+ device 2",
        error="500",
    )
    coordinator.api.get_devices = lambda: ([healthy], [error])

    result = asyncio.run(coordinator._async_update_data())

    check("Successful cycle returns a RadoffData", isinstance(result, RadoffData))
    check(
        "It contains both the healthy and the merged-stale device",
        len(result.devices) == 2,
    )
    stale_device = next(d for d in result.devices if d.device_id == "d2")
    check(
        "The failing device is stale in the returned data", stale_device.stale is True
    )


def test_recovered_device_is_no_longer_stale_next_cycle() -> None:
    """AC: 'Il dispositivo in errore torna disponibile al primo poll riuscito, senza reload.'"""
    coordinator = _make_coordinator(update_interval_seconds=60)
    coordinator.hass = FakeHass()

    error = DeviceFetchError(
        device_id="d1",
        device_serial="s1",
        device_type="Now+",
        name="Now+ device",
        error="500",
    )
    coordinator.api.get_devices = lambda: ([], [error])
    first = asyncio.run(coordinator._async_update_data())
    coordinator.data = first
    check("First cycle: device is stale", first.devices[0].stale is True)

    recovered = RadoffDevice(
        device_id="d1",
        device_serial="s1",
        device_type="Now+",
        name="Now+ device",
        readings={},
    )
    coordinator.api.get_devices = lambda: ([recovered], [])
    second = asyncio.run(coordinator._async_update_data())

    check(
        "Second cycle: device is fresh again, no reload needed",
        second.devices[0].stale is False,
    )


def test_update_timeout_factor_is_point_eight() -> None:
    check("const.UPDATE_TIMEOUT_FACTOR == 0.8", UPDATE_TIMEOUT_FACTOR == 0.8)  # noqa: PLR2004


def main() -> int:
    tests = [
        test_isolates_http_error_on_one_device,
        test_isolates_transport_error_on_one_device,
        test_auth_error_on_one_device_is_not_isolated,
        test_search_failure_is_never_isolated,
        test_retry_total_is_two,
        test_merge_reuses_previous_readings_and_marks_stale,
        test_merge_creates_empty_stale_device_with_no_previous_data,
        test_merge_preserves_devices_ok,
        test_update_cycle_times_out,
        test_update_cycle_within_budget_succeeds,
        test_recovered_device_is_no_longer_stale_next_cycle,
        test_update_timeout_factor_is_point_eight,
    ]
    for test in tests:
        test()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1

    print("All S-13 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
