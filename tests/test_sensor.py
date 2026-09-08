"""
Sensor entity tests (S-18): nominal payload -> expected entities.

Complements `test_coordinator.py`'s poll tests: this file asserts on every
reading of the nominal fixture (unit, device class, name, and - for the
qualitative siblings - index state), not just the handful already used to
prove the poll pipeline works end to end.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import DOMAIN

from .conftest import (
    auth_result,
    load_device_fixture,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    register_device,
    register_search,
)


@pytest.fixture
async def setup_nominal_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> MockConfigEntry:
    """Set up one config entry against the nominal one-device fixture."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_search(requests_mock, load_fixture("search_one_device.json"))
    register_device(
        requests_mock,
        "device-0000-0001",
        load_device_fixture("device_0001_nominal.json"),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("entity_id", "expected_state", "expected_unit", "expected_device_class"),
    [
        ("sensor.living_room_vocs", "50", "µg/m³", "volatile_organic_compounds"),
        ("sensor.living_room_co2", "600", "ppm", "carbon_dioxide"),
        ("sensor.living_room_pm10", "10", "µg/m³", "pm10"),
        ("sensor.living_room_pm2_5", "8", "µg/m³", "pm25"),
        ("sensor.living_room_pm1", "3", "µg/m³", "pm1"),
        ("sensor.living_room_temperature", "20.9", "°C", "temperature"),
        ("sensor.living_room_humidity", "45", "%", "humidity"),
        ("sensor.living_room_pressure", "101325", "Pa", "pressure"),
        ("sensor.living_room_air_quality", "20", None, "aqi"),
        ("sensor.living_room_air_quality_average", "25", None, "aqi"),
    ],
)
async def test_nominal_reading_entities(
    setup_nominal_entry: MockConfigEntry,
    hass: HomeAssistant,
    entity_id: str,
    expected_state: str,
    expected_unit: str | None,
    expected_device_class: str,
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
        ("sensor.living_room_co2_index", "good"),  # 500 < 600 <= 1000 -> "good"
        ("sensor.living_room_pm10_index", "excellent"),  # 10 <= 20 -> "excellent"
        ("sensor.living_room_pm2_5_index", "excellent"),  # 8 <= 16 -> "excellent"
        ("sensor.living_room_pm1_index", "excellent"),  # 3 <= 6 -> "excellent"
        ("sensor.living_room_temperature_index", "good"),  # 18 < 20.9 <= 27
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


async def test_no_index_entities_when_generate_index_disabled(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """`generate_index=False` (options) suppresses every *_index sibling entity."""
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_search(requests_mock, load_fixture("search_one_device.json"))
    register_device(
        requests_mock,
        "device-0000-0001",
        load_device_fixture("device_0001_nominal.json"),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v2_data,
        options={"generate_index": False},
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.living_room_temperature") is not None
    assert hass.states.get("sensor.living_room_temperature_index") is None
