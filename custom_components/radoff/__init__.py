"""The radoff integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CLIENT_ID, CONF_USERNAME, Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    CONF_POOL_ID,
    CONF_POOL_REGION,
    DOMAIN,
    ISSUE_MISSING_DOMAIN_ID,
)
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

    What this migration deliberately does NOT do (card RT-2926, finding
    T-06/F1): populate `domain_id`. A real version-1 entry - one created by
    the released version, 30e0cde - never contains it, because `domain_id`
    only came into existence with the multi-domain discovery of S-01/RT-2803,
    in this very same milestone. Obtaining it means authenticating against
    Cognito and calling `/auth/user/me/domains`, and the choice is genuinely
    ambiguous for an account with access to more than one domain. Decided
    with Piero: no network I/O happens here. A migration is executed during
    Home Assistant startup, cannot ask the user anything, and would have to
    guess; instead the entry migrates to version 2 without `domain_id`, and
    `async_setup_entry` below stops with an explicit `ConfigEntryError` plus
    a Repairs issue that carries the user into the fix flow in `repairs.py`,
    where discovery runs on demand and an ambiguous choice can actually be
    put to them.

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

        if not new_data.get(CONF_DOMAIN_ID):
            # The expected shape for any entry created by the released
            # version - see this function's docstring. Logged at info level
            # (not warning) because it is not a failure: it is the handover
            # to the Repairs fix flow, which `async_setup_entry` sets up.
            _LOGGER.info(
                "Config entry %s has no %s: it predates multi-domain discovery. "
                "A repair will be raised to complete its configuration",
                config_entry.entry_id,
                CONF_DOMAIN_ID,
            )

    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: RadoffConfigEntry
) -> bool:
    """Set up Example Integration from a config entry."""
    _LOGGER.debug("Radoff async_setup_entry")

    # Card RT-2926 / finding T-06/F1. Every entry created by the released
    # version reaches this point without a `domain_id` (see
    # `async_migrate_entry` above). This used to blow up three frames deeper
    # as a bare `KeyError: 'domain_id'` in `RadoffCoordinator.__init__`,
    # which - not being `ConfigEntryNotReady` - Home Assistant never retries
    # and cannot explain to anyone: the integration simply stayed broken.
    # Fail here instead, explicitly and in the user's own language, and
    # raise the Repairs issue whose fix flow (`repairs.py`) can actually
    # resolve the domain.
    if not config_entry.data.get(CONF_DOMAIN_ID):
        _async_create_missing_domain_issue(hass, config_entry)
        msg = f"Config entry {config_entry.entry_id} has no {CONF_DOMAIN_ID}"
        raise ConfigEntryError(
            msg,
            translation_domain=DOMAIN,
            translation_key=ISSUE_MISSING_DOMAIN_ID,
        )

    # Any issue left over from a previous, failed start (or from a domain
    # resolved through some other route, e.g. a re-added entry) is stale the
    # moment an entry does have its `domain_id`. Deleting an issue that was
    # never created is a no-op.
    _async_delete_missing_domain_issue(hass, config_entry)

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


def _missing_domain_issue_id(config_entry: ConfigEntry) -> str:
    """
    Return the Repairs issue id for one entry missing its `domain_id`.

    Scoped per entry id, not per integration: `manifest.json` declares
    `single_config_entry: false`, so two Radoff accounts can be configured
    side by side and each needs its own repair - resolving the domain of one
    says nothing about the other.
    """
    return f"{ISSUE_MISSING_DOMAIN_ID}_{config_entry.entry_id}"


@callback
def _async_create_missing_domain_issue(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """
    Raise the fixable Repairs issue for an entry with no `domain_id` (RT-2926).

    `is_persistent=False`: the issue is re-created by `async_setup_entry` on
    every start for as long as the entry actually needs it, so there is no
    value in keeping a stale copy across restarts. `data` carries the entry
    id, which is all `repairs.py::async_create_fix_flow` needs to find the
    entry it has to repair.

    The placeholder is the account's username, not `config_entry.title`:
    every Radoff entry is titled "Radoff" (see `config_flow.py`), so two
    configured accounts would otherwise produce two repair cards with the
    same words on them and nothing to tell which is which - seen while
    checking the rendered strings against the two-entry verification
    instance. The re-auth step already identifies an entry the same way
    (`reauth_confirm`'s `{username}`).
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _missing_domain_issue_id(config_entry),
        data={"entry_id": config_entry.entry_id},
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_MISSING_DOMAIN_ID,
        translation_placeholders={
            "username": config_entry.data.get(CONF_USERNAME, config_entry.title)
        },
    )


@callback
def _async_delete_missing_domain_issue(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Clear the `domain_id` repair for an entry that no longer needs it."""
    ir.async_delete_issue(hass, DOMAIN, _missing_domain_issue_id(config_entry))


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
