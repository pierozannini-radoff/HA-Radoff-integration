"""
The poll cycle, end to end through a real config entry.

Covers: the nominal poll, degraded devices and failed cycles, and the
scheduling - jitter, backoff, and what a 429 does to the next cycle.
Which status raises which error is the other half, in `test_api_transport.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import (
    CONF_BASE_URL,
    CONF_DOMAIN_PREFIX,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ERROR_DOMAIN_ACCESS_DENIED,
    POLL_JITTER_FRACTION,
)
from custom_components.radoff.coordinator import RadoffCoordinator

from .conftest import (
    auth_result,
    load_dev_fixture,
    load_devices_fixture,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
)


# Same synthetic body as `test_api_transport.py`: API Gateway's own 429,
# which carries neither an `error` key nor a `Retry-After` header. Not a
# captured fixture - provoking a real one would have meant saturating a
# quota shared with the Radoff mobile app.
RATE_LIMIT_BODY = {"message": "Too Many Requests"}


async def _setup_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v3_data: dict[str, Any],
) -> MockConfigEntry:
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
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


async def _repoll(hass: HomeAssistant) -> None:
    """Run one more coordinator cycle against whatever is registered now."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_coordinator_without_a_domain_prefix_raises_config_entry_error(
    hass: HomeAssistant,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A coordinator built without a `domain_prefix` raises `ConfigEntryError`, not `KeyError`."""
    data = {k: v for k, v in config_entry_v3_data.items() if k != CONF_DOMAIN_PREFIX}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=3)
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError) as err:
        RadoffCoordinator(hass, entry)

    assert err.value.translation_key == "missing_domain_prefix"


async def test_poll_nominal_produces_expected_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A nominal payload produces the expected entities, with unscaled values."""
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    await _setup_entry(hass, monkeypatch, config_entry_v3_data)

    temperature = hass.states.get("sensor.living_room_temperature")
    assert temperature is not None
    assert temperature.state == "20.9"
    assert temperature.attributes["unit_of_measurement"] == "°C"
    assert temperature.attributes["device_class"] == "temperature"

    temperature_index = hass.states.get("sensor.living_room_temperature_index")
    assert temperature_index is not None
    assert temperature_index.state == "good"  # 18.0 < 20.9 <= 27.0
    assert temperature_index.attributes["device_class"] == "enum"

    humidity = hass.states.get("sensor.living_room_humidity")
    assert humidity is not None
    assert humidity.state == "45.0"
    assert humidity.attributes["device_class"] == "humidity"

    # The AQI entity is created disabled - the backend computes its
    # temperature component with the wrong divisor - so there is no state
    # to poll. `test_sensor.py` asserts on it via the registry.
    assert hass.states.get("sensor.living_room_air_quality") is None


async def test_the_device_registry_entry_carries_the_firmware_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """`firmware_version` reaches `device_info.sw_version` in the device registry."""
    from homeassistant.helpers import device_registry as dr

    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v3_data)

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, "SER-0000-0001")})

    assert device is not None
    assert device.sw_version == "0.2.8"


