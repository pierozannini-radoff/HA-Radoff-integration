"""
Migration and setup-guard tests (S-18, RT-2926, T-06/F2, M-07).

Every entry built here has the shape a real entry actually has. A version-1
one carries NO domain at all: that key only came into existence with the
multi-domain discovery of S-01/RT-2803, in the very milestone that
introduced VERSION 2, so no entry created by the released version can
contain it. S-18's original tests all pre-seeded it into `data` (finding
T-06/F1) and therefore could not see that the migration left every real
installation unable to load.

Card M-07 brings VERSION 3, and with it the half of "what a real
installation already has" that these tests care about most: its *entities*.
The AQI step of T-06/F2 is gone as a separate stage - it is absorbed into
the one that re-keys every entity onto `radoff-{serial}-{measure}` - and
what replaces its tests is a migration run against the registry of a real
released installation, the 32 entities card RT-2827 measured and
`docs/T-06-evidenze.md` recorded one by one (see
`conftest.seed_released_registry`).

That is the card's third acceptance criterion, and the reason it is worth
the fixture: the identifiers change on both halves at once, and the only
thing standing between a user and the loss of every graph they have is that
the `entity_id` under each one does not move.
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
# The entry's own `data` (S-02, and M-07's domain rename)
# ---------------------------------------------------------------------------


async def test_migrate_v1_to_v3_strips_cognito_fields_and_moves_index(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """
    VERSION 1 -> 3: Cognito fields dropped from data, generate_index moved to options.

    The domain is *absent* from a real version-1 entry and stays absent: the
    migration deliberately does not go online to invent one (see
    `async_migrate_entry`'s docstring) - that is the repair flow's job.
    """
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
    """
    A QA entry's `domain_id` UUID is dropped, not carried over or translated.

    There is no offline mapping from a UUID to a `domain_prefix` - arch 2.0
    does not know the UUID at all - so keeping it would only leave a later
    reader something that looks like a domain and is not one. The entry
    reaches version 3 without a domain and the repair flow asks for one.
    """
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
    """
    A `domain_prefix` already in `data` is carried over untouched.

    Not a shape any released version can produce, but a checkout run against
    an intermediate build of this milestone can: the migration must not drop
    it and send such an entry through a pointless repair.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=config_entry_v3_data)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.data[CONF_DOMAIN_PREFIX] == DOMAIN_PREFIX


# ---------------------------------------------------------------------------
# The entity registry of a real released installation (card M-07, AC 3-7)
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
    """
    AC 3, on the registry card RT-2827 measured: 32 entities, none lost, none added.

    Three assertions and each one is a separate way the update could ruin
    somebody's installation: an `entity_id` that moved takes its dashboard
    cards and automations with it; an identifier still on the arch 1.x form
    is an entity nothing will ever feed again; and an extra entity is the
    duplicate-with-no-history this whole card exists to prevent.
    """
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
    """AC 7: running the migration twice changes nothing the second time."""
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
    """
    AC 4: a destination identifier another entity holds is left alone.

    `unique_id` is unique per (domain, platform) registry-wide, so the
    collision cannot be looked up per entry: `async_update_entity` raises on
    a duplicate, and an exception escaping a migration leaves the entry
    unloadable - a worse outcome than the orphaning being fixed. Here the
    holder belongs to a second Radoff account, which is not this entry's to
    touch at all.
    """
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
    """
    AC 5: the `*_average` entity that loses the race for `aqi_value` is removed.

    Only an instance that ran an intermediate build of this milestone holds
    both AQI entities - the released one, rich in history, and a few days of
    `airqualityindex_average` beside it. Both map to `aqi_value`; the
    pre-existing one takes the identifier, and the loser would otherwise sit
    in the registry reading `unavailable` for the rest of the
    installation's life.
    """
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
    """
    The normal AQI case: `airqualityindex_average` becomes `aqi_value`.

    This is what every installation that has been through RT-2927 holds, and
    it keeps its history: removal is the collision case only.
    """
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


