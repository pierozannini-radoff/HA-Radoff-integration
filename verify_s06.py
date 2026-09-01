"""
Manual verification harness for card S-06, run without Home Assistant.

It stubs out just enough of the `homeassistant` package for
custom_components/radoff/{coordinator,sensor}.py and __init__.py to import
and run, then exercises the acceptance criteria:

1. Two config entries: unloading the first must not remove the second's data
   from hass.data.
2. A device disappearing from the coordinator's data (get_device_by_id
   returns None) must not raise AttributeError in _handle_coordinator_update
   / native_value, and the entity must keep serving its last known device.
3. A property missing from device.sensors for one poll must not raise
   KeyError in native_value.
4. async_forward_entry_unload must not be called at all (no deprecation
   warning possible).
5. A setup/unload/setup cycle must leave hass.data[DOMAIN] with exactly one
   entry, matching the entry_id, no residue.
"""

import sys
import types
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "custom_components"))


def _install_ha_stubs() -> None:
    ha = types.ModuleType("homeassistant")

    const = types.ModuleType("homeassistant.const")
    const.CONF_CLIENT_ID = "client_id"
    const.CONF_PASSWORD = "password"
    const.CONF_USERNAME = "username"
    const.CONF_SCAN_INTERVAL = "scan_interval"

    class Platform:
        SENSOR = "sensor"

    const.Platform = Platform

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        def __init__(self, entry_id, data, options=None, unique_id=None):
            self.entry_id = entry_id
            self.data = data
            self.options = options or {}
            self.unique_id = unique_id

        def add_update_listener(self, _listener):
            return lambda: None

    config_entries.ConfigEntry = ConfigEntry

    core = types.ModuleType("homeassistant.core")
    core.DOMAIN = "homeassistant"

    class HomeAssistant:
        def __init__(self):
            self.data = {}

        async def async_add_executor_job(self, func, *args):
            return func(*args)

    def callback(func):
        return func

    core.HomeAssistant = HomeAssistant
    core.callback = callback

    exceptions = types.ModuleType("homeassistant.exceptions")

    class ConfigEntryNotReady(Exception):
        pass

    exceptions.ConfigEntryNotReady = ConfigEntryNotReady

    helpers = types.ModuleType("homeassistant.helpers")
    helpers_update_coordinator = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        def __init__(self, hass, logger, name=None, update_method=None,
                     update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.data = None

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        async def async_refresh(self):
            self.data = await self.update_method()

    class CoordinatorEntity:
        def __init__(self, coordinator, context=None):
            self.coordinator = coordinator
            self._context = context

    helpers_update_coordinator.UpdateFailed = UpdateFailed
    helpers_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    helpers_update_coordinator.CoordinatorEntity = CoordinatorEntity

    helpers_device_registry = types.ModuleType("homeassistant.helpers.device_registry")

    class DeviceInfo(dict):
        pass

    class DeviceEntry:
        pass

    helpers_device_registry.DeviceInfo = DeviceInfo
    helpers_device_registry.DeviceEntry = DeviceEntry

    helpers_entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")

    class AddEntitiesCallback:
        pass

    helpers_entity_platform.AddEntitiesCallback = AddEntitiesCallback

    components = types.ModuleType("homeassistant.components")
    components_sensor = types.ModuleType("homeassistant.components.sensor")

    class SensorDeviceClass:
        TEMPERATURE = "temperature"

    class SensorEntity:
        pass

    class SensorStateClass:
        MEASUREMENT = "measurement"

    components_sensor.SensorDeviceClass = SensorDeviceClass
    components_sensor.SensorEntity = SensorEntity
    components_sensor.SensorStateClass = SensorStateClass
    components_sensor.DEVICE_CLASS_UNITS = {}

    modules = {
        "homeassistant": ha,
        "homeassistant.const": const,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": helpers_update_coordinator,
        "homeassistant.helpers.device_registry": helpers_device_registry,
        "homeassistant.helpers.entity_platform": helpers_entity_platform,
        "homeassistant.components": components,
        "homeassistant.components.sensor": components_sensor,
    }
    for name, mod in modules.items():
        sys.modules[name] = mod


def _install_radoff_api_stub() -> None:
    """
    Stand in for the real `custom_components/radoff/api.py`.

    api.py is out of scope for card S-06 (untouched by this fix) and pulls in
    `pycognito`/live HA sensor-unit tables not available in this harness. Only
    the small surface `sensor.py`/`coordinator.py` actually import is stubbed
    here; the real api.py is not modified nor exercised by this script.
    """
    api_mod = types.ModuleType("radoff.api")

    @dataclass
    class RadoffSensor:
        name: str
        value: object
        device_class: object
        friendly_name: str
        unit: object
        normalize_fn: object

    @dataclass
    class Device:
        device_id: str
        device_serial: str
        device_type: str
        name: str
        sensors: dict = field(default_factory=dict)

    class APIAuthError(Exception):
        pass

    class API:
        DEFAULT_TIMEOUT = (10, 30)

        def __init__(self, username, password, domain_id=""):
            self.username = username
            self.password = password
            self.domain = domain_id
            self.connected = False
            self.controller_name = "cloud_poller"

        def connect(self):
            self.connected = True
            return True

        def get_devices(self):
            return []

    api_mod.RadoffSensor = RadoffSensor
    api_mod.Device = Device
    api_mod.APIAuthError = APIAuthError
    api_mod.API = API
    sys.modules["radoff.api"] = api_mod


_install_ha_stubs()
_install_radoff_api_stub()

from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402

import radoff  # noqa: E402
from radoff.coordinator import APIData, RadoffCoordinator  # noqa: E402
from radoff.api import Device, RadoffSensor as ApiRadoffSensor  # noqa: E402
from radoff.sensor import RadoffSensor  # noqa: E402
from radoff.const import DOMAIN  # noqa: E402


def make_device(device_id="dev-1", value=21.0):
    return Device(
        device_id=device_id,
        device_serial=f"serial-{device_id}",
        device_type="Now+",
        name=f"Device {device_id}",
        sensors={
            "internal_temperature": ApiRadoffSensor(
                name="internal_temperature",
                value=value,
                device_class="temperature",
                friendly_name="Temperature",
                unit="°C",
                normalize_fn=None,
            )
        },
    )


class FakeCoordinator:
    """Minimal stand-in exposing only what RadoffSensor needs from it."""

    def __init__(self, devices):
        self.data = APIData(controller_name="c", generate_index=True, devices=devices)

    def get_device_by_id(self, device_type, device_id):
        for d in self.data.devices:
            if d.device_type == device_type and d.device_id == device_id:
                return d
        return None


def test_c1_device_disappears_no_attributeerror():
    device = make_device()
    coordinator = FakeCoordinator([device])
    entity = RadoffSensor(
        sensor_key="internal_temperature",
        device=device,
        coordinator_context=coordinator,
        device_class=None,
        friendly_name="Temperature",
        normalize_fn=None,
        unit="°C",
        index_fn=None,
        is_index=False,
    )
    entity.async_write_ha_state = lambda: None

    # Simulate the device disappearing from the next poll's payload.
    coordinator.data.devices = []

    entity._handle_coordinator_update()  # noqa: SLF001
    value = entity.native_value  # must not raise
    info = entity.device_info  # must not raise

    assert entity.device is device, "entity should keep last known device"
    assert value == 21.0
    assert info["name"] == device.name
    print("C1 OK: device disappearance does not raise, entity stays coherent")


def test_c2_missing_property_returns_none():
    device = make_device()
    coordinator = FakeCoordinator([device])
    entity = RadoffSensor(
        sensor_key="internal_temperature",
        device=device,
        coordinator_context=coordinator,
        device_class=None,
        friendly_name="Temperature",
        normalize_fn=None,
        unit="°C",
        index_fn=None,
        is_index=False,
    )

    # Simulate a poll where this property has no sample yet.
    device.sensors = {}

    value = entity.native_value  # must not raise KeyError
    assert value is None
    print("C2 OK: missing property returns None instead of KeyError")


def test_c3_unload_one_entry_keeps_other():
    hass = HomeAssistant()
    hass.data[DOMAIN] = {
        "entry-A": types.SimpleNamespace(cancel_update_listener=lambda: None),
        "entry-B": types.SimpleNamespace(cancel_update_listener=lambda: None),
    }

    class FakeConfigEntries:
        async def async_unload_platforms(self, config_entry, platforms):
            return True

    hass.config_entries = FakeConfigEntries()

    entry_a = ConfigEntry("entry-A", data={})

    asyncio.run(radoff.async_unload_entry(hass, entry_a))

    assert "entry-B" in hass.data[DOMAIN], "second entry must survive unloading the first"
    assert "entry-A" not in hass.data[DOMAIN]
    print("C3 OK: unloading one entry does not remove the other")


def test_setup_unload_setup_no_residue():
    hass = HomeAssistant()

    device = make_device()

    class FakeAPI:
        connected = True
        controller_name = "c"

        def connect(self):
            self.connected = True
            return True

        def get_devices(self):
            return [device]

    class FakeCoordinatorFull(RadoffCoordinator):
        def __init__(self, hass, config_entry):  # noqa: ARG002
            self.hass = hass
            self.api = FakeAPI()
            self.generate_index = True
            self.data = None

        async def async_config_entry_first_refresh(self):
            self.data = APIData(
                controller_name="c", generate_index=True, devices=[device]
            )

    original_coordinator_cls = radoff.RadoffCoordinator
    radoff.RadoffCoordinator = FakeCoordinatorFull

    calls = {"forward": 0, "unload": 0}

    class FakeConfigEntries:
        async def async_forward_entry_setups(self, config_entry, platforms):
            calls["forward"] += 1
            assert platforms == radoff.PLATFORMS

        async def async_unload_platforms(self, config_entry, platforms):
            calls["unload"] += 1
            assert platforms == radoff.PLATFORMS
            return True

    hass.config_entries = FakeConfigEntries()
    entry = ConfigEntry("entry-1", data={})

    try:
        asyncio.run(radoff.async_setup_entry(hass, entry))
        assert list(hass.data[DOMAIN].keys()) == ["entry-1"]

        asyncio.run(radoff.async_unload_entry(hass, entry))
        assert DOMAIN not in hass.data, "domain key must be gone once empty"

        asyncio.run(radoff.async_setup_entry(hass, entry))
        assert list(hass.data[DOMAIN].keys()) == ["entry-1"]
    finally:
        radoff.RadoffCoordinator = original_coordinator_cls

    assert calls["forward"] == 2
    assert calls["unload"] == 1
    print(
        "Setup/unload/setup cycle OK: no residue in hass.data, "
        "async_unload_platforms used (no deprecated async_forward_entry_unload)"
    )


def test_c17_get_device_by_id_no_dead_except():
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(RadoffCoordinator.get_device_by_id))
    tree = ast.parse(src)
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
    ]
    assert not handlers, "get_device_by_id should have no except clause left"
    print("C17 OK: dead except IndexError removed from get_device_by_id")


def test_c19_apidata_not_dict_subclass():
    assert not issubclass(APIData, dict)
    print("C19 OK: APIData no longer inherits from dict")


if __name__ == "__main__":
    test_c1_device_disappears_no_attributeerror()
    test_c2_missing_property_returns_none()
    test_c3_unload_one_entry_keeps_other()
    test_setup_unload_setup_no_residue()
    test_c17_get_device_by_id_no_dead_except()
    test_c19_apidata_not_dict_subclass()
    print("\nAll S-06 acceptance criteria verified.")