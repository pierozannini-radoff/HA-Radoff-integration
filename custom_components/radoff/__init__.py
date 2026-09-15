"""The radoff integration."""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CLIENT_ID, Platform
from homeassistant.core import callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api import (
    APIRateLimitError,
    AuthChallengeRequiredError,
    AuthExpiredError,
    AuthInvalidError,
)
from .const import (
    CONF_DOMAIN_PREFIX,
    CONF_INDEX,
    CONF_POOL_ID,
    CONF_POOL_REGION,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    ISSUE_MISSING_DOMAIN_PREFIX,
)
from .coordinator import RadoffCoordinator
from .issues import (
    async_create_missing_domain_prefix_issue,
    async_delete_domain_issues,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

PLATFORMS: list[Platform] = [Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

type RadoffConfigEntry = ConfigEntry[RadoffCoordinator]


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """
    Migrate a config entry to the current version.

    Every path ends at version 3: Cognito constants and the legacy domain id
    are dropped from `data`, `generate_index` moves to `options`, and every
    entity identifier is rewritten onto `radoff-{serial}-{measure}`, which is
    what preserves the user's history and is the half that cannot be undone.

    No domain is invented to replace the dropped one: only the API can map
    the old id, and a migration runs at startup where no network call and no
    question to the user are possible. The entry is bumped either way - a
    missing domain is a question waiting for its user, not a failed
    migration, and leaving the version behind would replay this on every
    restart.

    Running it twice writes nothing the second time, which is also what makes
    the re-keying safe to repeat at setup: the version bump and the registry
    rewrite live in two stores with two save delays, so a start interrupted
    between them must not leave entities un-keyed for good.
    """
    _LOGGER.debug(
        "Checking radoff config entry %s for migration (version=%s.%s)",
        config_entry.entry_id,
        config_entry.version,
        config_entry.minor_version,
    )

    if config_entry.version >= CONFIG_ENTRY_VERSION:
        return True

    new_data = dict(config_entry.data)
    new_options = dict(config_entry.options)

    if config_entry.version == 1:
        new_options[CONF_INDEX] = new_data.pop(CONF_INDEX, True)
        new_data.pop(CONF_CLIENT_ID, None)
        new_data.pop(CONF_POOL_ID, None)
        new_data.pop(CONF_POOL_REGION, None)

    # Not translatable offline, and not kept either: it would leave a later
    # reader something that looks like a domain and is not one.
    legacy_domain_id = new_data.pop(_LEGACY_DOMAIN_ID_KEY, None)
    if legacy_domain_id:
        # The repair is the part a user can act on; which id was dropped is
        # for whoever is reading the entry alongside the registry.
        _LOGGER.info(
            "Config entry %s carried an arch 1.x domain id, which arch 2.0 "
            "cannot use: a repair will be raised to choose the domain again",
            config_entry.entry_id,
        )
        _LOGGER.debug(
            "The arch 1.x domain id dropped from config entry %s is %s",
            config_entry.entry_id,
            legacy_domain_id,
        )
    elif not new_data.get(CONF_DOMAIN_PREFIX):
        # Info, not warning: this is the handover to the Repairs flow, not a
        # failure.
        _LOGGER.info(
            "Config entry %s has no %s: it predates multi-domain discovery. "
            "A repair will be raised to complete its configuration",
            config_entry.entry_id,
            CONF_DOMAIN_PREFIX,
        )

    _async_migrate_unique_ids_to_serial(hass, config_entry)

    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
        options=new_options,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
    )

    _LOGGER.debug(
        "Migrated radoff config entry %s to version 3",
        config_entry.entry_id,
    )

    return True


# The key every pre-version-3 entry stored its domain under: a migration is
# the one place that still has to know a name nothing else uses.
_LEGACY_DOMAIN_ID_KEY = "domain_id"

# Legacy slug -> the measure that replaces it. Frozen literals, never derived
# from a live table: a migration records what it actually wrote.
_LEGACY_SLUG_TO_MEASURE = {
    "airqualityindex": "aqi_value",
    "airqualityindex_average": "aqi_value",
    "eco2": "eco2",
    "internal_temperature": "internal_temperature",
    "pm1": "pm1",
    "pm10": "pm10",
    "pm25": "pm25",
    "pressure": "pressure",
    "relative_humidity": "relative_humidity",
    "tvoc": "tvoc",
}

# Removed rather than kept when two slugs re-key onto one identifier: the
# loser of that collision could never receive a value again.
_REMOVABLE_ON_COLLISION = {"airqualityindex_average"}

# Measures whose unit changes with this migration: tvoc moves off µg/m³ onto
# the unit the API declares, which is not a concentration.
_UNIT_CHANGED_MEASURES = {"tvoc"}

# Suffix the qualitative sibling carries in its `unique_id`, distinct from its
# translation key `{measure}_index`.
_INDEX_SUFFIX = "-index"


def _parse_legacy_unique_id(unique_id: str) -> tuple[str, bool] | None:
    """Return `(slug, is_index)` for a legacy `unique_id`, or None."""
    if not unique_id.startswith(f"{DOMAIN}-"):
        return None

    remainder = unique_id.removeprefix(f"{DOMAIN}-")

    is_index = remainder.endswith(_INDEX_SUFFIX)
    if is_index:
        remainder = remainder.removesuffix(_INDEX_SUFFIX)

    _, separator, slug = remainder.rpartition("-")
    if not separator or not slug:
        return None

    return slug, is_index


@callback
def _async_serial_of_device(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Return the Radoff serial number of one device registry entry."""
    if device_id is None:
        return None

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None

    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            return identifier

    return None


@callback
def _async_clear_stale_unit_option(registry: er.EntityRegistry, entity_id: str) -> None:
    """
    Drop a unit override the user set for a measure that no longer has that unit.

    Clears only the user's display-unit override, which Home Assistant would
    otherwise reject against a unit that no longer applies. It does not touch
    the long-term statistics: those live in the recorder, where a unit change
    closes one series and opens another, and a config-entry migration has no
    business rewriting a user's recorded history.
    """
    entry = registry.async_get(entity_id)
    if entry is None:
        return

    sensor_options = dict(entry.options.get("sensor") or {})
    if not sensor_options.pop("unit_of_measurement", None):
        return

    registry.async_update_entity_options(entity_id, "sensor", sensor_options or None)

    _LOGGER.info(
        "Cleared the display-unit override of %s: the measure it belonged to "
        "does not carry that unit any more",
        entity_id,
    )


@callback
def _async_migrate_unique_ids_to_serial(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """
    Re-key every entity of one entry onto `radoff-{serial}-{measure}`.

    Rewriting the `unique_id` rather than the entity is what keeps a user's
    history: the registry keeps the same `entity_id`, so states and
    statistics stay attached to it, and every dashboard and automation
    referring to it keeps working.

    A target identifier that is already taken is never overwritten -
    `unique_id` is unique registry-wide, and the duplicate would abort the
    migration - and an entity whose serial cannot be read from its device is
    left alone rather than guessed at. Entities carrying a bare slug are
    processed first, so where two of them map to one measure the pre-existing
    one, the one with the history, takes the identifier.
    """
    registry = er.async_get(hass)
    entity_entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    outcomes: Counter[str] = Counter()

    def migration_order(entity_entry: er.RegistryEntry) -> tuple[int, str]:
        parsed = _parse_legacy_unique_id(entity_entry.unique_id)
        deferred = parsed is not None and parsed[0] in _REMOVABLE_ON_COLLISION
        return (1 if deferred else 0, entity_entry.unique_id)

    for entity_entry in sorted(entity_entries, key=migration_order):
        parsed = _parse_legacy_unique_id(entity_entry.unique_id)
        if parsed is None:
            continue

        slug, is_index = parsed
        measure = _LEGACY_SLUG_TO_MEASURE.get(slug)
        if measure is None:
            _LOGGER.debug(
                "Leaving %s alone: %s is not an identifier this migration knows",
                entity_entry.entity_id,
                entity_entry.unique_id,
            )
            continue

        serial = _async_serial_of_device(hass, entity_entry.device_id)
        if serial is None:
            outcomes["no_serial"] += 1
            _LOGGER.warning(
                "Not migrating %s: it has no device to read a serial number "
                "from, so its new unique_id cannot be computed",
                entity_entry.entity_id,
            )
            continue

        new_unique_id = f"{DOMAIN}-{serial}-{measure}"
        if is_index:
            new_unique_id += _INDEX_SUFFIX

        if new_unique_id == entity_entry.unique_id:
            continue

        collision_entity_id = registry.async_get_entity_id(
            entity_entry.domain, entity_entry.platform, new_unique_id
        )
        if collision_entity_id is not None:
            outcomes[
                _async_handle_collision(
                    registry, entity_entry, slug, new_unique_id, collision_entity_id
                )
            ] += 1
            continue

        if measure in _UNIT_CHANGED_MEASURES:
            _async_clear_stale_unit_option(registry, entity_entry.entity_id)

        registry.async_update_entity(
            entity_entry.entity_id, new_unique_id=new_unique_id
        )

        outcomes["migrated"] += 1
        # The entity_id is the handle a support request is written around and
        # is visible in the interface anyway; the pair of identifiers is what
        # a registry query needs, and carries the serial.
        _LOGGER.info(
            "Migrated %s onto its serial-based identifier",
            entity_entry.entity_id,
        )
        _LOGGER.debug(
            "Migrated %s from unique_id %s to %s",
            entity_entry.entity_id,
            entity_entry.unique_id,
            new_unique_id,
        )

    _async_log_migration_summary(config_entry, outcomes)


@callback
def _async_log_migration_summary(
    config_entry: ConfigEntry, outcomes: Counter[str]
) -> None:
    """Log what a whole migration run did, silent when it did nothing."""
    summary = (
        "Radoff entity migration of config entry %s: %d migrated, %d removed "
        "as duplicates, %d left alone (%d with no device to read a serial "
        "from, %d whose new identifier belongs to another entity)"
    )
    args = (
        config_entry.entry_id,
        outcomes["migrated"],
        outcomes["removed"],
        outcomes["no_serial"] + outcomes["kept"],
        outcomes["no_serial"],
        outcomes["kept"],
    )

    if not outcomes:
        # This runs again at every setup, where having nothing to do is the
        # normal case and one line per restart would be noise.
        _LOGGER.debug(summary, *args)
        return

    _LOGGER.info(summary, *args)


@callback
def _async_handle_collision(
    registry: er.EntityRegistry,
    entity_entry: er.RegistryEntry,
    slug: str,
    new_unique_id: str,
    collision_entity_id: str,
) -> str:
    """
    Deal with one entity whose new identifier is already someone else's.

    The line between the two outcomes is whether the entity has anything
    left to lose: one that lost the race for a measure by construction can
    never receive a value again and is removed, the only removal this
    migration performs. Everything else is left alone and logged - the
    colliding entity may belong to another config entry entirely, and a
    genuine ambiguity is to report, not to resolve by guessing.

    Returns
    -------
        Which of the two outcomes this entity got, to count in the summary.

    """
    collision = registry.async_get(collision_entity_id)

    if slug in _REMOVABLE_ON_COLLISION and collision is not None:
        _LOGGER.warning(
            "Removing %s: its measure now lives on %s, and an entity that can "
            "never receive a value again is worse kept than removed",
            entity_entry.entity_id,
            collision.entity_id,
        )
        _LOGGER.debug(
            "%s was removed in favour of %s, which holds unique_id %s",
            entity_entry.entity_id,
            collision.entity_id,
            new_unique_id,
        )
        registry.async_remove(entity_entry.entity_id)
        return "removed"

    _LOGGER.warning(
        "Not migrating %s: its new identifier is already held by %s. The "
        "entity is left untouched",
        entity_entry.entity_id,
        collision_entity_id,
    )
    _LOGGER.debug(
        "%s keeps unique_id %s: %s already holds %s",
        entity_entry.entity_id,
        entity_entry.unique_id,
        collision_entity_id,
        new_unique_id,
    )
    return "kept"


async def async_setup_entry(
    hass: HomeAssistant, config_entry: RadoffConfigEntry
) -> bool:
    """Set up Radoff from a config entry."""
    _LOGGER.debug("Radoff async_setup_entry")

    # Repeated from the migration: a restart between the version bump and the
    # registry rewrite would leave entities un-keyed with nothing to notice.
    _async_migrate_unique_ids_to_serial(hass, config_entry)

    # Fail here, translated, rather than three frames deeper as a bare
    # `KeyError` Home Assistant can neither retry nor explain.
    if not config_entry.data.get(CONF_DOMAIN_PREFIX):
        async_create_missing_domain_prefix_issue(hass, config_entry)
        msg = f"Config entry {config_entry.entry_id} has no {CONF_DOMAIN_PREFIX}"
        raise ConfigEntryError(
            msg,
            translation_domain=DOMAIN,
            translation_key=ISSUE_MISSING_DOMAIN_PREFIX,
        )

    # Any domain issue is stale once the entry has a domain it can poll.
    # Deleting one that was never created is a no-op.
    async_delete_domain_issues(hass, config_entry)

    coordinator = RadoffCoordinator(hass, config_entry)

    await coordinator.async_config_entry_first_refresh()

    # The order is forced: the refresh makes the types known, the platform
    # below consumes the schema. A failure here is retried, not completed.
    try:
        await coordinator.async_load_schemas()
    except (AuthInvalidError, AuthExpiredError, AuthChallengeRequiredError) as err:
        # The window the first refresh cannot cover: credentials that stop
        # working between that call and this one.
        raise ConfigEntryAuthFailed(str(err)) from err
    except APIRateLimitError as err:
        # Retried with Home Assistant's own backoff, and named separately so
        # a rate-limited stage is recognisable in the log.
        _LOGGER.warning(
            "Radoff rate limit reached while fetching the measurement "
            "schema at setup (the API asked for ~%.1fs of room); Home "
            "Assistant will retry this entry: %s",
            err.retry_after,
            err,
        )
        msg = f"Rate limited by the Radoff API: {err}"
        raise ConfigEntryNotReady(msg) from err
    except Exception as err:
        _LOGGER.warning(
            "Radoff could not fetch the measurement schema at setup: %s. "
            "Home Assistant will retry this entry",
            err,
        )
        msg = f"Radoff measurement schema unavailable: {err}"
        raise ConfigEntryNotReady(msg) from err

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