async def test_poll_degraded_device_missing_from_the_list(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A device present on setup but absent from a later poll goes unavailable."""
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v3_data)
    assert hass.states.get("sensor.living_room_temperature").state != "unavailable"

    register_devices(requests_mock, load_fixture("devices_empty.json"))
    await _repoll(hass)

    assert hass.states.get("sensor.living_room_temperature").state == "unavailable"


async def test_poll_degraded_missing_field_only_affects_that_reading(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A field missing from the telemetry block leaves its sibling entities untouched."""
    register_devices(requests_mock, load_devices_fixture("devices_missing_field.json"))

    await _setup_entry(hass, monkeypatch, config_entry_v3_data)

    temperature = hass.states.get("sensor.living_room_temperature")
    assert temperature is not None
    assert temperature.state == "unknown"

    humidity = hass.states.get("sensor.living_room_humidity")
    assert humidity is not None
    assert humidity.state == "45.0"


async def test_a_connected_device_without_telemetry_keeps_its_entities_available(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A `connected` device with `telemetry: null` keeps available entities, state `unknown`."""
    register_devices(requests_mock, load_devices_fixture("devices_no_telemetry.json"))

    entry = await _setup_entry(hass, monkeypatch, config_entry_v3_data)

    assert entry.state is ConfigEntryState.LOADED
    temperature = hass.states.get("sensor.living_room_temperature")
    assert temperature is not None
    assert temperature.state == "unknown"
    assert "last_measured_at" not in temperature.attributes
    assert temperature.attributes["connection_status"] == "connected"

    coordinator = entry.runtime_data
    assert [device.stale for device in coordinator.data.devices] == [True]


async def test_a_device_falling_silent_only_affects_its_own_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A device falling silent keeps its last value and timestamp; the other one updates."""
    two_devices = load_devices_fixture("devices_two_devices.json")
    register_devices(requests_mock, two_devices)
    await _setup_entry(hass, monkeypatch, config_entry_v3_data)

    bedroom_before = hass.states.get("sensor.bedroom_temperature")
    assert bedroom_before.state != "unavailable"
    measured_at_before = bedroom_before.attributes["last_measured_at"]

    silent = load_devices_fixture("devices_two_devices.json")
    silent["devices"][1]["telemetry"] = None
    register_devices(requests_mock, silent)
    await _repoll(hass)

    living_room = hass.states.get("sensor.living_room_temperature")
    assert living_room is not None
    assert living_room.state != "unavailable"

    bedroom = hass.states.get("sensor.bedroom_temperature")
    assert bedroom is not None
    assert bedroom.state == bedroom_before.state
    assert bedroom.attributes["last_measured_at"] == measured_at_before
    # The device that did report has moved on: the two timestamps are the
    # evidence that one value is fresh and the other is being remembered.
    assert living_room.attributes["last_measured_at"] != measured_at_before


async def test_a_5xx_costs_the_whole_cycle(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A 5xx costs the whole cycle: every entity goes unavailable."""
    register_devices(requests_mock, load_devices_fixture("devices_two_devices.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v3_data)
    assert hass.states.get("sensor.bedroom_temperature").state != "unavailable"

    register_devices(
        requests_mock, {"message": "Internal Server Error"}, status_code=500
    )
    await _repoll(hass)

    assert hass.states.get("sensor.living_room_temperature").state == "unavailable"
    assert hass.states.get("sensor.bedroom_temperature").state == "unavailable"


async def test_a_status_outside_the_taxonomy_costs_the_whole_cycle(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A status the taxonomy does not classify costs the whole cycle, like a 5xx."""
    register_devices(requests_mock, load_devices_fixture("devices_two_devices.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v3_data)
    assert hass.states.get("sensor.bedroom_temperature").state != "unavailable"

    register_devices(requests_mock, {"error": "teapot"}, status_code=418)
    await _repoll(hass)

    assert hass.states.get("sensor.living_room_temperature").state == "unavailable"
    assert hass.states.get("sensor.bedroom_temperature").state == "unavailable"


async def test_a_403_stops_the_entry_instead_of_retrying_forever(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A 403 stops the entry with `ConfigEntryError`, and opens no re-auth prompt."""
    register_devices(
        requests_mock,
        load_dev_fixture("error__devices_foreign_domain"),
        status_code=403,
    )

    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == ERROR_DOMAIN_ACCESS_DENIED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_the_base_url_option_is_what_the_poll_talks_to(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """An entry with `base_url` in its options polls that host, not the default."""
    other_host = "https://api.int.iot.radoff.life"
    register_devices(
        requests_mock,
        load_devices_fixture("devices_one_device.json"),
        base_url=other_host,
    )

    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True, CONF_BASE_URL: other_host},
        version=3,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.living_room_temperature").state == "20.9"
    assert {request.netloc for request in requests_mock.request_history} == {
        "api.int.iot.radoff.life"
    }


def _make_coordinator(
    hass: HomeAssistant,
    config_entry_v3_data: dict[str, Any],
    *,
    entry_id: str,
    options: dict[str, Any] | None = None,
) -> RadoffCoordinator:
    """Build a coordinator on a real entry, without ever polling with it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options=options or {"generate_index": True},
        version=3,
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    return RadoffCoordinator(hass, entry)


def _next_delay(
    coordinator: RadoffCoordinator, monkeypatch: pytest.MonkeyPatch
) -> float:
    """Return the delay, in seconds, this coordinator's next refresh asks for."""
    seen: list[float] = []
    monkeypatch.setattr(
        DataUpdateCoordinator,
        "_schedule_refresh",
        lambda self: seen.append(self.update_interval.total_seconds()),
    )
    coordinator._schedule_refresh()  # noqa: SLF001
    return seen[0]


async def test_two_coordinators_created_together_do_not_poll_together(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """Two coordinators created in the same instant ask for different delays."""
    first = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-one")
    second = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-two")

    assert first.poll_jitter != second.poll_jitter
    assert _next_delay(first, monkeypatch) != _next_delay(second, monkeypatch)


async def test_the_offset_never_shortens_the_interval_below_what_was_asked(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """The offset is added, never subtracted: the delay stays in [interval, +10%)."""
    coordinator = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-one")

    delay = _next_delay(coordinator, monkeypatch)

    assert DEFAULT_SCAN_INTERVAL <= delay
    assert delay < DEFAULT_SCAN_INTERVAL * (1 + POLL_JITTER_FRACTION)


async def test_the_offset_of_an_entry_survives_a_restart(
    hass: HomeAssistant,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """The same entry gets the same offset every time it is set up."""
    before = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-one")
    after = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-one")

    assert before.poll_jitter == after.poll_jitter


async def test_a_long_backoff_pushes_only_the_next_cycle_out(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A backoff longer than the interval delays one cycle, and leaves `update_interval` alone."""
    coordinator = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-one")
    nominal = coordinator.update_interval

    coordinator._rate_limit_delay = DEFAULT_SCAN_INTERVAL * 3.0  # noqa: SLF001

    assert _next_delay(coordinator, monkeypatch) == DEFAULT_SCAN_INTERVAL * 3.0
    assert coordinator.update_interval == nominal

    assert _next_delay(coordinator, monkeypatch) < DEFAULT_SCAN_INTERVAL * 1.5


async def test_a_short_backoff_does_not_pull_the_next_cycle_forward(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A backoff shorter than the remaining wait does not pull the next cycle forward."""
    coordinator = _make_coordinator(hass, config_entry_v3_data, entry_id="entry-one")

    coordinator._rate_limit_delay = 5.0  # noqa: SLF001

    assert _next_delay(coordinator, monkeypatch) >= DEFAULT_SCAN_INTERVAL


async def test_a_429_skips_the_cycle_without_making_entities_unavailable(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A 429 skips the cycle: the entities keep the previous readings and stay available."""
    register_devices(requests_mock, load_devices_fixture("devices_two_devices.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v3_data)
    before_state = hass.states.get("sensor.bedroom_temperature")
    before = before_state.state
    measured_at_before = before_state.attributes["last_measured_at"]
    assert before != "unavailable"

    register_devices(requests_mock, RATE_LIMIT_BODY, status_code=429)
    await _repoll(hass)

    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert coordinator.last_update_success is True
    assert hass.states.get("sensor.bedroom_temperature").state == before
    assert hass.states.get("sensor.living_room_temperature").state != "unavailable"

    # Nothing about the rate limit reads as the device being gone.
    bedroom = hass.states.get("sensor.bedroom_temperature")
    assert bedroom.attributes["connection_status"] == "connected"
    assert bedroom.attributes["last_measured_at"] == measured_at_before

    register_devices(requests_mock, load_devices_fixture("devices_two_devices.json"))
    await _repoll(hass)

    assert hass.states.get("sensor.bedroom_temperature").state == before
    assert coordinator.last_update_success is True


async def test_a_429_on_the_very_first_poll_retries_the_setup(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A 429 on the very first poll sends the entry to `setup_retry`: there is nothing to keep."""
    register_devices(requests_mock, RATE_LIMIT_BODY, status_code=429)
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
