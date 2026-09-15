"""
From payload to entities: what a device's telemetry and schema produce.

Covers: every reading of the nominal fixture, `unique_id` shape, the entity
set a type declares, availability from `connection_status`, and attributes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.radoff.const import DOMAIN

from .conftest import (
    auth_result,
    load_dev_fixture,
    load_devices_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
    register_measures_ranges,
)

SERIAL = "SER-0000-0001"


@pytest.fixture
async def setup_nominal_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> MockConfigEntry:
    """Set up one config entry against the nominal one-device fixture."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("entity_id", "expected_state", "expected_unit", "expected_device_class"),
    [
        # tvoc: the response declares `V - Ix`, published verbatim - it is
        # not a concentration, so there is no device class, and that is what
        # makes an arbitrary unit string legal.
        # The released version showed µg/m³ here; the change is deliberate
        # and announced in the README, statistics gap included.
        ("sensor.living_room_vocs", "50.0", "V - Ix", None),
        ("sensor.living_room_co2", "600.0", "ppm", "carbon_dioxide"),
        ("sensor.living_room_pm10", "10.0", "µg/m³", "pm10"),
        ("sensor.living_room_pm2_5", "8.0", "µg/m³", "pm25"),
        ("sensor.living_room_pm1", "3.0", "µg/m³", "pm1"),
        ("sensor.living_room_temperature", "20.9", "°C", "temperature"),
        ("sensor.living_room_humidity", "45.0", "%", "humidity"),
        # Declared in Pa, the unit the API sends. Converting to hPa/mbar is
        # the Home Assistant UI's job.
        ("sensor.living_room_pressure", "101325.0", "Pa", "pressure"),
    ],
)
async def test_nominal_reading_entities(
    setup_nominal_entry: MockConfigEntry,
    hass: HomeAssistant,
    entity_id: str,
    expected_state: str,
    expected_unit: str | None,
    expected_device_class: str | None,
) -> None:
    """Every raw-value entity has the expected state, unit and device class."""
    state = hass.states.get(entity_id)
    assert state is not None, f"missing entity {entity_id}"
    assert state.state == expected_state
    assert state.attributes.get("unit_of_measurement") == expected_unit
    assert state.attributes.get("device_class") == expected_device_class


@pytest.mark.parametrize(
    ("entity_id", "expected_state"),
    [
        ("sensor.living_room_vocs_index", "excellent"),  # 50 <= 100 -> "excellent"
        # 500 < 600 <= 1000 -> the second band, which the API calls "high".
        ("sensor.living_room_co2_index", "high"),
        ("sensor.living_room_pm10_index", "excellent"),  # 10 <= 20 -> "excellent"
        ("sensor.living_room_pm2_5_index", "excellent"),  # 8 <= 16 -> "excellent"
        ("sensor.living_room_pm1_index", "excellent"),  # 3 <= 6 -> "excellent"
        # low <= 18 < 20.9 <= 27 -> "good"; the third band is "high"
        ("sensor.living_room_temperature_index", "good"),
        ("sensor.living_room_humidity_index", "good"),  # 40 < 45 <= 60
    ],
)
async def test_index_entities(
    setup_nominal_entry: MockConfigEntry,
    hass: HomeAssistant,
    entity_id: str,
    expected_state: str,
) -> None:
    """Qualitative *_index siblings report the state their thresholds select."""
    state = hass.states.get(entity_id)
    assert state is not None, f"missing entity {entity_id}"
    assert state.state == expected_state
    assert state.attributes.get("device_class") == "enum"


@pytest.mark.parametrize(
    ("entity_id", "expected_unique_id"),
    [
        ("sensor.living_room_temperature", f"{DOMAIN}-{SERIAL}-internal_temperature"),
        ("sensor.living_room_air_quality", f"{DOMAIN}-{SERIAL}-aqi_value"),
        (
            "sensor.living_room_temperature_index",
            f"{DOMAIN}-{SERIAL}-internal_temperature-index",
        ),
    ],
)
async def test_unique_ids_are_serial_and_field(
    setup_nominal_entry: MockConfigEntry,
    hass: HomeAssistant,
    entity_id: str,
    expected_unique_id: str,
) -> None:
    """`unique_id` is `radoff-{serial_number}-{field}`."""
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)

    assert entry is not None, f"missing entity {entity_id}"
    assert entry.unique_id == expected_unique_id


