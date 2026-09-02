"""Constants for the radoff integration."""

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DOMAIN = "radoff"
CONF_POOL_ID = "pool_id"
CONF_POOL_REGION = "pool_region"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 10
CONF_INDEX = "generate_index"
CONF_DOMAIN_ID = "domain_id"

# Multiplier used to derive the "stale after" freshness threshold consumed by
# RadoffEntity.available (card S-07): a reading is considered fresh while its
# age is below `update_interval * DEFAULT_STALE_MULTIPLIER`. Not exposed as a
# config option yet - see card S-11 for whether that turns out to be worth
# doing; today it is only ever read from here.
DEFAULT_STALE_MULTIPLIER = 3

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
