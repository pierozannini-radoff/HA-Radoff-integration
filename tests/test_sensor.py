"""
Sensor entity tests (S-18, updated by cards M-03 and M-04): payload -> entities.

Complements `test_coordinator.py`'s poll tests: this file asserts on every
reading of the nominal fixture (unit, device class, value, and - for the
qualitative siblings - index state), not just the handful already used to
prove the poll pipeline works end to end.

Two things M-03 changed are pinned here rather than described: the
`unique_id` shape (`radoff-{serial_number}-{field}`) and the AQI, which is
one entity fed by `telemetry.aqi_value` - the bucket suffix S-10 needed is
gone with the buckets.

Card M-04 changes where every expectation below comes from. Units, device
classes and qualitative states are no longer a table in `sensor.py`: they
are what `/analytics/measures-ranges` serves for the device's type, mocked
here from the real payloads M-01 captured on dev (see
`conftest.py::register_measures_ranges`). Three of the expectations moved as
a result, and each move is a fact about the API rather than a preference:

- `tvoc` has no unit and no device class. The API declares its unit as
  `V - Ix` (T-02 D-07), which is not µg/m³ and not a Home Assistant unit at
  all, so the VOC device class - which would force µg/m³ - is wrong for it.
- the five-band vocabulary is `excellent -> high -> good -> poor ->
  terrible`, with `high` second. eco2 at 600 is therefore `high`, where the
  hardcoded table used to call it `good`. The thresholds are identical; the
  word for the band is the backend's now.
- temperature and humidity band as `low -> good -> high`.

And `aqi_value` is created disabled (T-08 D-08: the backend computes its
temperature component with the wrong divisor), which is why it is tested
through the entity registry instead of through `hass.states`.
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
        # tvoc: unit `V - Ix` in the response, which maps onto no Home
        # Assistant unit and therefore onto no device class either (M-04).
        ("sensor.living_room_vocs", "50.0", None, None),
        ("sensor.living_room_co2", "600.0", "ppm", "carbon_dioxide"),
        ("sensor.living_room_pm10", "10.0", "µg/m³", "pm10"),
        ("sensor.living_room_pm2_5", "8.0", "µg/m³", "pm25"),
        ("sensor.living_room_pm1", "3.0", "µg/m³", "pm1"),
        ("sensor.living_room_temperature", "20.9", "°C", "temperature"),
        ("sensor.living_room_humidity", "45.0", "%", "humidity"),
        # M-04 AC: declared in Pa, the unit the API sends. Converting to
        # hPa/mbar is the Home Assistant UI's job.
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
        # 500 < 600 <= 1000 -> the second band, which the API calls "high"
        # and the deleted table called "good" (M-04).
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


async def test_a_field_the_schema_does_not_declare_becomes_a_raw_entity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A telemetry field outside the schema is published as a bare value (M-04).

    The inverse of what M-03's provisional table did, and deliberately so.
    `radon_status` is the field this exists for: T-08 D-10 is still open, so
    nothing is known about the enumeration behind its value, and publishing
    the raw state while saying the map is unknown is more honest than either
    dropping it or inventing labels. No unit, no device class, no bands -
    there is nothing to derive them from.
    """
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["telemetry"]["radon_status"] = 2
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


