#!/usr/bin/env python3
"""
Standalone verification for card S-11 (OptionsFlow for polling interval and
index-entity generation), same convention as verify_s07.py/verify_s08.py/
verify_s09.py/verify_s10.py: `homeassistant` is not installed in this
sandbox, so this stubs the minimal surface needed to import the *real*
integration modules (const.py, api/*, config_flow.py, coordinator.py) and
exercise them directly - not a reimplementation of their logic.

Run: `python3 verify_s11.py` from the repo root.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT / "custom_components"))

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> None:  # noqa: FBT001
    """Record one assertion's outcome without stopping the run on failure."""
    global checks  # noqa: PLW0603
    checks += 1
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {description}")
    if not condition:
        failures.append(description)


# --------------------------------------------------------------------------
# Minimal homeassistant stub - only what const.py / api/* / config_flow.py /
# coordinator.py actually import. entity.py and sensor.py are NOT touched by
# S-11 and are deliberately not imported here, so their (larger) dependency
# surface - CoordinatorEntity, DeviceInfo, SensorEntity, ... - is skipped.
# --------------------------------------------------------------------------

ha = types.ModuleType("homeassistant")
ha_core = types.ModuleType("homeassistant.core")
ha_const = types.ModuleType("homeassistant.const")
ha_exceptions = types.ModuleType("homeassistant.exceptions")
ha_config_entries = types.ModuleType("homeassistant.config_entries")
ha_helpers = types.ModuleType("homeassistant.helpers")
ha_helpers_selector = types.ModuleType("homeassistant.helpers.selector")
ha_helpers_update_coordinator = types.ModuleType(
    "homeassistant.helpers.update_coordinator"
)
ha_components = types.ModuleType("homeassistant.components")
ha_components_sensor = types.ModuleType("homeassistant.components.sensor")

# -- homeassistant.core --
ha_core.DOMAIN = "homeassistant"


class HomeAssistant:
    """Stand-in for the real HomeAssistant instance."""


def callback(func):
    """Stand-in for @homeassistant.core.callback - a no-op decorator here."""
    return func


ha_core.HomeAssistant = HomeAssistant
ha_core.callback = callback

# -- homeassistant.const --
ha_const.CONF_USERNAME = "username"
ha_const.CONF_PASSWORD = "password"
ha_const.CONF_SCAN_INTERVAL = "scan_interval"
ha_const.CONF_CLIENT_ID = "client_id"
ha_const.CONCENTRATION_MICROGRAMS_PER_CUBIC_METER = "µg/m³"
ha_const.CONCENTRATION_PARTS_PER_MILLION = "ppm"
ha_const.PERCENTAGE = "%"


class UnitOfPressure:
    PA = "Pa"


class UnitOfTemperature:
    CELSIUS = "°C"


class Platform:
    SENSOR = "sensor"


ha_const.UnitOfPressure = UnitOfPressure
ha_const.UnitOfTemperature = UnitOfTemperature
ha_const.Platform = Platform

# -- homeassistant.exceptions --


class HomeAssistantError(Exception):
    """Stand-in base class."""


class ConfigEntryAuthFailed(HomeAssistantError):
    """Stand-in for the real exception the coordinator raises on bad auth."""


ha_exceptions.HomeAssistantError = HomeAssistantError
ha_exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed

# -- homeassistant.config_entries --


class ConfigEntry:
    """Minimal stand-in carrying exactly what this integration reads."""

    def __init__(
        self, *, data=None, options=None, entry_id="test-entry", unique_id=None
    ):
        self.data = data or {}
        self.options = options or {}
        self.entry_id = entry_id
        self.unique_id = unique_id
        self.version = 2


class _FlowHandlerMixin:
    """
    Reproduces the `async_show_form`/`async_create_entry`/`async_abort` slice
    of HA's real `FlowHandler` base, which both `ConfigFlow` and `OptionsFlow`
    inherit from - `RadoffOptionsFlow.async_step_init` uses all three.
    """

    def async_show_form(self, *, step_id, data_schema=None, errors=None, **kwargs):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            **kwargs,
        }

    def async_create_entry(self, *, title=None, data=None, options=None):
        return {
            "type": "create_entry",
            "title": title,
            "data": data,
            "options": options,
        }

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}


class ConfigFlow(_FlowHandlerMixin):
    """Minimal stand-in reproducing only what config_flow.py relies on."""

    def __init_subclass__(cls, *, domain=None, **kwargs):
        cls.domain = domain
        super().__init_subclass__(**kwargs)


class OptionsFlow(_FlowHandlerMixin):
    """Minimal stand-in - real HA's OptionsFlow base carries much more."""


