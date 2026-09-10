"""
Sensor entity tests (S-18, updated by card M-03): payload -> expected entities.

Complements `test_coordinator.py`'s poll tests: this file asserts on every
reading of the nominal fixture (unit, device class, value, and - for the
qualitative siblings - index state), not just the handful already used to
prove the poll pipeline works end to end.

Two things this card changes are pinned here rather than described: the
`unique_id` shape (`radoff-{serial_number}-{field}`) and the AQI, which is
one entity fed by `telemetry.aqi_value` - the bucket suffix S-10 needed is
gone with the buckets.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import DOMAIN

from .conftest import (
    auth_result,
    load_devices_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
)

SERIAL = "SER-0000-0001"


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
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
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
        ("sensor.living_room_vocs", "50.0", "µg/m³", "volatile_organic_compounds"),
        ("sensor.living_room_co2", "600.0", "ppm", "carbon_dioxide"),
        ("sensor.living_room_pm10", "10.0", "µg/m³", "pm10"),
        ("sensor.living_room_pm2_5", "8.0", "µg/m³", "pm25"),
        ("sensor.living_room_pm1", "3.0", "µg/m³", "pm1"),
        ("sensor.living_room_temperature", "20.9", "°C", "temperature"),
        ("sensor.living_room_humidity", "45.0", "%", "humidity"),
        ("sensor.living_room_pressure", "101325.0", "Pa", "pressure"),
        # One AQI entity, fed by `telemetry.aqi_value` (card M-03). The
        # `_average` suffix S-10 gave it belonged to a bucket that no
        # longer exists.
        ("sensor.living_room_air_quality", "1.15", None, "aqi"),
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
    """
    M-03 AC: `unique_id` is `radoff-{serial_number}-{field}`.

    Both halves are new - the serial replaces the arch 1.x device UUID,
    which has no counterpart in 2.0 (T-02 D-02), and the suffix is the
    telemetry field name. Re-keying the entities existing installations
    already have onto this form is card M-07, not this one, which is why
    nothing here asserts on a migration.
    """
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)

    assert entry is not None, f"missing entity {entity_id}"
    assert entry.unique_id == expected_unique_id


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
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
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


async def test_a_field_with_no_descriptor_creates_no_entity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A telemetry field the provisional table does not describe is skipped.

    It still becomes a `Reading` (and reaches diagnostics) - what it does
    not get is an entity, because there is no unit, device class or name to
    give it until card M-04 brings the API's own schema. A radon field on a
    device that has one is the case this will meet first.
    """
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["telemetry"]["radon"] = 42.0
    register_devices(requests_mock, payload)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.living_room_radon") is None
    device = entry.runtime_data.data.devices[0]
    assert device.readings["radon"].value == 42.0
