"""The radoff integration."""

from __future__ import annotations

import logging
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

    VERSION 1 -> 3 and VERSION 2.x -> 3 (card M-07). The two paths differ in
    one step and end in the same place, so they are written as one pass
    rather than as a chain of per-version functions.

    What the 1 -> 2 step of S-02 did, and still does here:
      - `client_id`, `pool_id`, `pool_region` are dropped from `data`. They
        are internal Cognito infrastructure constants (see const.py), not
        per-user configuration, and must no longer be persisted per entry:
        this is what allows a future app-client rotation to be a plain
        integration update instead of a manual fix for every installed
        entry.
      - `generate_index` moves from `data` to `options` (default True if it
        was never set), since it is a user preference rather than connection
        data.

    What version 3 adds, and why it is the irreversible one:

    1. `domain_id` -> `domain_prefix`, which is a migration and not a
       rename. A version-1 entry has no domain at all (see below); a
       version-2 entry has the arch 1.x domain UUID, and arch 2.0 neither
       accepts a UUID nor knows how to map one to a prefix - the only
       authority on that mapping is `GET /data/user/me/domains`, which is
       network I/O. So the old key is dropped and no new one is invented:
       the entry reaches version 3 without a domain, `async_setup_entry`
       stops with an explicit `ConfigEntryError`, and the Repairs issue it
       raises carries the user to the fix flow in `repairs.py`, where
       discovery runs on demand and an ambiguous choice can actually be put
       to them. Decided with Piero, and unchanged since RT-2926 stated it
       for the version-1 case: no network I/O happens in a migration, which
       runs during Home Assistant startup and cannot ask anyone anything.

    2. Every entity identifier is rewritten onto the arch 2.0 form -
       `radoff-{serial}-{measure}` instead of `radoff-{device_uuid}-{slug}`
       - by `_async_migrate_unique_ids_to_serial` below. This is what
       preserves the user's history across the whole migration, and it is
       the half that cannot be undone.

    The entry is bumped to version 3 **whether or not it ends up with a
    domain**, and that is deliberate (decided with Piero). The two halves
    are independent: the re-keying needs no domain, only the device
    registry, and an entry left at its old version would run the whole
    migration again on every restart, re-log every warning, and have Home
    Assistant record a failed migration each time - all while the entity
    work it already did stands. The missing domain is not a failed
    migration, it is a question waiting for its user.

    Idempotence, which AC 7 asks for explicitly: running this twice changes
    nothing the second time. The version-1 step only touches entries still
    at version 1; the domain strip only removes a key that is there; and
    the re-keying computes the identifier it would write and skips the
    entity when that is the identifier it already has - so a second pass
    over an already-migrated entry performs zero registry writes.

    That idempotence is also what makes the re-keying safe to run outside
    this function, which `async_setup_entry` does on every start. It has
    to: the version bump here and the registry rewrite are two different
    stores with two different save delays, so a start interrupted between
    them would otherwise record "migrated" over entities that never were -
    and this function would never be called again to notice. Keeping the
    call here as well is not redundant: it is what re-keys an entry that
    migrates and loads in the same start, before any entity is created.
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

    # The arch 1.x domain UUID, if this entry has one. Not translated into a
    # `domain_prefix` - it cannot be, offline - and not kept either: leaving
    # it would only give a later reader something that looks like a domain
    # and is not one.
    legacy_domain_id = new_data.pop(_LEGACY_DOMAIN_ID_KEY, None)
    if legacy_domain_id:
        _LOGGER.info(
            "Config entry %s carried the arch 1.x domain id %s, which arch 2.0 "
            "cannot use: a repair will be raised to choose the domain again",
            config_entry.entry_id,
            legacy_domain_id,
        )
    elif not new_data.get(CONF_DOMAIN_PREFIX):
        # The expected shape for any entry created by the released version -
        # see this function's docstring. Logged at info level (not warning)
        # because it is not a failure: it is the handover to the Repairs fix
        # flow, which `async_setup_entry` sets up.
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