ConfigFlowResult = dict

ha_config_entries.ConfigEntry = ConfigEntry
ha_config_entries.ConfigFlow = ConfigFlow
ha_config_entries.OptionsFlow = OptionsFlow
ha_config_entries.ConfigFlowResult = ConfigFlowResult

# -- homeassistant.helpers.selector --
# Real HA selectors are callables used as voluptuous validators: `Schema({key:
# NumberSelector(...)})(data)` invokes `NumberSelector.__call__` on the raw
# value. This reproduces just the min/max/coercion behaviour needed to
# exercise S-11 AC "Un valore sotto il minimo viene rifiutato dal form.".

import voluptuous as vol  # noqa: E402


class NumberSelectorMode:
    BOX = "box"
    SLIDER = "slider"


class NumberSelectorConfig(dict):
    """Real HA types this as a TypedDict; a plain dict behaves the same here."""


class NumberSelector:
    def __init__(self, config: NumberSelectorConfig) -> None:
        self.config = config

    def __call__(self, data):
        value = vol.Coerce(float)(data)
        minimum = self.config.get("min")
        maximum = self.config.get("max")
        if minimum is not None and value < minimum:
            msg = f"Value {value} is too small (minimum {minimum})"
            raise vol.Invalid(msg)
        if maximum is not None and value > maximum:
            msg = f"Value {value} is too large (maximum {maximum})"
            raise vol.Invalid(msg)
        return value


class BooleanSelector:
    def __call__(self, data):
        return vol.Coerce(bool)(data) if not isinstance(data, bool) else data


ha_helpers_selector.NumberSelector = NumberSelector
ha_helpers_selector.NumberSelectorConfig = NumberSelectorConfig
ha_helpers_selector.NumberSelectorMode = NumberSelectorMode
ha_helpers_selector.BooleanSelector = BooleanSelector

# -- homeassistant.helpers.update_coordinator --


class UpdateFailed(Exception):
    """Stand-in for the real exception."""


class DataUpdateCoordinator:
    """
    Minimal stand-in reproducing only what RadoffCoordinator.__init__ uses.

    Deliberately does NOT implement `async_config_entry_first_refresh` or any
    polling machinery - this script exercises `__init__` (options resolution,
    `update_interval`, the `API(...)` it constructs), not a live poll cycle.
    """

    def __init__(self, hass, logger, *, name, update_method, update_interval):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_method = update_method
        self.update_interval = update_interval


ha_helpers_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
ha_helpers_update_coordinator.UpdateFailed = UpdateFailed

# -- homeassistant.components.sensor --


class SensorDeviceClass:
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

# Register every stub module before importing any real integration code.
for name, module in {
    "homeassistant": ha,
    "homeassistant.core": ha_core,
    "homeassistant.const": ha_const,
    "homeassistant.exceptions": ha_exceptions,
    "homeassistant.config_entries": ha_config_entries,
    "homeassistant.helpers": ha_helpers,
    "homeassistant.helpers.selector": ha_helpers_selector,
    "homeassistant.helpers.update_coordinator": ha_helpers_update_coordinator,
    "homeassistant.components": ha_components,
    "homeassistant.components.sensor": ha_components_sensor,
}.items():
    sys.modules[name] = module

# botocore/pycognito: only api/auth.py needs these, and only for exception
# types / the AWSSRP class name - never actually instantiated or called by
# this script (no network, no real Cognito handshake).
botocore = types.ModuleType("botocore")
botocore_exceptions = types.ModuleType("botocore.exceptions")


class ClientError(Exception):
    def __init__(self, response=None, *args):
        super().__init__(*args)
        self.response = response or {}


class EndpointConnectionError(Exception):
    pass


botocore_exceptions.ClientError = ClientError
botocore_exceptions.EndpointConnectionError = EndpointConnectionError
sys.modules["botocore"] = botocore
sys.modules["botocore.exceptions"] = botocore_exceptions

pycognito = types.ModuleType("pycognito")
pycognito_aws_srp = types.ModuleType("pycognito.aws_srp")


class AWSSRP:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def authenticate_user(self):  # pragma: no cover - never called here
        msg = "verify_s11.py never performs a real Cognito handshake"
        raise NotImplementedError(msg)


pycognito_aws_srp.AWSSRP = AWSSRP
sys.modules["pycognito"] = pycognito
sys.modules["pycognito.aws_srp"] = pycognito_aws_srp

# --------------------------------------------------------------------------
# Import the REAL integration code, now that its dependencies resolve.
# --------------------------------------------------------------------------

