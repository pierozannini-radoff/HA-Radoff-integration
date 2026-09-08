"""
Coordinator poll tests (S-18): nominal payload and degraded conditions.

Every test sets up a real config entry end to end (`async_setup_entry` ->
`RadoffCoordinator.async_config_entry_first_refresh` -> `sensor.py`'s
platform setup), with the Radoff API mocked at the transport level - the
same path Home Assistant itself exercises on every startup and poll.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import DOMAIN

from .conftest import (
    load_device_fixture,
    load_fixture,
    make_id_token,
    auth_result,
    patch_authenticate_user,
    register_device,
    register_search,
)


async def _setup_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v2_data: dict[str, Any],
) -> MockConfigEntry:
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
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


async def test_poll_nominal_produces_expected_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A nominal payload produces the expected entities, with specific values.

    Asserts on concrete unit, device class, index state and value - not just
    "the entity exists" (S-18 AC: "asserzioni su valori specifici").
    """
    register_search(requests_mock, load_fixture("search_one_device.json"))
    register_device(
        requests_mock,
        "device-0000-0001",
        load_device_fixture("device_0001_nominal.json"),
    )

    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

    temperature = hass.states.get("sensor.living_room_temperature")
    assert temperature is not None
    assert temperature.state == "20.9"  # round(2500 * 0.00835, 1)
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert temperature.attributes["device_class"] == "temperature"

    temperature_index = hass.states.get("sensor.living_room_temperature_index")
    assert temperature_index is not None
    assert temperature_index.state == "good"  # 18.0 < 20.9 <= 27.0
    assert temperature_index.attributes["device_class"] == "enum"

    humidity = hass.states.get("sensor.living_room_humidity")
    assert humidity is not None
    assert humidity.state == "45"
    assert humidity.attributes["device_class"] == "humidity"

    aqi_average = hass.states.get("sensor.living_room_air_quality_average")
    assert aqi_average is not None
    assert aqi_average.state == "25"


async def test_poll_degraded_device_missing_from_search(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """A device present on setup but absent from a later search goes unavailable."""
    register_search(requests_mock, load_fixture("search_one_device.json"))
    register_device(
        requests_mock,
        "device-0000-0001",
        load_device_fixture("device_0001_nominal.json"),
    )
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)
    assert hass.states.get("sensor.living_room_temperature").state != "unavailable"

    register_search(requests_mock, {"devices": []})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.living_room_temperature").state == "unavailable"


async def test_poll_degraded_missing_property_only_affects_that_reading(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """A property missing from one sample only affects that one entity."""
    register_search(requests_mock, load_fixture("search_one_device.json"))
    register_device(
        requests_mock,
        "device-0000-0001",
        load_device_fixture("device_0001_missing_property.json"),
    )

    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

    assert hass.states.get("sensor.living_room_temperature") is None
    humidity = hass.states.get("sensor.living_room_humidity")
    assert humidity is not None
    assert humidity.state == "45"


async def test_poll_degraded_500_isolated_to_one_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A 500 on one device's GET makes only that device's entities unavailable.

    Both devices must succeed on the FIRST poll (so their entities actually
    get created by `sensor.py`'s one-shot platform setup, which only builds
    entities for readings present in that first snapshot) - only the SECOND
    poll cycle isolates the 500 to the bedroom device, per `_merge_device_
    errors`' "reuse previous readings, flip stale=True" behaviour.
    """
    register_search(requests_mock, load_fixture("search_two_devices.json"))
    register_device(
        requests_mock,
        "device-0000-0001",
        load_device_fixture("device_0001_nominal.json"),
    )
    register_device(
        requests_mock,
        "device-0000-0002",
        load_device_fixture("device_0002_nominal.json"),
    )
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)
    assert hass.states.get("sensor.bedroom_temperature").state != "unavailable"

    register_device(requests_mock, "device-0000-0002", status_code=500)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    living_room = hass.states.get("sensor.living_room_temperature")
    assert living_room is not None
    assert living_room.state != "unavailable"

    bedroom = hass.states.get("sensor.bedroom_temperature")
    assert bedroom is not None
    assert bedroom.state == "unavailable"
