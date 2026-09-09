"""Constants for the radoff integration."""

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DOMAIN = "radoff"
CONF_POOL_ID = "pool_id"
CONF_POOL_REGION = "pool_region"
DEFAULT_SCAN_INTERVAL = 60

# Card S-11: MIN_SCAN_INTERVAL was 10 seconds and dead code (no OptionsFlow
# could ever write CONF_SCAN_INTERVAL - see finding F6). T-02's question 6
# ("rate limit ufficiali dell'API, per definire un update_interval di
# default difendibile") is still open in every analysis document in this
# project - no answer has come back from the Radoff backend team. Per this
# card's own instruction ("se non arriva risposta, alzarlo a un valore
# difendibile e documentarlo"), raised from 10 to 30: with the N+1 polling
# pattern (`get_devices()` = 1 search + 1 GET per device, see finding F5),
# 10 seconds is aggressive even for a single-device account, let alone the
# property-manager/multi-site accounts the analysis docs call out. 30 is a
# provisional, defensible floor, not a value derived from any confirmed
# rate limit - revisit once T-02 actually answers question 6.
MIN_SCAN_INTERVAL = 30

# Upper bound for the OptionsFlow's scan_interval NumberSelector (card
# S-11). Not a backend requirement, just a sane ceiling so the form can't be
# set to something the user would forget about (e.g. once a day).
MAX_SCAN_INTERVAL = 3600

CONF_INDEX = "generate_index"
CONF_DOMAIN_ID = "domain_id"

# Repairs issue raised when a config entry carries no `domain_id` at all
# (card RT-2926, finding T-06/F1). `domain_id` was born with the
# multi-domain discovery of S-01/RT-2803, in the same milestone that
# introduced config entry VERSION 2: no entry created by the released
# version (30e0cde) can possibly contain it, and `async_migrate_entry`
# (__init__.py) deliberately does not go online to invent one. The entry is
# therefore left in an explicit setup error and this issue is what carries
# the user to the fix flow in `repairs.py`, where the domain is discovered
# (and, when ambiguous, chosen) interactively.
ISSUE_MISSING_DOMAIN_ID = "missing_domain_id"

# Multiplier used to derive the "stale after" freshness threshold consumed by
# RadoffEntity.available (card S-07): a reading is considered fresh while its
# age is below `update_interval * DEFAULT_STALE_MULTIPLIER`. Card S-11
# evaluated exposing this as a third OptionsFlow field (per its own "COSA
# FARE": "moltiplicatore di staleness (o la sua esposizione va valutata,
# vedi S-07)") and, decided with Piero, left it as an internal constant: no
# acceptance criterion of S-11 requires it, and RadoffCoordinator.stale_after
# already tracks a changed scan_interval automatically (it is derived from
# `update_interval`, not stored separately - see coordinator.py). Revisit if
# a future card finds users need it independently of the poll interval.
DEFAULT_STALE_MULTIPLIER = 3

# Fraction of `update_interval` used as the overall wall-clock budget for one
# coordinator update cycle (card S-13). The API client's per-device fetch is
# still a synchronous, sequential N+1 pattern (1 search + 1 GET per device -
# see `api/client.py::get_devices`), not yet converted to aiohttp (tracked
# separately as an "L" item, see `architettura-target-sprint-m.md` §11), so
# a slow or unresponsive backend could otherwise let a single update cycle
# run well past `update_interval` itself with nothing to stop it.
# `RadoffCoordinator.async_update_data` wraps its work in
# `asyncio.timeout(update_interval * UPDATE_TIMEOUT_FACTOR)` and raises
# `UpdateFailed` with an explicit message if that budget is exceeded,
# instead of letting the cycle run indefinitely and the next one start late
# or overlap with a still-running one.
UPDATE_TIMEOUT_FACTOR = 0.8

# AWS Cognito defaults for Radoff API.
#
# These are internal implementation details, not secrets: a public Cognito app
# client is by definition distributed with any client that talks to it. They
# used to be exposed as required, user-editable fields in the config flow
# (client_id, pool_id, pool_region) and persisted verbatim into each config
# entry's `data` (see S-02). This meant a user could see and mistakenly edit
# production infrastructure values, and rotating the Cognito app client would
# silently break every existing installation, since the old values stayed
# written in users' entries forever.
#
# As of config entry VERSION 2, these are read directly from here by
# `api.py` (via `API.__init__` defaults) and are no longer part of the config
# flow schema or of `config_entry.data`. Rotating the app client is now a
# matter of updating these constants and releasing a new integration version;
# `async_migrate_entry` (see __init__.py) strips any leftover Cognito fields
# from entries created before this change.
DEFAULT_POOL_ID = "eu-west-1_zD4CSIZ6i"
DEFAULT_POOL_REGION = "eu-west-1"
DEFAULT_CLIENT_ID = "61ckd0c4qoq0ov7mmphrj7kstj"


def _load_manifest() -> dict[str, str]:
    """
    Read `manifest.json` next to this file.

    The integration version lives in exactly one place - `manifest.json`,
    bumped at release time (see card S-19) - so nothing else hardcodes it.
    A read failure here is only expected in an unpacked/dev checkout that is
    missing the file; degrade to a clearly-fallback value instead of crashing
    the whole integration over a User-Agent string (see card S-03).
    """
    manifest_path = Path(__file__).with_name("manifest.json")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOGGER.warning("Unable to read manifest.json to build the User-Agent")
        return {}


_MANIFEST = _load_manifest()

# Version and documentation URL used to build USER_AGENT below. Both come
# from manifest.json so they can never drift from what HACS/HA itself report
# for this integration.
INTEGRATION_VERSION = _MANIFEST.get("version", "0.0.0")
INTEGRATION_DOCUMENTATION_URL = _MANIFEST.get(
    "documentation", "https://github.com/radoff/ha-radoff-integration"
)

# Identifies our own traffic to the Radoff API instead of impersonating the
# official mobile app (see card S-03). Backend-side recognition of this
# prefix for segmentation/rate-limiting is tracked separately in T-02.
USER_AGENT = (
    f"HomeAssistant-Radoff/{INTEGRATION_VERSION} (+{INTEGRATION_DOCUMENTATION_URL})"
)
