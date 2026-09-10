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

Card M-05 adds a second subject to this file: *when* the cycle runs. The
scheduling tests below drive `_schedule_refresh` directly rather than
waiting on the event loop - the point being tested is the delay this
coordinator asks for, not Home Assistant's ability to honour a timer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import (
    CONF_BASE_URL,
    CONF_DOMAIN_ID,
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
# quota shared with the Radoff mobile app (M-01 deliberately did not).
RATE_LIMIT_BODY = {"message": "Too Many Requests"}


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

    Card M-04 changed the shape of "affects", not the isolation: entities
    follow the device type's schema now, not the last payload, so the
    missing field keeps its entity instead of producing none at all.

    Card M-06 changes it again, and this time it is what the entity
    *shows*. The device says `connected`, so nothing about it is
    unavailable; the one field it did not send has no value to show and no
    earlier value to fall back on, which is `unknown`. The isolation is
    unchanged and is still the claim: the sibling that did arrive is
    unaffected.
    """
    register_devices(requests_mock, load_devices_fixture("devices_missing_field.json"))

    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    M-06 AC1: `connected` plus `telemetry: null` is not unavailable.

    The condition of the majority of the devices M-01 censused (D-16), and
    the card's first acceptance criterion. The entry must load - a domain
    where nothing is transmitting is a normal state of the world - and the
    entities must stay available: `GET /data/devices` looks at a 6-hour
    window and does not widen it on a miss, so silence this long is a fact
    about the query, not about the device, and the device itself says
    `connected`.

    Three assertions, and each one failed before this card in a different
    way. The state is `unknown` rather than `unavailable` (there is no
    earlier value on a device that has never transmitted, so there is
    nothing to remember). `stale` is still `True`, because it never meant
    offline and still does not. And `last_measured_at` is absent rather
    than borrowed from somewhere: nothing was ever measured.

    Only observable at all since card M-04 - before it, a device with
    `telemetry: null` had no entities, so the criterion had no subject.
    """
    register_devices(requests_mock, load_devices_fixture("devices_no_telemetry.json"))

    entry = await _setup_entry(hass, monkeypatch, config_entry_v2_data)

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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A device that falls silent keeps its last value; the other one updates.

    This is what per-device degradation looks like after card M-03: not a
    device whose own request failed (there is no per-device request any
    more), but a device the API answered about and that has nothing to say.

    Card M-06 decides what that device shows, and this test is where the
    decision and its cost are both pinned. The state stays the last value
    the device sent - decided with Piero, so that history stays continuous
    and automations do not meet `unknown` on every gap - while
    `last_measured_at` keeps the timestamp that value arrived with. That
    pair is the whole design: the state looks current and the attribute
    says it is not. The sibling device, which did report, moves both.
    """
    two_devices = load_devices_fixture("devices_two_devices.json")
    register_devices(requests_mock, two_devices)
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

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


def _make_coordinator(
    hass: HomeAssistant,
    config_entry_v2_data: dict[str, Any],
    *,
    entry_id: str,
    options: dict[str, Any] | None = None,
) -> RadoffCoordinator:
    """Build a coordinator on a real entry, without ever polling with it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v2_data,
        options=options or {"generate_index": True},
        version=2,
        entry_id=entry_id,
    )
    entry.add_to_hass(hass)
    return RadoffCoordinator(hass, entry)


def _next_delay(
    coordinator: RadoffCoordinator, monkeypatch: pytest.MonkeyPatch
) -> float:
    """
    Return the delay, in seconds, this coordinator's next refresh asks for.

    `RadoffCoordinator._schedule_refresh` swaps the delay it wants into
    `update_interval` and delegates to `DataUpdateCoordinator`, so capturing
    what the base class sees is exactly what a timer would have been armed
    with - without arming one, and without a test that has to sleep.
    """
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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Card M-05 AC: two coordinators created in the same instant are offset.

    The acceptance criterion in the card's own words ("Due coordinator
    creati nello stesso istante non pollano nello stesso istante"). Nothing
    here waits for a timer: what is pinned is the delay each one asks for,
    which is what a timer would use.
    """
    first = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-one")
    second = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-two")

    assert first.poll_jitter != second.poll_jitter
    assert _next_delay(first, monkeypatch) != _next_delay(second, monkeypatch)


async def test_the_offset_never_shortens_the_interval_below_what_was_asked(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    The offset is added, never subtracted (card M-05).

    A symmetric jitter would put an entry configured at the 60s floor at
    54s, under the device cadence that floor exists to respect - so the
    delay stays within [interval, interval + 10%).
    """
    coordinator = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-one")

    delay = _next_delay(coordinator, monkeypatch)

    assert DEFAULT_SCAN_INTERVAL <= delay
    assert delay < DEFAULT_SCAN_INTERVAL * (1 + POLL_JITTER_FRACTION)