async def test_no_index_entities_when_generate_index_disabled(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """`generate_index=False` (options) suppresses every *_index sibling entity."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": False},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.living_room_temperature") is not None
    assert hass.states.get("sensor.living_room_temperature_index") is None


async def test_a_field_the_schema_does_not_declare_becomes_a_raw_entity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A telemetry field the schema does not declare becomes a bare, unitless entity."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["telemetry"]["radon_status"] = 2
    register_devices(requests_mock, payload)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.living_room_radon_status")
    assert state is not None
    assert state.state == "2"
    assert state.attributes.get("unit_of_measurement") is None
    assert state.attributes.get("device_class") is None
    assert hass.states.get("sensor.living_room_radon_status_index") is None


def _schema_slugs(fixture_name: str, *, with_index: bool) -> set[str]:
    """Entity slugs the given `measures-ranges` fixture implies."""
    schema = load_dev_fixture(fixture_name)
    slugs = set(schema)
    if with_index:
        slugs |= {
            f"{name}_index" for name, measure in schema.items() if measure["ranges"]
        }
    return slugs


# `sismoff` manca da questa lista deliberatamente: le fixture di dev
# contengono il suo *schema* ma nessun device di quel tipo. Aggiungerlo qui
# e' una riga, il giorno che una passata su dev cattura un sismoff: la
# funzione e' gia' parametrica sul tipo.
@pytest.mark.parametrize(
    ("serial", "fixture_name"),
    [
        ("3D90E0", "measures_ranges__nowplus"),
        ("57FA28", "measures_ranges__sense"),
    ],
)
async def test_entities_match_the_measures_the_type_declares(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    serial: str,
    fixture_name: str,
) -> None:
    """A device gets the entities its type declares, even with an empty telemetry block."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_devices(requests_mock, load_dev_fixture("devices__full"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    prefix = f"{DOMAIN}-{serial}-"
    built = {
        registry_entry.unique_id.removeprefix(prefix).removesuffix("-index")
        + ("_index" if registry_entry.unique_id.endswith("-index") else "")
        for registry_entry in registry.entities.values()
        if registry_entry.unique_id.startswith(prefix)
    }

    assert built == _schema_slugs(fixture_name, with_index=True)


async def test_an_unknown_device_type_keeps_the_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A device type the catalogue does not know keeps its device and its telemetry."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["type"] = "not-a-real-device-type"
    register_devices(requests_mock, payload)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert [device.serial_number for device in entry.runtime_data.data.devices] == [
        SERIAL
    ]
    assert entry.runtime_data.schemas["not-a-real-device-type"] == {}

    state = hass.states.get("sensor.living_room_eco2")
    assert state is not None
    assert state.state == "600.0"
    assert state.attributes.get("unit_of_measurement") is None
    assert hass.states.get("sensor.living_room_eco2_index") is None


async def test_the_aqi_entities_are_created_disabled(
    setup_nominal_entry: MockConfigEntry,
    hass: HomeAssistant,
) -> None:
    """The AQI entity and its qualitative sibling are created disabled."""
    registry = er.async_get(hass)

    for entity_id in (
        "sensor.living_room_air_quality",
        "sensor.living_room_air_quality_index",
    ):
        registry_entry = registry.async_get(entity_id)
        assert registry_entry is not None, f"missing entity {entity_id}"
        assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None


async def test_a_unit_outside_the_map_costs_the_unit_not_the_setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unmappable unit produces a WARNING and an entity without a unit."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    schema = load_dev_fixture("measures_ranges__nowplus")
    schema["eco2"]["unit"] = "parsecs per fortnight"
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    register_measures_ranges(requests_mock, payloads={"nowplus": schema})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.living_room_co2")
    assert state is not None
    assert state.state == "600.0"
    assert state.attributes.get("unit_of_measurement") is None

    assert "parsecs per fortnight" in caplog.text
    assert any(
        record.levelname == "WARNING" and "parsecs per fortnight" in record.message
        for record in caplog.records
    )


# --------------------------------------------------------------------------
# Availability comes from `connection_status`
# --------------------------------------------------------------------------
#
# One helper for this group instead of the inline boilerplate the tests
# above repeat: every case below is "set the entry up against a payload
# with one field changed", and six copies of the same fifteen lines would
# bury the one line that differs.


async def _setup_with(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    payload: dict[str, Any],
) -> MockConfigEntry:
    """Set one entry up against `payload` as the device list."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_devices(requests_mock, payload)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("connection_status", "expected_available"),
    [
        ("connected", True),
        # Not connected, with a full and fresh telemetry block - the
        # fixture's - and unavailable all the same. Availability is the
        # connection, not the data.
        ("disconnected", False),
        # A value from outside the enumeration we have. Available, because
        # "not connected" is a claim we can only make about a value we
        # recognise.
        ("evaporated", True),
        # Same string as the first case, spelled differently. Matched, not
        # treated as new: see `classify_connection_status`.
        ("CONNECTED", True),
        # No field at all: nothing is known, so nothing is asserted.
        (None, True),
    ],
)
async def test_availability_follows_connection_status(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    connection_status: str | None,
    expected_available: bool,
) -> None:
    """An entity is unavailable when its device says `disconnected`, and at no other time."""
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["connection_status"] = connection_status

    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v3_data, payload)

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert (state.state != "unavailable") is expected_available
    if expected_available:
        # Available *and* showing the value, not merely not-unavailable.
        assert state.state == "20.9"


async def test_an_unknown_connection_status_warns_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unseen `connection_status` warns once per value, not once per device."""
    payload = load_devices_fixture("devices_two_devices.json")
    for device in payload["devices"]:
        device["connection_status"] = "evaporated"

    caplog.clear()
    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v3_data, payload)

    warnings = [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "evaporated" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "KNOWN_CONNECTION_STATUSES" in warnings[0].getMessage()

    for entity_id in (
        "sensor.living_room_temperature",
        "sensor.bedroom_temperature",
    ):
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.state != "unavailable"


async def test_the_diagnostic_attributes_carry_both_timestamps(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """The entity carries the measurement time, the status time and both status fields."""
    payload = load_devices_fixture("devices_one_device.json")
    telemetry_timestamp = payload["devices"][0]["telemetry"]["timestamp"]
    connection_timestamp = payload["devices"][0]["connection_status_updated_at"]

    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v3_data, payload)

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert state.attributes["last_measured_at"] == telemetry_timestamp
    assert state.attributes["connection_status_updated_at"] == connection_timestamp
    assert state.attributes["connection_status"] == "connected"
    assert state.attributes["status"] == "active"


async def test_a_restart_restores_the_last_known_value(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A restart restores the last known value together with the timestamp it arrived with."""
    restored_measured_at = "2026-09-07T08:15:00+00:00"
    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(
                    "sensor.living_room_temperature",
                    "19.5",
                    attributes={"last_measured_at": restored_measured_at},
                ),
                {"native_value": 19.5, "native_unit_of_measurement": "°C"},
            ),
        ),
    )

    payload = load_devices_fixture("devices_no_telemetry.json")
    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v3_data, payload)

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert state.state == "19.5"
    assert state.attributes["last_measured_at"] == restored_measured_at


async def test_a_connected_device_with_no_status_timestamp_is_available(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A missing `connection_status_updated_at` drops the attribute and nothing else."""
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["connection_status_updated_at"] = None

    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v3_data, payload)

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert state.state == "20.9"
    assert "connection_status_updated_at" not in state.attributes


def test_the_staleness_multiplier_is_gone_from_the_codebase() -> None:
    """No `DEFAULT_STALE_MULTIPLIER` survives in the integration, comments included."""
    integration = Path("custom_components/radoff")
    offenders = [
        path.name
        for path in sorted(integration.rglob("*.py"))
        if "DEFAULT_STALE_MULTIPLIER" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
