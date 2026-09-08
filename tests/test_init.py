"""Migration test (S-18): config entry VERSION 1 -> 2."""

from __future__ import annotations

from homeassistant.const import CONF_CLIENT_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff import async_migrate_entry
from custom_components.radoff.const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    CONF_POOL_ID,
    CONF_POOL_REGION,
    DOMAIN,
)


async def test_migrate_v1_to_v2_strips_cognito_fields_and_moves_index(
    hass: HomeAssistant,
) -> None:
    """
    VERSION 1 -> 2: Cognito fields dropped from data, generate_index moved to options.

    `domain_id` stays untouched in `data` (it was never a version-1 vs -2
    concern) - only the three Cognito infrastructure fields and
    `generate_index` move.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "username": "user@example.com",
            "password": "hunter2",
            CONF_CLIENT_ID: "legacy-client-id",
            CONF_POOL_ID: "legacy-pool-id",
            CONF_POOL_REGION: "eu-west-1",
            CONF_DOMAIN_ID: "aaaaaaaa-0000-0000-0000-000000000001",
            CONF_INDEX: False,
        },
        options={},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert CONF_CLIENT_ID not in entry.data
    assert CONF_POOL_ID not in entry.data
    assert CONF_POOL_REGION not in entry.data
    assert CONF_INDEX not in entry.data
    assert entry.data[CONF_DOMAIN_ID] == "aaaaaaaa-0000-0000-0000-000000000001"
    assert entry.data["username"] == "user@example.com"
    assert entry.options[CONF_INDEX] is False


async def test_migrate_v1_to_v2_defaults_index_when_absent(
    hass: HomeAssistant,
) -> None:
    """`generate_index` defaults to True in options when never set in data (v1)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "username": "user@example.com",
            "password": "hunter2",
            CONF_DOMAIN_ID: "aaaaaaaa-0000-0000-0000-000000000001",
        },
        options={},
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)

    assert entry.options[CONF_INDEX] is True


async def test_migrate_preserves_entity_unique_ids(
    hass: HomeAssistant,
) -> None:
    """
    Migration must not touch any entity's unique_id.

    `unique_id` (`RadoffEntity.unique_id`, entity.py) is built from
    `device_id`/`reading_key`, neither of which the migration reads or
    writes - this test registers an entity against the v1 entry first, then
    asserts the same unique_id is still resolvable to the same entity after
    migration.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "username": "user@example.com",
            "password": "hunter2",
            CONF_CLIENT_ID: "legacy-client-id",
            CONF_POOL_ID: "legacy-pool-id",
            CONF_POOL_REGION: "eu-west-1",
            CONF_DOMAIN_ID: "aaaaaaaa-0000-0000-0000-000000000001",
        },
        options={},
    )
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