# `sismoff` manca da questa lista deliberatamente: le fixture di M-01
# contengono il suo *schema* ma nessun device di quel tipo, e la card lo
# esclude per decisione del 2026-09-10 (vedi
# `docs/M-04-verifica-dev.md`, "Scostamenti"). Aggiungerlo qui e' una riga,
# il giorno che una passata su dev cattura un sismoff: la funzione e' gia'
# parametrica sul tipo.
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
    config_entry_v2_data: dict[str, Any],
    serial: str,
    fixture_name: str,
) -> None:
    """
    M-04 AC, on the real fixtures: entities == the measures the type declares.

    Both devices come from `devices__full.json`, the page M-01 actually
    captured on dev, and both schemas are the real `measures-ranges`
    responses for their types. The `sense` is the interesting half: its
    telemetry block is *empty* in that capture, and it still gets the ten
    entities its type declares - `radon_bqm3` among them, which the `nowplus`
    does not have because that hardware has no radon sensor (T-02 D-13).
    Entities follow the type's schema, not the last payload, so a device
    that goes quiet keeps a stable set instead of losing and regaining it.
    """
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    register_devices(requests_mock, load_dev_fixture("devices__full"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    M-04 AC: a device type the catalogue does not know does not lose the device.

    The schema call answers 404 with `available` (the real body M-01
    captured), which is a statement about the catalogue, not about this
    device. Setup completes, the device is there, and its telemetry becomes
    entities with no unit and no bands - everything that can still be
    derived without a schema.
    """
    patch_authenticate_user(
        monkeypatch,
        result=auth_result(make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])),
    )
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["type"] = "not-a-real-device-type"
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
    """
    M-04 AC: `aqi_value` exists but starts disabled (T-08 D-08).

    The backend computes the AQI's temperature component with a divisor of
    120 that does not belong there, so the published index is wrong. The
    entity is created anyway - a user who wants it enables it in one click,
    and the day the backend fixes the divisor the default flips back with no
    migration and no re-keying. Its qualitative sibling goes with it: it
    bands the same wrong number.
    """
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
    config_entry_v2_data: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    M-04 AC: an unmappable unit produces a WARNING, never a failed setup.

    The unit enumeration Home Assistant understands is closed and so is the
    API's, and the day the backend adds a value to its own the integration
    must keep working. The entity is created without a unit and the log says
    which measure and which unit, so the mapping can be added deliberately
    rather than discovered from a broken installation.
    """
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
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
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
# Card M-06: availability comes from `connection_status`
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
    config_entry_v2_data: dict[str, Any],
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
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.parametrize(
    ("connection_status", "expected_available"),
    [
        ("connected", True),
        # M-06 AC2: not connected, with a full and fresh telemetry block -
        # the fixture's - and unavailable all the same. Availability is the
        # connection, not the data.
        ("disconnected", False),
        # M-06 AC3: a value from outside the enumeration we have. Available,
        # because "not connected" is a claim we can only make about a value
        # we recognise (T-08 D-17 is still open).
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
    config_entry_v2_data: dict[str, Any],
    connection_status: str | None,
    expected_available: bool,
) -> None:
    """
    M-06: `connection_status` decides availability, and only two values can.

    The whole card in one parametrisation. Every case uses the nominal
    payload - a device with complete, current telemetry - so the only
    variable is the connection field, which is exactly the claim being
    made: an entity is unavailable when its device says `disconnected`, and
    at no other time.

    The two rows that matter most are the ones that were impossible before:
    a value we have never seen, and a missing field, both leave the entity
    working. S-07's rule ("anything that is not fresh data is unavailable")
    would have taken every entity of the device down in both cases, on the
    strength of a string.
    """
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["connection_status"] = connection_status

    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v2_data, payload)

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
    config_entry_v2_data: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    M-06 AC3: an unseen `connection_status` is a WARNING, not an outage.

    The other half of the AC the parametrisation above covers: the value
    must be *reported*, because the enumeration is an open request (T-08
    D-17) and a new state arriving silently would leave us deciding
    availability from a vocabulary we no longer know.

    Warned once, and this is where that is pinned: both devices of the
    fixture carry the same new value, and one line comes out. Deduped by
    value rather than by device on purpose - a domain migrating to a new
    state would otherwise write one WARNING per device per poll, which is
    how a useful signal becomes a log to be ignored.
    """
    payload = load_devices_fixture("devices_two_devices.json")
    for device in payload["devices"]:
        device["connection_status"] = "evaporated"

    caplog.clear()
    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v2_data, payload)

    warnings = [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "evaporated" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "T-08" in warnings[0].getMessage()

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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    M-06 AC6: the entity carries the measurement time and the status time.

    Two timestamps that answer two different questions - when did the
    device last produce data, and when did the backend last change its mind
    about the device being connected - plus the two status fields that are
    routinely mistaken for each other (`status` is administrative,
    `connection_status` operational). A user report has to arrive with all
    four, on the entity, or telling "reported offline but measuring" from
    "connected but measuring nothing" needs a diagnostics download.

    All four are published unconditionally when the payload carries them,
    with no derived flag beside them: the live pass of this card
    established that `connection_status_updated_at` is the moment the
    status last changed (T-02 D-17 (e)), so there is nothing here to
    compare it against and the age is left for a reader to judge.
    """
    payload = load_devices_fixture("devices_one_device.json")
    telemetry_timestamp = payload["devices"][0]["telemetry"]["timestamp"]
    connection_timestamp = payload["devices"][0]["connection_status_updated_at"]

    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v2_data, payload)

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
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    M-06: a value survives a restart onto a device that is still silent.

    The consequence of the decision this card made about "no value right
    now" (decided with Piero: the last known value, not `unknown`), taken
    to the case where it costs the most to implement and matters most to
    get right. Without the restore, every Home Assistant restart would
    punch an `unknown` into the history of every quiet device - which is
    the outcome the decision was meant to avoid, showing up once a week
    instead of once a poll.

    Both halves are restored and both are asserted: the value, so the state
    is continuous, and its timestamp, so the state does not come back
    claiming to have been measured at the moment of the restart. The
    timestamp is deliberately old here - a fixture value from before this
    suite ran, not "now" - because a restored value with a fresh timestamp
    is precisely the lie the attribute exists to prevent.
    """
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
    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v2_data, payload)

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert state.state == "19.5"
    assert state.attributes["last_measured_at"] == restored_measured_at


async def test_a_connected_device_with_no_status_timestamp_is_available(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    M-06: a missing `connection_status_updated_at` costs nothing.

    The timestamp is a diagnostic and only that, so a payload that does not
    carry it takes the attribute away and nothing else: the entity stays
    available on the strength of `connection_status` itself - which is
    present, and says `connected` - and the value it shows is unaffected.

    Kept as its own test because the omission has to stay an omission: an
    attribute published as `None` would read as "checked, and there is no
    timestamp", a different claim from "the payload did not say".
    """
    payload = load_devices_fixture("devices_one_device.json")
    payload["devices"][0]["connection_status_updated_at"] = None

    await _setup_with(hass, monkeypatch, requests_mock, config_entry_v2_data, payload)

    state = hass.states.get("sensor.living_room_temperature")
    assert state is not None
    assert state.state == "20.9"
    assert "connection_status_updated_at" not in state.attributes


def test_the_staleness_multiplier_is_gone_from_the_codebase() -> None:
    """
    M-06 AC5: the staleness multiplier does not exist any more.

    Written as a text search over the integration rather than as an
    `hasattr` check on `const.py`, because the criterion is about the
    codebase and not only about the module: the name surviving in a
    docstring, a comment or a dead import would mean the reasoning it stood
    for is still being handed to the next reader as current.

    The constant was `3`, multiplying the poll interval to produce a
    freshness threshold: two installations polling at 60s and at 3600s
    inherited thresholds of 3 and 180 minutes for identical hardware, and
    neither number came from the devices or from the backend. What replaced
    it is not another number but a different field - `connection_status`,
    which the backend owns and which answers the question availability was
    asking all along. See `entity.py::available`.
    """
    integration = Path("custom_components/radoff")
    offenders = [
        path.name
        for path in sorted(integration.rglob("*.py"))
        if "DEFAULT_STALE_MULTIPLIER" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
