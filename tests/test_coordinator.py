"""
Coordinator poll tests (S-18, updated by card M-03): nominal and degraded.

Every test sets up a real config entry end to end (`async_setup_entry` ->
`RadoffCoordinator.async_config_entry_first_refresh` -> `sensor.py`'s
platform setup), with the Radoff API mocked at the transport level - the
same path Home Assistant itself exercises on every startup and poll.

What card M-03 changes here is the shape of "degraded". S-13's degraded
cases were per-device: one device's `GET` failed, `_merge_device_errors`
kept its previous readings and flipped `stale`, and the other device kept
updating. With a single `GET /data/devices` per cycle there is no such
state to test: a failure is the cycle's, and the per-device condition that
remains is the one the payload itself reports - `telemetry: null`, a device
the API answered about and that has no data.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import (
    CONF_BASE_URL,
    CONF_DOMAIN_ID,
    DOMAIN,
    ERROR_DOMAIN_ACCESS_DENIED,
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


async def _repoll(hass: HomeAssistant) -> None:
    """Run one more coordinator cycle against whatever is registered now."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_coordinator_without_domain_id_raises_config_entry_error(
    hass: HomeAssistant,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Constructing the coordinator without a `domain_id` never raises `KeyError`.

    Card RT-2926 / finding T-06/F1. `__init__.py::async_setup_entry` stops
    before getting here, but this class must not be the thing that decides
    whether the integration crashes: any caller reaching it without a
    domain gets the same explicit, translated `ConfigEntryError`.
    """
    data = {k: v for k, v in config_entry_v2_data.items() if k != CONF_DOMAIN_ID}
    entry = MockConfigEntry(domain=DOMAIN, data=data, version=2)
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError) as err:
        RadoffCoordinator(hass, entry)

    assert err.value.translation_key == "missing_domain_id"


async def test_poll_nominal_produces_expected_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A nominal payload produces the expected entities, with specific values.

    Asserts on concrete unit, device class, index state and value - not just
    "the entity exists" (S-18 AC: "asserzioni su valori specifici"). The
    temperature is the one to watch: `20.9` is the value the payload
    carries, not a scaled one (card M-03 removed the scaling factor).
    """
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

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

    # The AQI entity is created disabled (card M-04, T-08 D-08: the backend
    # computes its temperature component with the wrong divisor), so there
    # is no state to poll. `test_sensor.py` asserts on it via the registry.
    assert hass.states.get("sensor.living_room_air_quality") is None


