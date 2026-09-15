"""
Config entry migration to VERSION 3, and the guards that run after it.

Covers: the data migration, the offline re-keying of a released
installation's 32 entities, the missing-domain repair, and schema fetching.
"""

from __future__ import annotations

import ast
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_CLIENT_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.radoff
from custom_components.radoff import async_migrate_entry
from custom_components.radoff.const import (
    CONF_DOMAIN_PREFIX,
    CONF_INDEX,
    CONF_POOL_ID,
    CONF_POOL_REGION,
    DOMAIN,
    ISSUE_MISSING_DOMAIN_PREFIX,
)

from .conftest import (
    auth_result,
    load_devices_fixture,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
    register_measures_ranges,
    seed_released_registry,
)

DOMAIN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
DOMAIN_PREFIX = "home1234"

# The two devices of the released-installation fixture, as their serial
# numbers - which is what every migrated identifier is built from.
SERIAL_NOWPLUS = "AA11BB"
SERIAL_NOW = "CC22DD"
DEVICE_UUID = "11111111-1111-4111-8111-111111111111"


def _issue_id(entry: MockConfigEntry) -> str:
    return f"{ISSUE_MISSING_DOMAIN_PREFIX}_{entry.entry_id}"


def _legacy(slug: str, device_uuid: str = DEVICE_UUID) -> str:
    """The unique_id the released version gave one reading of one device."""
    return f"{DOMAIN}-{device_uuid}-{slug}"


def _register_device(hass: HomeAssistant, entry: MockConfigEntry, serial: str) -> str:
    """Register one Radoff device the way the integration itself does."""
    return (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, serial)},
            name=f"Radoff {serial}",
            manufacturer="Radoff",
        )
        .id
    )


# ---------------------------------------------------------------------------
# The entry's own `data`
# ---------------------------------------------------------------------------


