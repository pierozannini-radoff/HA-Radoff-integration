"""The radoff integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CLIENT_ID, Platform

from .const import CONF_INDEX, CONF_POOL_ID, CONF_POOL_REGION
from .coordinator import RadoffCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

type RadoffConfigEntry = ConfigEntry[RadoffCoordinator]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """
    Migrate a config entry to the current version.

    VERSION 1 -> 2 (S-02):
      - `client_id`, `pool_id`, `pool_region` are dropped from `data`. They are
        internal Cognito infrastructure constants (see const.py), not per-user
        configuration, and must no longer be persisted per entry: this is what
        allows a future app-client rotation to be a plain integration update
        instead of a manual fix for every installed entry.
      - `generate_index` moves from `data` to `options` (default True if it was
        never set), since it is a user preference rather than connection data.

    This function is idempotent: it only touches entries still at version 1,
    so a second (or later) restart of an already-migrated entry is a no-op.
    No entity's `unique_id` is affected by this migration.
    """
    _LOGGER.debug(
        "Checking radoff config entry %s for migration (version=%s)",
        config_entry.entry_id,
        config_entry.version,
    )

    if config_entry.version == 1:
        new_data = dict(config_entry.data)
        generate_index = new_data.pop(CONF_INDEX, True)
        new_data.pop(CONF_CLIENT_ID, None)
        new_data.pop(CONF_POOL_ID, None)
        new_data.pop(CONF_POOL_REGION, None)

        new_options = {**config_entry.options, CONF_INDEX: generate_index}

        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            options=new_options,
            version=2,
        )

        _LOGGER.debug(
            "Migrated radoff config entry %s from version 1 to version 2",
            config_entry.entry_id,
        )

    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: RadoffConfigEntry
) -> bool:
    """Set up Example Integration from a config entry."""
    _LOGGER.debug("Radoff async_setup_entry")

    coordinator = RadoffCoordinator(hass, config_entry)

    # `async_config_entry_first_refresh` already raises `ConfigEntryNotReady`
    # itself when the first refresh fails (see card S-06, C20): the extra
    # `if not coordinator.api.connected: raise ConfigEntryNotReady` that used
    # to follow this call was unreachable and has been removed.
    await coordinator.async_config_entry_first_refresh()

    # `async_on_unload` registers the listener's cancel callback to run when
    # this entry is unloaded (S-14), replacing the manual `RuntimeData.
    # cancel_update_listener` bookkeeping this used to need.
    config_entry.async_on_unload(
        config_entry.add_update_listener(_async_update_listener)
    )

    config_entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    return True


async def _async_update_listener(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Handle config options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: ConfigEntry,  # noqa: ARG001
    device_entry: DeviceEntry,  # noqa: ARG001
) -> bool:
    """Delete device if selected from UI."""
    return True


async def async_unload_entry(
    hass: HomeAssistant, config_entry: RadoffConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