async def test_the_device_registry_entry_carries_the_firmware_version(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Card M-03: `firmware_version` reaches `device_info.sw_version`.

    A field arch 1.x did not report at all, and the one addition of this
    card that a user can actually see - in the device page, not in an
    entity.
    """
    from homeassistant.helpers import device_registry as dr

    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, "SER-0000-0001")})

    assert device is not None
    assert device.sw_version == "0.2.8"


async def test_poll_degraded_device_missing_from_the_list(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """A device present on setup but absent from a later poll goes unavailable."""
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)
    assert hass.states.get("sensor.living_room_temperature").state != "unavailable"

    register_devices(requests_mock, load_fixture("devices_empty.json"))
    await _repoll(hass)

    assert hass.states.get("sensor.living_room_temperature").state == "unavailable"


async def test_poll_degraded_missing_field_only_affects_that_reading(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A field missing from the telemetry block only affects that one entity.

    Card M-04 changes the shape of "affects", not the isolation. Entities
    follow the device type's schema now, not the last payload, so the
    missing field keeps its entity and that entity goes `unavailable`
    where before it produced no entity at all. That is the better half of
    the trade: a device that skips a field for one cycle no longer loses
    and regains an entity, with the history that implies.
    """
    register_devices(requests_mock, load_devices_fixture("devices_missing_field.json"))

    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

    temperature = hass.states.get("sensor.living_room_temperature")
    assert temperature is not None
    assert temperature.state == "unavailable"

    humidity = hass.states.get("sensor.living_room_humidity")
    assert humidity is not None
    assert humidity.state == "45.0"


async def test_a_device_without_telemetry_creates_no_entities_and_no_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    M-03 AC: `telemetry: null` sets up cleanly, with no data and no error.

    The condition of the majority of the devices M-01 censused (D-16). The
    entry must load: a domain where nothing is transmitting is a normal
    state of the world, not a failure of the integration.

    Card M-04 moved where the entities come from - the device type's
    schema, not the payload - so a silent device now shows the entities its
    type declares, all `unavailable`, instead of none at all. The claim
    being tested is unchanged and is the one that matters: no value is
    invented and nothing raises.
    """
    register_devices(requests_mock, load_fixture("devices_no_telemetry.json"))

    entry = await _setup_entry(hass, monkeypatch, config_entry_v2_data)

    assert entry.state is ConfigEntryState.LOADED
    temperature = hass.states.get("sensor.living_room_temperature")
    assert temperature is not None
    assert temperature.state == "unavailable"
    coordinator = entry.runtime_data
    assert [device.stale for device in coordinator.data.devices] == [True]


async def test_a_device_falling_silent_only_affects_its_own_entities(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    One device's `telemetry: null` leaves the other device's entities alone.

    This is what per-device degradation looks like after card M-03: not a
    device whose own request failed (there is no per-device request any
    more), but a device the API answered about and that has nothing to say.
    """
    two_devices = load_devices_fixture("devices_two_devices.json")
    register_devices(requests_mock, two_devices)
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)
    assert hass.states.get("sensor.bedroom_temperature").state != "unavailable"

    silent = load_devices_fixture("devices_two_devices.json")
    silent["devices"][1]["telemetry"] = None
    register_devices(requests_mock, silent)
    await _repoll(hass)

    living_room = hass.states.get("sensor.living_room_temperature")
    assert living_room is not None
    assert living_room.state != "unavailable"

    bedroom = hass.states.get("sensor.bedroom_temperature")
    assert bedroom is not None
    assert bedroom.state == "unavailable"


async def test_a_500_costs_the_whole_cycle(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Card M-03: with one call per cycle, a 5xx makes every entity unavailable.

    Deliberately pinned, because it is the behaviour S-13 was written to
    avoid - back when a 5xx could come from one device's own GET and the
    other devices' data was still perfectly good. It cannot come from one
    device any more: the request that failed is the one that would have
    brought every device's telemetry, so there is nothing to keep.
    """
    register_devices(requests_mock, load_devices_fixture("devices_two_devices.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)
    assert hass.states.get("sensor.bedroom_temperature").state != "unavailable"

    register_devices(
        requests_mock, {"message": "Internal Server Error"}, status_code=500
    )
    await _repoll(hass)

    assert hass.states.get("sensor.living_room_temperature").state == "unavailable"
    assert hass.states.get("sensor.bedroom_temperature").state == "unavailable"


async def test_a_403_stops_the_entry_instead_of_retrying_forever(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Card M-02: a 403 on the poll is a reconfiguration, not a lost cycle.

    The API refusing the domain persisted on this entry cannot be fixed by
    retrying (the token is fine, the domain is not), so the coordinator
    raises `ConfigEntryError` and Home Assistant stops polling - the entry
    goes to `setup_error` with the translated message, instead of failing an
    update every interval forever. It is deliberately not
    `ConfigEntryAuthFailed`: no re-auth prompt appears, because the password
    is not the problem.
    """
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
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Card M-02: an entry with `base_url` in its options polls that host.

    The wiring the advanced option exists for, asserted end to end:
    options -> `RadoffCoordinator` -> `API`. Nothing is registered on the
    default host, so a request that ignored the option would fail the poll
    rather than quietly pass.
    """
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
        data=config_entry_v2_data,
        options={"generate_index": True, CONF_BASE_URL: other_host},
        version=2,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.living_room_temperature").state == "20.9"
    assert {request.netloc for request in requests_mock.request_history} == {
        "api.int.iot.radoff.life"
    }