async def test_migrate_v1_to_v3_strips_cognito_fields_and_moves_index(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """VERSION 1 -> 3 drops the Cognito fields and moves `generate_index` to options."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert (entry.version, entry.minor_version) == (3, 1)
    assert CONF_CLIENT_ID not in entry.data
    assert CONF_POOL_ID not in entry.data
    assert CONF_POOL_REGION not in entry.data
    assert CONF_INDEX not in entry.data
    assert CONF_DOMAIN_PREFIX not in entry.data
    assert entry.data["username"] == "user@example.com"
    assert entry.options[CONF_INDEX] is False


async def test_migrate_v1_to_v3_defaults_index_when_absent(
    hass: HomeAssistant,
) -> None:
    """`generate_index` defaults to True in options when never set in data (v1)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={"username": "user@example.com", "password": "hunter2"},
        options={},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.options[CONF_INDEX] is True


async def test_migrate_v2_to_v3_drops_the_arch_1x_domain_id(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """An arch 1.x `domain_id` UUID is dropped, not carried over or translated."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert (entry.version, entry.minor_version) == (3, 1)
    assert "domain_id" not in entry.data
    assert CONF_DOMAIN_PREFIX not in entry.data
    assert entry.data["username"] == "user@example.com"
    assert DOMAIN_ID in caplog.text


async def test_migrate_keeps_a_domain_prefix_already_present(
    hass: HomeAssistant, config_entry_v3_data: dict[str, Any]
) -> None:
    """A `domain_prefix` already in `data` is carried over untouched."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=config_entry_v3_data)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.data[CONF_DOMAIN_PREFIX] == DOMAIN_PREFIX


# ---------------------------------------------------------------------------
# The entity registry of a real released installation
# ---------------------------------------------------------------------------

# What the 32 identifiers of the released backup become. Written out rather
# than computed from the map the code uses, deliberately: a test that builds
# its expectation with the implementation's own table asserts that the table
# is self-consistent, not that it is right.
EXPECTED_MIGRATION = {
    "airqualityindex": "aqi_value",
    "eco2": "eco2",
    "eco2-index": "eco2-index",
    "internal_temperature": "internal_temperature",
    "internal_temperature-index": "internal_temperature-index",
    "pm1": "pm1",
    "pm1-index": "pm1-index",
    "pm10": "pm10",
    "pm10-index": "pm10-index",
    "pm25": "pm25",
    "pm25-index": "pm25-index",
    "pressure": "pressure",
    "relative_humidity": "relative_humidity",
    "relative_humidity-index": "relative_humidity-index",
    "tvoc": "tvoc",
    "tvoc-index": "tvoc-index",
}


def _expected_unique_ids() -> set[str]:
    """The 32 identifiers a migrated released installation must hold."""
    return {
        f"{DOMAIN}-{serial}-{new}"
        for serial in (SERIAL_NOWPLUS, SERIAL_NOW)
        for new in EXPECTED_MIGRATION.values()
    }


async def test_a_released_backup_keeps_every_entity_id_and_gains_serial_keys(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """A released backup keeps every `entity_id` and gains serial-keyed identifiers."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)
    before = seed_released_registry(hass, entry)
    assert len(before) == 32

    assert await async_migrate_entry(hass, entry)

    registry = er.async_get(hass)
    after = er.async_entries_for_config_entry(registry, entry.entry_id)

    assert len(after) == 32
    assert {entity.unique_id for entity in after} == _expected_unique_ids()

    # The entity_id of every one of them is exactly where it was, which is
    # what `states` and the hourly `statistics` series are attached to.
    assert {entity.entity_id for entity in after} == set(before.values())


async def test_the_released_backup_migration_is_idempotent(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """Running the migration twice changes nothing the second time."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)
    seed_released_registry(hass, entry)

    assert await async_migrate_entry(hass, entry)

    registry = er.async_get(hass)
    first_pass = {
        entity.entity_id: entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    # Not the version guard doing the work: the entry is put back to a
    # pre-migration version so the whole pass runs again over entities that
    # have already been re-keyed.
    hass.config_entries.async_update_entry(entry, version=1, minor_version=1)

    assert await async_migrate_entry(hass, entry)

    second_pass = {
        entity.entity_id: entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert second_pass == first_pass


async def test_an_occupied_target_unique_id_is_not_overwritten(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """A target `unique_id` another entity already holds is left alone."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)
    other = MockConfigEntry(domain=DOMAIN, version=3, data=config_entry_v1_data)
    other.add_to_hass(hass)

    device_id = _register_device(hass, entry, SERIAL_NOWPLUS)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor", DOMAIN, _legacy("eco2"), config_entry=entry, device_id=device_id
    )
    occupant = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}-{SERIAL_NOWPLUS}-eco2", config_entry=other
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(legacy.entity_id).unique_id == _legacy("eco2")
    assert (
        registry.async_get(occupant.entity_id).unique_id
        == f"{DOMAIN}-{SERIAL_NOWPLUS}-eco2"
    )
    assert "already held by" in caplog.text
    # The entry still reaches version 3: the re-keying it could do is done,
    # and an entry left behind would run the whole migration again on every
    # restart for a conflict that is not going to resolve itself.
    assert entry.version == 3


async def test_an_orphaned_average_entity_is_removed_not_left_unavailable(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """The `*_average` entity that loses the race for `aqi_value` is removed, not orphaned."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    device_id = _register_device(hass, entry, SERIAL_NOWPLUS)
    registry = er.async_get(hass)
    pre_existing = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        _legacy("airqualityindex"),
        config_entry=entry,
        device_id=device_id,
        suggested_object_id="living_room_air_quality",
    )
    newcomer = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        _legacy("airqualityindex_average"),
        config_entry=entry,
        device_id=device_id,
        suggested_object_id="living_room_air_quality_average",
    )

    assert await async_migrate_entry(hass, entry)

    migrated = registry.async_get(pre_existing.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{DOMAIN}-{SERIAL_NOWPLUS}-aqi_value"
    assert registry.async_get(newcomer.entity_id) is None
    assert "can never receive a value again" in caplog.text


async def test_a_lone_average_entity_is_migrated_not_removed(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """A lone `airqualityindex_average` is re-keyed onto `aqi_value`, keeping its history."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    device_id = _register_device(hass, entry, SERIAL_NOWPLUS)
    registry = er.async_get(hass)
    aqi = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        _legacy("airqualityindex_average"),
        config_entry=entry,
        device_id=device_id,
        suggested_object_id="living_room_air_quality",
    )

    assert await async_migrate_entry(hass, entry)

    migrated = registry.async_get(aqi.entity_id)
    assert migrated is not None
    assert migrated.unique_id == f"{DOMAIN}-{SERIAL_NOWPLUS}-aqi_value"
    # States and statistics hang off the `entity_id`, not off the unique id:
    # the row found under the new key has to be the row that was already there.
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}-{SERIAL_NOWPLUS}-aqi_value"
        )
        == aqi.entity_id
    )


async def test_an_entity_without_a_device_is_left_intact(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """An entity with no device has no serial to be re-keyed onto, and is left intact."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    orphan = registry.async_get_or_create(
        "sensor", DOMAIN, _legacy("pressure"), config_entry=entry
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(orphan.entity_id).unique_id == _legacy("pressure")
    assert "no device to read a serial number from" in caplog.text


async def test_an_identifier_this_migration_does_not_know_is_left_alone(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """An identifier this migration does not recognise is left alone."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    device_id = _register_device(hass, entry, SERIAL_NOWPLUS)
    registry = er.async_get(hass)
    untouched = [
        f"{DOMAIN}-{SERIAL_NOWPLUS}-aqi_value",
        f"{DOMAIN}-{SERIAL_NOWPLUS}-radon_bqm3-index",
        "someone-elses-identifier",
    ]
    created = {
        unique_id: registry.async_get_or_create(
            "sensor", DOMAIN, unique_id, config_entry=entry, device_id=device_id
        )
        for unique_id in untouched
    }

    assert await async_migrate_entry(hass, entry)

    for unique_id, entity in created.items():
        assert registry.async_get(entity.entity_id).unique_id == unique_id


async def test_another_entrys_entities_are_not_migrated_by_this_ones_pass(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """Another entry's entities are left to that entry's own migration pass."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)
    other = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    other.add_to_hass(hass)

    device_id = _register_device(hass, other, SERIAL_NOW)
    registry = er.async_get(hass)
    foreign = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        _legacy("tvoc", "22222222-2222-4222-8222-222222222222"),
        config_entry=other,
        device_id=device_id,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(foreign.entity_id).unique_id == _legacy(
        "tvoc", "22222222-2222-4222-8222-222222222222"
    )


async def test_the_tvoc_display_unit_override_is_cleared(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """A display-unit override on tvoc is cleared, because tvoc no longer has that unit."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)
    created = seed_released_registry(
        hass,
        entry,
        slugs={SERIAL_NOWPLUS: ["tvoc", "eco2"]},
        unit_overrides={"tvoc": "mg/m³", "eco2": "ppm"},
    )

    assert await async_migrate_entry(hass, entry)

    registry = er.async_get(hass)
    tvoc = registry.async_get(created[_legacy("tvoc")])
    assert tvoc.unique_id == f"{DOMAIN}-{SERIAL_NOWPLUS}-tvoc"
    assert tvoc.options.get("sensor", {}).get("unit_of_measurement") is None

    # And only tvoc: eco2 is still ppm before and after, so its override is
    # a setting the user made and this migration has no business touching.
    eco2 = registry.async_get(created[_legacy("eco2")])
    assert eco2.options["sensor"]["unit_of_measurement"] == "ppm"


# ---------------------------------------------------------------------------
# The setup guard and its repair
# ---------------------------------------------------------------------------


async def test_upgrade_of_a_real_v1_entry_raises_a_repair_not_a_keyerror(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """A real v1 entry upgrading stops with a translated `ConfigEntryError` and a repair."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 3
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == ISSUE_MISSING_DOMAIN_PREFIX
    assert "KeyError" not in caplog.text

    issue = ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.data == {"entry_id": entry.entry_id}
    # Every Radoff entry is titled "Radoff": with two accounts configured,
    # the username is the only thing that tells the two repair cards apart.
    assert issue.translation_placeholders == {"username": "user@example.com"}


async def test_a_qa_entry_on_the_old_domain_id_also_gets_the_repair(
    hass: HomeAssistant, config_entry_v2_data: dict[str, Any]
) -> None:
    """An entry carrying an arch 1.x `domain_id` reaches the same repair."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == ISSUE_MISSING_DOMAIN_PREFIX
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is not None


async def test_setup_with_a_domain_prefix_clears_a_stale_repair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """An entry that does have its `domain_prefix` loads and drops any leftover issue."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_devices(requests_mock, load_fixture("devices_empty.json"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=1,
        data=config_entry_v3_data,
        options={CONF_INDEX: True},
    )
    entry.add_to_hass(hass)

    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry),
        data={"entry_id": entry.entry_id},
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_MISSING_DOMAIN_PREFIX,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is None


# ---------------------------------------------------------------------------
# The schema call at setup
# ---------------------------------------------------------------------------


async def test_the_schema_is_fetched_once_per_type_and_cached(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """The schema is fetched once per device type and cached, not once per device."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_devices(requests_mock, load_fixture("devices_two_devices.json"))

    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data=config_entry_v3_data, options={CONF_INDEX: True}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    schema_calls = [
        request
        for request in requests_mock.request_history
        if request.path == "/analytics/measures-ranges"
    ]
    types_asked = [request.qs["device_type"][0] for request in schema_calls]

    assert types_asked == ["nowplus"]
    assert set(types_asked) == set(entry.runtime_data.schemas)
    # And the cache holds real measures, not empty placeholders.
    assert all(entry.runtime_data.schemas.values())


async def test_a_schema_endpoint_that_is_down_retries_instead_of_loading(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A schema endpoint that is down raises `ConfigEntryNotReady`, and is retried."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    register_measures_ranges(requests_mock, payloads={}, status_code=500)

    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data=config_entry_v3_data, options={CONF_INDEX: True}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_429_on_the_schema_call_names_itself_and_retries(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    caplog: pytest.LogCaptureFixture,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """A 429 on the schema call retries the setup, and the log names the rate limit."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))
    register_measures_ranges(
        requests_mock, payloads={}, status_code=HTTPStatus.TOO_MANY_REQUESTS
    )

    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data=config_entry_v3_data, options={CONF_INDEX: True}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "rate limit reached while fetching the measurement schema" in caplog.text


# ---------------------------------------------------------------------------
# The arch 1.x domain names, which no code may use
# ---------------------------------------------------------------------------


def test_the_arch_1x_domain_names_are_gone_from_the_code() -> None:
    """No code *uses* the arch 1.x domain names; an AST walk, so comments may keep them."""
    forbidden = {
        "CONF_DOMAIN_ID",
        "ISSUE_MISSING_DOMAIN_ID",
        "_extract_domain_claims",
    }
    integration_dir = Path(custom_components.radoff.__file__).parent

    offenders: list[str] = []
    for path in sorted(integration_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            used = None
            if isinstance(node, ast.Name):
                used = node.id
            elif isinstance(node, ast.Attribute):
                used = node.attr
            elif isinstance(node, ast.alias):
                used = node.name
            if used in forbidden:
                offenders.append(f"{path.name}:{getattr(node, 'lineno', '?')} {used}")

    assert not offenders, f"arch 1.x domain names still used: {offenders}"


async def test_a_lost_registry_save_is_repaired_on_the_next_start(
    hass: HomeAssistant, config_entry_v3_data: dict[str, Any]
) -> None:
    """An entry at version 3 whose entities were never re-keyed is put right on the next start."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, minor_version=1, data=config_entry_v3_data
    )
    entry.add_to_hass(hass)

    device_id = _register_device(hass, entry, SERIAL_NOWPLUS)
    registry = er.async_get(hass)
    stranded = registry.async_get_or_create(
        "sensor", DOMAIN, _legacy("eco2"), config_entry=entry, device_id=device_id
    )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        registry.async_get(stranded.entity_id).unique_id
        == f"{DOMAIN}-{SERIAL_NOWPLUS}-eco2"
    )


async def test_an_entry_without_a_domain_still_gets_its_entities_re_keyed(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """An entry without a domain still gets its entities re-keyed: that runs first."""
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    device_id = _register_device(hass, entry, SERIAL_NOWPLUS)
    registry = er.async_get(hass)
    stranded = registry.async_get_or_create(
        "sensor", DOMAIN, _legacy("tvoc"), config_entry=entry, device_id=device_id
    )

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert (
        registry.async_get(stranded.entity_id).unique_id
        == f"{DOMAIN}-{SERIAL_NOWPLUS}-tvoc"
    )