from radoff import const  # noqa: E402
from radoff import config_flow  # noqa: E402
from radoff import coordinator as coordinator_module  # noqa: E402
from radoff.api.client import API  # noqa: E402

print("=== S-11 verification ===\n")

# --------------------------------------------------------------------------
# 1. const.py: MIN/MAX relationship and the T-02-driven bump.
# --------------------------------------------------------------------------

check(
    const.MIN_SCAN_INTERVAL == 30,  # noqa: PLR2004
    "MIN_SCAN_INTERVAL raised from the old dead-code 10s to 30s "
    "(no T-02 answer yet, per this card's own fallback instruction)",
)
check(
    const.MIN_SCAN_INTERVAL < const.DEFAULT_SCAN_INTERVAL < const.MAX_SCAN_INTERVAL,
    "MIN_SCAN_INTERVAL < DEFAULT_SCAN_INTERVAL < MAX_SCAN_INTERVAL holds "
    f"({const.MIN_SCAN_INTERVAL} < {const.DEFAULT_SCAN_INTERVAL} < "
    f"{const.MAX_SCAN_INTERVAL})",
)

# --------------------------------------------------------------------------
# 2. RadoffOptionsFlow.async_get_options_flow wiring (AC: "Configure" button)
# --------------------------------------------------------------------------

check(
    hasattr(config_flow.ConfigPatternFlow, "async_get_options_flow"),
    "ConfigPatternFlow exposes async_get_options_flow (this is what makes "
    "the 'Configure' button appear on the integration's card)",
)

fake_entry = config_flow.ConfigEntry(
    data={"username": "user@example.com", "password": "x", "domain_id": "d1"},
    options={},
)
options_flow = config_flow.ConfigPatternFlow.async_get_options_flow(fake_entry)
check(
    isinstance(options_flow, config_flow.RadoffOptionsFlow),
    "async_get_options_flow(entry) returns a RadoffOptionsFlow instance",
)
check(
    options_flow.config_entry is fake_entry,
    "RadoffOptionsFlow stores the config_entry it was built for",
)

# --------------------------------------------------------------------------
# 3. The init form: defaults reflect current options, and its NumberSelector
#    actually rejects a value below MIN_SCAN_INTERVAL (AC: "Un valore sotto
#    il minimo viene rifiutato dal form.").
# --------------------------------------------------------------------------

import asyncio  # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


entry_with_custom_options = config_flow.ConfigEntry(
    data={}, options={"scan_interval": 300, "generate_index": False}
)
flow_with_options = config_flow.RadoffOptionsFlow(entry_with_custom_options)
form_result = run(flow_with_options.async_step_init(None))
data_schema = form_result["data_schema"]

check(
    form_result["step_id"] == "init",
    "async_step_init() with no input shows the 'init' form",
)

# The schema's marker default is what pre-fills the form; recover it the
# same way voluptuous itself does (iterate Marker keys).
schema_defaults = {}
for marker in data_schema.schema:
    key_name = str(marker)
    default_factory = getattr(marker, "default", None)
    if default_factory is not vol.UNDEFINED and default_factory is not None:
        schema_defaults[key_name] = default_factory()

check(
    schema_defaults.get("scan_interval") == 300,  # noqa: PLR2004
    "the form's scan_interval default reflects the entry's current option (300)",
)
check(
    schema_defaults.get("generate_index") is False,
    "the form's generate_index default reflects the entry's current option (False)",
)

# Below MIN_SCAN_INTERVAL must be rejected...
rejected = False
try:
    data_schema({"scan_interval": const.MIN_SCAN_INTERVAL - 1, "generate_index": True})
except vol.Invalid:
    rejected = True
check(
    rejected,
    f"a scan_interval below MIN_SCAN_INTERVAL "
    f"({const.MIN_SCAN_INTERVAL - 1}s) is rejected by the form's schema",
)

# ...above MAX_SCAN_INTERVAL must be rejected too...
rejected_high = False
try:
    data_schema({"scan_interval": const.MAX_SCAN_INTERVAL + 1, "generate_index": True})
except vol.Invalid:
    rejected_high = True
check(
    rejected_high,
    f"a scan_interval above MAX_SCAN_INTERVAL "
    f"({const.MAX_SCAN_INTERVAL + 1}s) is rejected by the form's schema",
)

# ...and a valid value in range must be accepted and saved via
# async_create_entry (this is what triggers the reload via the existing
# update_listener in __init__.py).
validated = data_schema({"scan_interval": 300, "generate_index": False})
check(
    validated == {"scan_interval": 300.0, "generate_index": False},
    "a valid in-range submission validates cleanly",
)