async def test_an_entity_without_a_device_is_left_intact(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    An entity with no device has no serial to be re-keyed onto, so it is not.

    The serial is only knowable through the device registry, which is the
    whole reason this migration can run offline. With no device there is
    nothing to compute and guessing is not an option - so the entity stays
    exactly as it is and says so in the log.
    """
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
    """
    Anything that is not an arch 1.x Radoff identifier is not touched.

    Two shapes matter here: an entity of some other integration that somehow
    shares this config entry, and one this card has already migrated - the
    second is what makes a repeated pass free rather than destructive.
    """
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
    """
    A second Radoff account's entities are left to their own migration.

    `manifest.json` declares `single_config_entry: false`; each entry
    migrates when Home Assistant sets *it* up.
    """
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
    """
    tvoc loses µg/m³, so a user's display-unit override of it is dropped.

    The override converts a unit the entity no longer has (T-02 D-07: the
    API declares `V - Ix`, which is not a concentration). Leaving it would
    have Home Assistant refuse or ignore a setting the user can see, which
    reads as a bug in the integration rather than as the deliberate change
    it is.

    What this does *not* claim to fix is the long-term statistics: those
    live in the recorder, a unit change breaks the series by design, and the
    README announces the gap next to the ~4 °C temperature step.
    """
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
# The setup guard and its repair (RT-2926, widened by M-07)
# ---------------------------------------------------------------------------


async def test_upgrade_of_a_real_v1_entry_raises_a_repair_not_a_keyerror(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    The RT-2827/T-06 scenario end to end: update, restart, no domain.

    This is what a real installation does on the first restart after the
    update - migration succeeds, then setup runs. It used to die with
    `KeyError: 'domain_id'` three frames deeper in `RadoffCoordinator`,
    which Home Assistant never retries and cannot explain. It must now stop
    with a translated `ConfigEntryError` and a fixable repair.
    """
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
    """
    Card M-07: an arch 1.x `domain_id` reaches the same repair, not a silent load.

    Worth its own test because the entry *looks* configured - it has a
    domain, it just has one no version of the API this integration now talks
    to has ever heard of.
    """
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
# The schema call at setup (card M-04)
# ---------------------------------------------------------------------------


async def test_the_schema_is_fetched_once_per_type_and_cached(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """
    M-04: one `measures-ranges` call per device *type*, kept in the runtime data.

    Two devices on the page, both `nowplus`, and exactly one schema call:
    the cache is keyed on the type, not on the device. On the 83-device
    domain M-01 censused that is the difference between one request per
    setup and eighty-three, against a quota shared with the Radoff apps
    (T-02 D-21/D-22).
    """
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
    """
    M-04: no schema, no setup - `ConfigEntryNotReady`, which Home Assistant retries.

    Deliberately not a fallback to a built-in table: that table is what the
    card exists to delete, and an entry that loaded without a schema would
    give the user a full set of nameless, unitless entities that only a
    restart could fix.
    """
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
    """
    Card M-05: a rate-limited setup is retried by Home Assistant, and says so.

    The outcome is the same `ConfigEntryNotReady` as the test above, and
    that is the decision, not an oversight: at setup there are no entities
    yet to keep available, so there is nothing for the poll path's 429
    handling to protect and no reason to run a retry loop of our own
    alongside the framework's. What the dedicated clause adds is that the
    log names the 429 and the backoff the client computed, instead of
    reading like the schema endpoint is down.
    """
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
# The names this card removes (M-07 AC 2)
# ---------------------------------------------------------------------------


def test_the_arch_1x_domain_names_are_gone_from_the_code() -> None:
    """
    AC 2: no code reads `CONF_DOMAIN_ID`, `ISSUE_MISSING_DOMAIN_ID` or the d_ claims.

    Deliberately an AST walk and not a text grep, which is what the AC's
    wording suggests and what would be wrong here: several comments still
    name `CONF_DOMAIN_ID`, and they should. A migration is the one place
    that has to explain what it replaced, and `const.py` saying "this
    replaces CONF_DOMAIN_ID" is the note that stops the next reader from
    reintroducing it. What must not survive is a *use*: an import, a read,
    an attribute access.

    `_extract_domain_claims` is in the list for the third name the AC gives.
    Card M-02 deleted the helper that decoded the IdToken's `d_<uuid>`
    claims; `tests/conftest.py::make_id_token` still builds tokens carrying
    them, because a real Radoff IdToken has them, and this test is what
    says the difference between a fixture that is realistic and code that
    depends on it.
    """
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
    """
    An entry already at version 3 whose entities were never re-keyed is put right.

    Found on the verification instance, not by reasoning: the version bump
    and the registry rewrite are two different stores with two different
    save delays (one second against ten), so a start interrupted between
    them records "version 3" on disk over a registry that was never
    rewritten. Home Assistant then never calls the migration handler again,
    because the versions match - and sixteen entities stay on arch 1.x
    identifiers permanently, holding their history, while a fresh set
    appears beside them.

    The entry here has a domain, so setup gets past the guard and fails
    later for want of a mocked API; what matters is that the entities are
    already re-keyed by then.
    """
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
    """
    The re-keying runs before the domain guard, not after it.

    An entry that reaches version 3 without a domain sits unloadable until
    somebody gets round to the repair - which can be days. Its entities are
    not waiting on anything: their new identifiers are computable from the
    device registry alone, and leaving them on the old ones for the
    duration would leave the user's history hanging on a thread for no
    reason.
    """
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