# The key every pre-version-3 entry stored its domain under. A frozen
# literal rather than a constant imported from const.py, which no longer has
# one: `CONF_DOMAIN_ID` is gone from the integration, and a migration is
# precisely the one place that still has to know a name nothing else uses.
_LEGACY_DOMAIN_ID_KEY = "domain_id"

# Arch 1.x entity slug -> the arch 2.0 measure name that replaces it.
#
# The left-hand side is every slug this integration has ever written into a
# `unique_id`: the nine property names of the released version's `data`
# bucket (30e0cde, `api.py::MAPPING`), plus the one slug S-10 minted for the
# aggregated bucket and RT-2927 re-keyed the pre-existing AQI entity
# onto. The right-hand side comes from `measures-ranges`, verified live on
# dev by M-04 (`scripts/verify_m04_live.py`, 17 PASS): the names are not
# ours to choose any more, which is why eight of the ten entries are
# identities and the two AQI ones are not.
#
# Frozen literals, deliberately, and not derived from any table the code
# still uses - same reasoning RT-2927 wrote down for `_AGGREGATED_AQI_SLUG`,
# which this map absorbs: a migration records what it actually wrote, at the
# time it wrote it, so that a later change to the live schema cannot
# silently move the identifiers a past release already put in users'
# registries.
#
# Three measures of the 2.0 catalogue are absent on purpose - `radon_bqm3`,
# `co`, `ch4` did not exist in the released version, so there is nothing of
# theirs to migrate. They are simply created (card M-04).
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

# The slugs of the aggregated-data bucket of arch 1.x, which 2.0 does not
# have. (Spelled out in words rather than with the bucket's own JSON key, so
# that the arch 1.x tripwire in `tests/test_api_transport.py` keeps meaning
# what it says: no leftover of that data model in the integration.)
#
# There is exactly one, and this is the whole of what the card's "le entità
# del bucket aggregato che non hanno corrispondente in 2.0 vanno rimosse dal
# registry" can mean once the 1.x responses are read rather than assumed:
# that bucket only ever carried `airqualityindex` (30e0cde,
# `api.py::MAPPING`), so `airqualityindex_average` is the only `*_average`
# identifier any installation can hold.
#
# It *does* have a counterpart - `aqi_value` - so the normal outcome for it
# is to be re-keyed like everything else, keeping its history. Removal is
# the collision case only: an installation that ran an intermediate build of
# this milestone holds both the pre-existing `airqualityindex` entity and a
# few days of `airqualityindex_average`, and both map to `aqi_value`. One of
# them wins the identifier (the pre-existing one - see the ordering in
# `_async_migrate_unique_ids_to_serial`), and the loser is what this set is
# for: an entity that can never again receive a value, left `unavailable`
# forever unless it is removed. No released version can produce that state;
# only a dev or verification instance can.
_REMOVABLE_ON_COLLISION = {"airqualityindex_average"}

# The measure whose unit disappears between the released version and arch
# 2.0 (card M-04, T-02 D-07): tvoc was published as µg/m³ with device class
# `volatile_organic_compounds`, and is published as `V - Ix` with no device
# class now, because that is what the API declares and it is not a
# concentration. See `_async_clear_stale_unit_option` for what this
# migration does about it, and for what it deliberately does not.
_UNIT_CHANGED_MEASURES = {"tvoc"}

# Suffix the qualitative sibling of a measure carries in its `unique_id`.
#
# Unchanged from arch 1.x, and worth stating because the other string for
# the same entity did change: the *translation key* is `{measure}_index`,
# with an underscore (card M-04). Two different strings for two different
# purposes; only this one belongs in an identifier.
_INDEX_SUFFIX = "-index"


