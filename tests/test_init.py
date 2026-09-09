"""
Migration and setup-guard tests (S-18, RT-2926, T-06/F2): config entry migrations.

Every entry built here has the shape a real version-1 entry actually has:
NO `domain_id`. That key only came into existence with the multi-domain
discovery of S-01/RT-2803, in the very milestone that introduced VERSION 2,
so no entry created by the released version can contain it. S-18's original
tests all pre-seeded it into `data` (finding T-06/F1) and therefore could
not see that the migration left every real installation unable to load.

The 2.1 -> 2.2 tests at the bottom cover the other half of "what a real
installation already has": its *entities*. Card T-06/F2 - the AQI entity a
released installation owns is re-keyed onto the AGGREGATED slug instead of
being left behind, `unavailable` and cut off from its own history.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_CLIENT_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff import async_migrate_entry
from custom_components.radoff.const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    CONF_POOL_ID,
    CONF_POOL_REGION,
    DOMAIN,
    ISSUE_MISSING_DOMAIN_ID,
)

from .conftest import (
    auth_result,
    make_id_token,
    patch_authenticate_user,
    register_search,
)

DOMAIN_ID = "aaaaaaaa-0000-0000-0000-000000000001"


def _issue_id(entry: MockConfigEntry) -> str:
    return f"{ISSUE_MISSING_DOMAIN_ID}_{entry.entry_id}"


async def test_migrate_v1_to_v2_strips_cognito_fields_and_moves_index(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """
    VERSION 1 -> 2: Cognito fields dropped from data, generate_index moved to options.

    `domain_id` is *absent* from a real version-1 entry and stays absent:
    the migration deliberately does not go online to invent one (see
    `async_migrate_entry`'s docstring) - that is the repair flow's job.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert CONF_CLIENT_ID not in entry.data
    assert CONF_POOL_ID not in entry.data
    assert CONF_POOL_REGION not in entry.data
    assert CONF_INDEX not in entry.data
    assert CONF_DOMAIN_ID not in entry.data
    assert entry.data["username"] == "user@example.com"
    assert entry.options[CONF_INDEX] is False


async def test_migrate_v1_to_v2_defaults_index_when_absent(
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


async def test_migrate_keeps_an_already_present_domain_id(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """
    A `domain_id` already in `data` is carried over untouched.

    Not a shape any released version can produce, but a dev checkout run
    against an intermediate build of this milestone can: the migration must
    not drop it and send such an entry through a pointless repair.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={**config_entry_v1_data, CONF_DOMAIN_ID: DOMAIN_ID},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.data[CONF_DOMAIN_ID] == DOMAIN_ID


async def test_migrate_preserves_entity_unique_ids(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """
    Migration must not touch any entity's unique_id.

    `unique_id` (`RadoffEntity.unique_id`, entity.py) is built from
    `device_id`/`reading_key`, neither of which the migration reads or
    writes - this test registers an entity against the v1 entry first, then
    asserts the same unique_id is still resolvable to the same entity after
    migration.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    unique_id = f"{DOMAIN}-device-0000-0001-internal_temperature"
    entry_entity = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=unique_id,
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(entry_entity.entity_id).unique_id == unique_id


async def test_upgrade_of_a_real_v1_entry_raises_a_repair_not_a_keyerror(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    The RT-2827/T-06 scenario end to end: update, restart, no `domain_id`.

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

    assert entry.version == 2
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == ISSUE_MISSING_DOMAIN_ID
    assert "KeyError" not in caplog.text

    issue = ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
    assert issue is not None
    assert issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.data == {"entry_id": entry.entry_id}
    # Every Radoff entry is titled "Radoff": with two accounts configured,
    # the username is the only thing that tells the two repair cards apart.
    assert issue.translation_placeholders == {"username": "user@example.com"}


async def test_setup_with_domain_id_clears_a_stale_repair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """An entry that does have its `domain_id` loads and drops any leftover issue."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_search(requests_mock, {"devices": []})

    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data=config_entry_v2_data, options={CONF_INDEX: True}
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
        translation_key=ISSUE_MISSING_DOMAIN_ID,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry)) is None


def _aqi_unique_id(device_id: str = "device-0000-0001") -> str:
    """The unique_id the released version (30e0cde) gave the single AQI entity."""
    return f"{DOMAIN}-{device_id}-airqualityindex"


def _aggregated_aqi_unique_id(device_id: str = "device-0000-0001") -> str:
    """The unique_id `(Bucket.AGGREGATED, "airqualityindex")` resolves to today."""
    return f"{DOMAIN}-{device_id}-airqualityindex_average"


async def test_migrate_rekeys_the_pre_existing_aqi_entity(
    hass: HomeAssistant, config_entry_v2_data: dict[str, Any]
) -> None:
    """
    2.1 -> 2.2: the released AQI entity keeps its entity_id and gains the new key.

    The entity_id is what `states` and the hourly `statistics` series are
    attached to, so asserting it is unchanged *is* asserting the history
    survives (card AC2).
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    aqi = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aqi_unique_id(),
        config_entry=entry,
        suggested_object_id="living_room_air_quality",
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.minor_version == 2
    migrated = registry.async_get(aqi.entity_id)
    assert migrated is not None
    assert migrated.unique_id == _aggregated_aqi_unique_id()
    assert registry.async_get_entity_id("sensor", DOMAIN, _aqi_unique_id()) is None


async def test_migrate_aqi_is_idempotent(
    hass: HomeAssistant, config_entry_v2_data: dict[str, Any]
) -> None:
    """An entry already at minor version 2 is left completely alone."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aqi_unique_id(),
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(stale.entity_id).unique_id == _aqi_unique_id()


async def test_migrate_aqi_collision_keeps_the_entity_with_the_history(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    An instance that already ran the broken build has both entities: the old wins.

    Only a dev or verification instance can be in this state (no released
    version creates an `..._average` entity). The one created by that build
    holds a few days of samples at most; the orphaned original holds
    everything from before it, so the original takes over the identifier and
    the newcomer is removed.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aqi_unique_id(),
        config_entry=entry,
        suggested_object_id="living_room_air_quality",
    )
    newcomer = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aggregated_aqi_unique_id(),
        config_entry=entry,
        suggested_object_id="living_room_air_quality_average",
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(newcomer.entity_id) is None
    migrated = registry.async_get(legacy.entity_id)
    assert migrated is not None
    assert migrated.unique_id == _aggregated_aqi_unique_id()
    assert "keep its history" in caplog.text


async def test_migrate_aqi_leaves_every_other_entity_untouched(
    hass: HomeAssistant, config_entry_v2_data: dict[str, Any]
) -> None:
    """
    Only the AQI is re-keyed - the other 30 entities of a real instance are not.

    Includes the two shapes closest to the migrated one: the "-index" siblings
    the released version did build for other properties, and an AQI-named one
    that is not the bare slug.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    untouched = [
        f"{DOMAIN}-device-0000-0001-internal_temperature",
        f"{DOMAIN}-device-0000-0001-relative_humidity-index",
        f"{DOMAIN}-device-0000-0001-airqualityindex-index",
        f"{DOMAIN}-device-0000-0001-airqualityindex_average",
    ]
    created = {
        unique_id: registry.async_get_or_create(
            domain="sensor",
            platform=DOMAIN,
            unique_id=unique_id,
            config_entry=entry,
        )
        for unique_id in untouched
    }

    assert await async_migrate_entry(hass, entry)

    for unique_id, entity in created.items():
        assert registry.async_get(entity.entity_id).unique_id == unique_id


async def test_migrate_aqi_of_a_v1_entry_runs_in_the_same_pass(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> None:
    """
    A real released entry (version 1) gets both steps at once.

    That is the actual upgrade path: nobody is at 2.1 except an instance that
    ran an intermediate build of this milestone.
    """
    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    aqi = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aqi_unique_id(),
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    assert (entry.version, entry.minor_version) == (2, 2)
    assert registry.async_get(aqi.entity_id).unique_id == _aggregated_aqi_unique_id()


async def test_migrate_aqi_only_touches_its_own_entry(
    hass: HomeAssistant, config_entry_v2_data: dict[str, Any]
) -> None:
    """
    A second Radoff account's AQI entity is not migrated by the first's pass.

    `manifest.json` declares `single_config_entry: false`; each entry migrates
    when Home Assistant sets *it* up.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, data=config_entry_v2_data
    )
    other.add_to_hass(hass)

    registry = er.async_get(hass)
    other_aqi = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aqi_unique_id("device-0000-0002"),
        config_entry=other,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(other_aqi.entity_id).unique_id == _aqi_unique_id(
        "device-0000-0002"
    )


async def test_migrate_aqi_skips_a_collision_owned_by_another_entry(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """
    A target unique_id held by another entry's entity is left alone, not stolen.

    `unique_id` is unique per (domain, platform) registry-wide, so the
    collision check cannot be limited to this entry's own entities: raising
    out of a migration would leave the entry unloadable, and removing another
    entry's entity would destroy history this card exists to protect.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=1, data=config_entry_v2_data
    )
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, data=config_entry_v2_data
    )
    other.add_to_hass(hass)

    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aqi_unique_id(),
        config_entry=entry,
    )
    foreign = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=_aggregated_aqi_unique_id(),
        config_entry=other,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get(legacy.entity_id).unique_id == _aqi_unique_id()
    assert (
        registry.async_get(foreign.entity_id).unique_id == _aggregated_aqi_unique_id()
    )
    assert "another config entry" in caplog.text
    # The entry still reaches minor version 2: the pass ran, and re-running it
    # on every restart would only repeat the same warning.
    assert entry.minor_version == 2