async def test_the_offset_of_an_entry_survives_a_restart(
    hass: HomeAssistant,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    The same entry gets the same offset every time it is set up (M-05).

    This is what makes the spread hold through the correlated events that
    would otherwise undo it - a Home Assistant upgrade, a host reboot, an
    outage everyone recovers from at once. A `random` draw would put every
    installation that restarted together back in step; the offset is
    derived from the entry_id instead, so rebuilding the coordinator is
    indistinguishable from never having stopped.
    """
    before = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-one")
    after = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-one")

    assert before.poll_jitter == after.poll_jitter


async def test_a_long_backoff_pushes_only_the_next_cycle_out(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A backoff longer than the interval delays one cycle, and only one.

    Card M-05, the scheduling half of M-02's 429 work. `update_interval`
    itself must not move while this happens: the cycle timeout budget
    (S-13) and this entry's jitter share are both derived from it, and
    neither has any business changing because one cycle was rate limited.
    (S-07's freshness threshold hung off it too, and was asserted here
    until card M-06 removed the threshold itself.)
    """
    coordinator = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-one")
    nominal = coordinator.update_interval

    coordinator._rate_limit_delay = DEFAULT_SCAN_INTERVAL * 3.0  # noqa: SLF001

    assert _next_delay(coordinator, monkeypatch) == DEFAULT_SCAN_INTERVAL * 3.0
    assert coordinator.update_interval == nominal

    assert _next_delay(coordinator, monkeypatch) < DEFAULT_SCAN_INTERVAL * 1.5


async def test_a_short_backoff_does_not_pull_the_next_cycle_forward(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    The 429 backoff is a floor on the wait, never a replacement for it.

    Card M-05. A single 429 asks for ~5s (RATE_LIMIT_BACKOFF_START), and
    the next cycle was five minutes away regardless. Honouring the number
    literally would poll a rate-limited backend *more* often than a healthy
    one - which is the failure this assertion exists to prevent someone
    reintroducing.
    """
    coordinator = _make_coordinator(hass, config_entry_v2_data, entry_id="entry-one")

    coordinator._rate_limit_delay = 5.0  # noqa: SLF001

    assert _next_delay(coordinator, monkeypatch) >= DEFAULT_SCAN_INTERVAL


async def test_a_429_skips_the_cycle_without_making_entities_unavailable(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    Card M-05 AC: a 429 skips the cycle and the next one runs normally.

    Deliberately the opposite of `test_a_500_costs_the_whole_cycle` above,
    and the difference is the point: a 5xx means the data could not be
    fetched, a 429 means it was not asked for. The devices are still online
    and still emitting once a minute, so their entities keep the previous
    poll's readings instead of going `unavailable` - and the backoff the
    client computed is carried into the next scheduling decision.

    Card M-06 AC4 ("un 429 o un ciclo fallito non vengono confusi con un
    device offline") lands on this test, and makes it stronger than M-05
    could: back then the entities kept their readings only until those
    readings aged past the freshness threshold, so a long rate-limited
    stretch still ended in `unavailable`. There is no threshold now.
    Availability is the device's `connection_status`, which a 429 does not
    touch, so the entities also keep reporting `connected` with the
    `last_measured_at` of the last cycle that succeeded - the rate limit is
    visible as data that has stopped moving, never as a device that has
    gone away.
    """
    register_devices(requests_mock, load_devices_fixture("devices_two_devices.json"))
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)
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

    # M-06 AC4: nothing about the rate limit reads as the device being gone.
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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    The one case where a 429 still fails a cycle (card M-05).

    Keeping the previous poll's data needs a previous poll. At setup there
    is none - and no entities to protect either - so the entry goes to
    `setup_retry` and Home Assistant retries it, rather than completing a
    setup with no data at all.
    """
    register_devices(requests_mock, RATE_LIMIT_BODY, status_code=429)
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

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_frozen_connection_status_is_flagged_not_hidden(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    M-06: a `connected` older than the backend's own window warns, once.

    The safety net that replaces the staleness multiplier, and the reason
    it is only a net. Availability now rests on a single field this client
    does not own, so the case where that field stops being updated - the
    DynamoDB sync behind it stalls, a device leaves the sync but not the
    list - has to be visible. `CONNECTION_STATUS_STALE_WINDOW` is six
    hours because that is the window `GET /data/devices` itself looks at
    (T-02 D-16), not a multiple of anything we chose.

    What it deliberately does not do is decide - decided with Piero. The
    entities stay available and the state keeps its value: the cadence at
    which the backend refreshes that field is exactly what T-08 D-17 still
    has open, so turning an old timestamp into "offline" would put a second
    invented threshold where the first one has just been removed. The
    warning and the attribute are the whole reaction.

    Warned once per episode, and it stops when the status comes back inside
    the window - that is the second half of this test, and the difference
    between a signal and 288 lines a day.
    """
    stale_payload = load_devices_fixture("devices_one_device.json")
    stale_payload["devices"][0]["connection_status_updated_at"] = (
        datetime.now(UTC) - timedelta(hours=9)
    ).isoformat()

    caplog.clear()
    register_devices(requests_mock, stale_payload)
    await _setup_entry(hass, monkeypatch, config_entry_v2_data)

    def _warnings() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
            and "connection_status" in record.getMessage()
        ]

    assert len(_warnings()) == 1
    assert "9.0 hours" in _warnings()[0]

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert state.state == "20.9"
    assert state.attributes["connection_status_stale"] is True

    # A second cycle in the same condition adds nothing to the log.
    register_devices(requests_mock, stale_payload)
    await _repoll(hass)
    assert len(_warnings()) == 1

    # The status is refreshed: the flag clears, and so does the dedup - a
    # later episode is reported again rather than swallowed.
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    await _repoll(hass)

    state = hass.states.get("sensor.living_room_temperature")
    assert "connection_status_stale" not in state.attributes

    register_devices(requests_mock, stale_payload)
    await _repoll(hass)
    assert len(_warnings()) == 2