def _parse_legacy_unique_id(unique_id: str) -> tuple[str, bool] | None:
    """
    Return `(slug, is_index)` for an arch 1.x `unique_id`, or None.

    The shape is `radoff-{device_uuid}-{slug}` with an optional `-index`,
    and it is parsed from the right because that is the only end of it that
    is unambiguous: the UUID in the middle contains four dashes of its own,
    while no slug this integration has ever minted contains one (they are
    lower-case identifiers with underscores - `internal_temperature`,
    `airqualityindex_average`). So the last dash-separated token is the
    slug, whatever the shape of what precedes it.

    None means "not something this migration recognises", which covers both
    an identifier from another integration and one this card has already
    rewritten - `radoff-{serial}-aqi_value` parses to the slug `aqi_value`,
    which is not an arch 1.x slug and so is not in the map the caller looks
    it up in.
    """
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
    """
    Return the Radoff serial number of one device registry entry.

    This is the whole of what makes the re-keying an offline migration
    (decided with Piero, and the reason no alternative was needed): the
    device registry has been keyed on `(radoff, serial_number)` since S-07
    and was left untouched by M-03 precisely so that this would hold, so
    every entity already knows its serial through its device - no network
    call, no arch 1.x UUID to translate, and a deterministic answer.

    `None` when the entity has no device, or its device carries no Radoff
    identifier. Both are the caller's cue to leave that entity alone.
    """
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

    Card M-07, and the honest scope of it. What this clears is the *user's*
    display-unit override (`options["sensor"]["unit_of_measurement"]`, the
    "unit of measurement" field of the entity settings dialog): a tvoc
    entity set to display mg/m³ is set to display a conversion of µg/m³, and
    after this migration the entity has no µg/m³ to convert - Home Assistant
    would reject or ignore the override, with a message about a unit that no
    longer applies.

    What it explicitly does **not** do is repair the long-term statistics.
    Those live in the recorder's own `statistics_meta`, not in the entity
    registry, and Home Assistant treats a unit change on an existing entity
    as a break in the series by design: the old series is closed and a new
    one starts, leaving a visible gap. Rewriting a user's recorded history
    is not something a config-entry migration should reach into - so the gap
    is announced instead (README, "what changes when you update"), which is
    the same treatment the ~4 °C temperature step gets and for the same
    reason: a known, explained discontinuity beats a mysterious one.
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
    Re-key every entity of one entry onto `radoff-{serial}-{measure}` (card M-07).

    This is the step the whole card exists for. Without it, an installation
    updating to arch 2.0 keeps its 32 entities in the registry, fed by
    nothing and permanently `unavailable`, while 32 brand-new ones appear
    beside them carrying the same values with no history, no dashboard card
    pointing at them and no automation referencing them. Renaming the
    `unique_id` - rather than the entity - is what prevents that: the
    registry keeps the same `entity_id`, so `states` and the hourly
    `statistics` series stay attached to it.

    The `entity_id` is deliberately left alone (`sensor.*_qualita_aria` keeps
    its name even where the measure behind it is now called `aqi_value`):
    Home Assistant never rewrites an `entity_id` on its own, and doing it
    here would break every dashboard card and automation referring to it -
    exactly the damage this migration exists to prevent.

    Three cautions, two of them inherited verbatim from RT-2927's AQI step
    (`_async_migrate_aggregated_aqi_unique_ids`, which this function
    replaces and absorbs):

    - **a target identifier that is already taken is never overwritten.**
      `unique_id` is unique per (domain, platform) across the *whole*
      registry, not per config entry, so the collision is looked up
      globally: `async_update_entity` raises on a duplicate, and an
      exception here would abort the migration and leave the entry
      unloadable - a worse outcome than the orphaning being fixed. Unlike
      RT-2927, the colliding entity is never *removed* to make room, not
      even when this entry owns it: removing an entity is the one action in
      this function that destroys a user's history, and it is reserved for
      the single case that has nothing to lose (see
      `_REMOVABLE_ON_COLLISION`).
    - **an entity with no device, or whose device carries no Radoff
      identifier, is left exactly as it is** and logged. The serial is only
      knowable through the device, so there is nothing to compute and
      guessing is not an option.
    - **an unrecognised identifier is left alone**, silently at INFO. That
      covers an entity this card has already migrated, which is what makes
      a second pass free.

    The iteration order is not incidental. Entities are processed with the
    bare arch 1.x slugs first, so that where two of them map to the same
    measure - only `airqualityindex` and `airqualityindex_average` can, and
    only on an instance that ran an intermediate build of this milestone -
    the pre-existing entity, the one carrying the history, is the one that
    takes the identifier.
    """
    registry = er.async_get(hass)
    entity_entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)

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
            _async_handle_collision(
                registry, entity_entry, slug, new_unique_id, collision_entity_id
            )
            continue

        if measure in _UNIT_CHANGED_MEASURES:
            _async_clear_stale_unit_option(registry, entity_entry.entity_id)

        registry.async_update_entity(
            entity_entry.entity_id, new_unique_id=new_unique_id
        )

        _LOGGER.info(
            "Migrated %s from unique_id %s to %s",
            entity_entry.entity_id,
            entity_entry.unique_id,
            new_unique_id,
        )


@callback
def _async_handle_collision(
    registry: er.EntityRegistry,
    entity_entry: er.RegistryEntry,
    slug: str,
    new_unique_id: str,
    collision_entity_id: str,
) -> None:
    """
    Deal with one entity whose new identifier is already someone else's.

    Two outcomes, and the line between them is whether the entity being
    migrated has anything left to lose.

    The `airqualityindex_average` entity of an instance that also holds the
    pre-existing `airqualityindex` one is a duplicate of a measure that now
    has a single identifier: it lost the race for `aqi_value` by
    construction (see the ordering in the caller), it will never receive
    another value, and leaving it means leaving an entity that reads
    `unavailable` in someone's dashboard for the rest of the installation's
    life. That one is removed - the card's AC in as many words, and the only
    removal this migration performs.

    Everything else is left strictly alone and logged at WARNING. The
    colliding entity may belong to another config entry (two Radoff accounts
    reporting the same device), in which case it is not this entry's to
    touch at all; or it may be a genuine ambiguity nobody has seen yet,
    which is a thing to report rather than to resolve by guessing. The
    migration still completes, and the entity is still there to look at.
    """
    collision = registry.async_get(collision_entity_id)

    if slug in _REMOVABLE_ON_COLLISION and collision is not None:
        _LOGGER.warning(
            "Removing %s: its measure now lives on %s (unique_id %s), and an "
            "entity that can never receive a value again is worse kept than "
            "removed",
            entity_entry.entity_id,
            collision.entity_id,
            new_unique_id,
        )
        registry.async_remove(entity_entry.entity_id)
        return

    _LOGGER.warning(
        "Not migrating %s: unique_id %s is already held by %s. The entity is "
        "left untouched",
        entity_entry.entity_id,
        new_unique_id,
        collision_entity_id,
    )


async def async_setup_entry(
    hass: HomeAssistant, config_entry: RadoffConfigEntry
) -> bool:
    """Set up Example Integration from a config entry."""
    _LOGGER.debug("Radoff async_setup_entry")

    # Card M-07, and the reason this runs here and not only inside
    # `async_migrate_entry`: the version bump and the registry rewrite are
    # not one write, and they are not even saved on the same schedule.
    # Home Assistant persists config entries a second after they change and
    # the entity registry ten seconds after; a restart, a crash or a Ctrl+C
    # inside that window leaves an entry that says "version 3" on disk over
    # a registry that was never rewritten - and since Home Assistant only
    # calls the migration handler when the versions differ, the next start
    # would never look at those entities again. Seen for real on the
    # verification instance (docs/M-07-verifica-dev.md): sixteen entities
    # left on arch 1.x identifiers, permanently, with their history
    # attached to them and a fresh set of entities about to appear beside
    # them - the exact damage this card exists to prevent.
    #
    # Running it at every setup closes that window: the re-keying is
    # idempotent by construction (it computes the identifier it would write
    # and skips the entity when that is the one it already has), so a pass
    # over an already-migrated entry performs no registry write at all and
    # costs one scan of that entry's entities. It runs *before* the domain
    # guard below on purpose - an entry with no domain still has entities
    # to put right, and it may sit there unloadable until someone gets
    # round to the repair.
    _async_migrate_unique_ids_to_serial(hass, config_entry)

    # Card RT-2926 / finding T-06/F1. Every entry created by the released
    # version reaches this point without a domain (see `async_migrate_entry`
    # above). This used to blow up three frames deeper as a bare
    # `KeyError: 'domain_id'` in `RadoffCoordinator.__init__`, which - not
    # being `ConfigEntryNotReady` - Home Assistant never retries and cannot
    # explain to anyone: the integration simply stayed broken. Fail here
    # instead, explicitly and in the user's own language, and raise the
    # Repairs issue whose fix flow (`repairs.py`) can actually resolve the
    # domain.
    #
    # Card M-07 changes only which key is read, and widens who arrives here:
    # an entry created in QA against arch 1.x has a `domain_id` UUID that
    # the migration dropped, because arch 2.0 cannot use it and nothing can
    # translate it offline. Both cases need the same answer from the same
    # person, so both get the same issue.
    if not config_entry.data.get(CONF_DOMAIN_PREFIX):
        async_create_missing_domain_prefix_issue(hass, config_entry)
        msg = f"Config entry {config_entry.entry_id} has no {CONF_DOMAIN_PREFIX}"
        raise ConfigEntryError(
            msg,
            translation_domain=DOMAIN,
            translation_key=ISSUE_MISSING_DOMAIN_PREFIX,
        )

    # Any issue left over from a previous, failed start (or from a domain
    # resolved through some other route, e.g. a re-added entry) is stale the
    # moment an entry does have a domain it can poll - including the 403
    # repair a past poll may have raised, since the domain on the entry now
    # may well be a different one. Deleting an issue that was never created
    # is a no-op.
    async_delete_domain_issues(hass, config_entry)

    coordinator = RadoffCoordinator(hass, config_entry)

    # `async_config_entry_first_refresh` already raises `ConfigEntryNotReady`
    # itself when the first refresh fails (see card S-06, C20): the extra
    # `if not coordinator.api.connected: raise ConfigEntryNotReady` that used
    # to follow this call was unreachable and has been removed.
    await coordinator.async_config_entry_first_refresh()

    # Card M-04: the measurement schema, one call per device type seen in
    # that first refresh, cached on the coordinator for the sensor platform
    # forwarded below. The order is not incidental - the refresh is what
    # makes the types known, and the platform is what consumes the schema,
    # so this is the only point in the sequence where it fits.
    #
    # An unrecognised type is handled inside `async_load_schemas` (WARNING,
    # device kept). What reaches here is the other kind of failure: the
    # endpoint did not answer at all. That is a "not ready" state, not a
    # broken installation - retrying is what Home Assistant does with
    # `ConfigEntryNotReady`, and it is much better than completing a setup
    # whose entities would stay nameless and unitless until the next
    # restart. There is deliberately no hardcoded fallback schema to use
    # instead: that table is what this card exists to delete.
    try:
        await coordinator.async_load_schemas()
    except (AuthInvalidError, AuthExpiredError, AuthChallengeRequiredError) as err:
        # The narrow window the first refresh cannot cover: a token that
        # expires, or credentials that stop working, between that call and
        # this one. Left to the clause below it would become "not ready"
        # and retry forever without ever asking the user for a password -
        # the exact defect S-08/C7 fixed on the poll path. The coordinator
        # maps the same three to `ConfigEntryAuthFailed` for the same
        # reason (see `_async_update_data`).
        raise ConfigEntryAuthFailed(str(err)) from err
    except APIRateLimitError as err:
        # Card M-05. The defect the poll path's 429 clause exists to avoid
        # does not arise here - at setup there are no entities yet to mark
        # unavailable - so this stays `ConfigEntryNotReady` and the retry is
        # Home Assistant's, with its own backoff, not `err.retry_after`.
        # What this clause adds over the catch-all below is that a 429 is
        # named as a 429 in the log, with the delay the client computed, so
        # a rate-limited stage is recognisable in a user's log instead of
        # reading as "the schema endpoint is down".
        #
        # Note the schema call is not part of the poll budget: it runs once
        # per device type at setup (five types in the whole catalogue), not
        # once per cycle - see `RadoffCoordinator.async_load_schemas`.
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