save_result = run(flow_with_options.async_step_init(validated))
check(
    save_result["type"] == "create_entry" and save_result["data"] == validated,
    "submitting valid options calls async_create_entry(data=...) "
    "(what HA persists into config_entry.options and reloads on)",
)

# --------------------------------------------------------------------------
# 4. coordinator.py actually reads scan_interval/generate_index from
#    options (AC: "Cambiando l'intervallo a 300s, il poll successivo avviene
#    dopo 300s" / "Disattivando generate_index, le entità *_index vengono
#    rimosse al reload").
# --------------------------------------------------------------------------


class _FakeHass:
    pass


entry_300s_no_index = coordinator_module.ConfigEntry(
    data={
        "username": "user@example.com",
        "password": "x",
        "domain_id": "d1",
    },
    options={"scan_interval": 300, "generate_index": False},
    unique_id="user@example.com",
)

coord = coordinator_module.RadoffCoordinator(_FakeHass(), entry_300s_no_index)

check(
    coord.update_interval.total_seconds() == 300,  # noqa: PLR2004
    "RadoffCoordinator.update_interval is 300s when config_entry.options "
    "sets scan_interval=300 (no HA restart needed - just the reload the "
    "update_listener already triggers on save)",
)
check(
    coord.generate_index is False,
    "RadoffCoordinator.generate_index is False when config_entry.options "
    "sets generate_index=False",
)
check(
    coord.stale_after.total_seconds() == 300 * const.DEFAULT_STALE_MULTIPLIER,
    "stale_after keeps tracking update_interval automatically after a "
    "scan_interval change, with no separate option needed (S-11 decision: "
    "the staleness multiplier itself stays an internal constant)",
)
check(
    coord.api.scan_interval == 300,  # noqa: PLR2004
    "the API instance the coordinator builds is told the resolved "
    "scan_interval (300), for the 429 message below",
)

# A second entry with no options set at all must fall back to the defaults
# untouched (an entry that has never opened "Configure").
entry_defaults = coordinator_module.ConfigEntry(
    data={"username": "u2@example.com", "password": "x", "domain_id": "d1"},
    options={},
    unique_id="u2@example.com",
)
coord_defaults = coordinator_module.RadoffCoordinator(_FakeHass(), entry_defaults)
check(
    coord_defaults.update_interval.total_seconds() == const.DEFAULT_SCAN_INTERVAL,
    "an entry with no options set still defaults to DEFAULT_SCAN_INTERVAL "
    "(60s) - S-11 must not change behaviour for existing entries that "
    "never open Configure",
)
check(
    coord_defaults.generate_index is True,
    "an entry with no options set still defaults generate_index to True",
)

# --------------------------------------------------------------------------
# 5. api/client.py: the 429 message names the current interval, never a
#    fixed value equal to the default (AC: "Il messaggio di errore 429 non
#    suggerisce più un valore uguale al default.").
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, url="https://api.iot.radoff.life/x", headers=None):
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}


api_300 = API(username="u", password="p", domain_id="d1", scan_interval=300)
try:
    api_300._check_response_status(_FakeResponse(429))  # noqa: SLF001
    message_300 = None
except Exception as err:  # noqa: BLE001
    message_300 = str(err)

check(
    message_300 is not None and "300" in message_300,
    "the 429 message cites the account's actual current interval (300s)",
)
check(
    message_300 is not None and "60 seconds" not in message_300,
    "the 429 message never hardcodes 'above 60 seconds' any more "
    f"(got: {message_300!r})",
)
check(
    message_300 is not None and "options" in message_300.lower(),
    "the 429 message points at the integration's options, which now "
    "actually exist (finding F6 - before S-11 there was no OptionsFlow "
    "to point to)",
)

api_default = API(username="u", password="p", domain_id="d1")
try:
    api_default._check_response_status(_FakeResponse(429))  # noqa: SLF001
    message_default = None
except Exception as err:  # noqa: BLE001
    message_default = str(err)

check(
    message_default is not None and str(const.DEFAULT_SCAN_INTERVAL) in message_default,
    "an API built without an explicit scan_interval falls back to citing "
    "DEFAULT_SCAN_INTERVAL, never leaving the message with no number at all",
)
check(
    message_300 != message_default,
    "the 429 message differs between two entries with different polling "
    "intervals - i.e. it is genuinely dynamic, not a disguised constant",
)

# --------------------------------------------------------------------------

print(f"\n{checks - len(failures)}/{checks} checks passed.")
if failures:
    print("\nFAILURES:")
    for description in failures:
        print(f" - {description}")
    sys.exit(1)

print("All S-11 checks passed.")
