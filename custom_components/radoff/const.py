"""Constants for the radoff integration."""

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

DOMAIN = "radoff"
CONF_POOL_ID = "pool_id"
CONF_POOL_REGION = "pool_region"

# Default poll interval, in seconds: one request per cycle against a quota of
# 50 req/s shared, per stage, with Radoff's own apps.
DEFAULT_SCAN_INTERVAL = 300

# Floor for the poll interval, set by the device cadence rather than by any
# rate limit: a device emits one message per minute, radon every five.
MIN_SCAN_INTERVAL = 60

# Ceiling for the poll interval, so the form cannot be set to once a day.
MAX_SCAN_INTERVAL = 3600

CONF_INDEX = "generate_index"

# Current config entry version, and the single place it is written down.
CONFIG_ENTRY_VERSION = 3
CONFIG_ENTRY_MINOR_VERSION = 1

# The tenant domain persisted on a config entry: a human-readable prefix,
# passed as a query parameter on every domain-scoped call.
CONF_DOMAIN_PREFIX = "domain_prefix"

# Base URL, and the single place a host is written down: the API version
# lives in the hostname, so another environment is another host.
DEFAULT_BASE_URL = "https://v2.api.dev.iot.radoff.life"

# Per-entry override of `DEFAULT_BASE_URL`. In `options`, not `data`: it is
# where to reach the API, not connection identity.
CONF_BASE_URL = "base_url"

# HTTP 429 backoff. The response carries no `Retry-After`, so the delay is
# this client's to choose; the cap is one poll interval.
RATE_LIMIT_BACKOFF_START = 5
RATE_LIMIT_BACKOFF_MAX = 300

# Fraction of the backoff drawn at random and subtracted, so installations
# hitting the same 429 do not come back in lockstep.
RATE_LIMIT_BACKOFF_JITTER = 0.25

# Fraction of the poll interval drawn once per entry and added to every
# cycle. One-sided: subtracting would breach the floor above.
POLL_JITTER_FRACTION = 0.10

# Repair for an entry with no usable domain: it does not know which domain to
# poll, and only the user can settle it.
ISSUE_MISSING_DOMAIN_PREFIX = "missing_domain_prefix"

# Repair for an entry the API answers 403 on. Same fix flow, distinct id so
# the wording can say access was lost rather than never granted.
ISSUE_DOMAIN_ACCESS_DENIED = "domain_access_denied"

# Translation key of the setup error raised on that same 403. Not an auth
# failure: the credentials are valid, so asking for the password is a dead end.
ERROR_DOMAIN_ACCESS_DENIED = "domain_access_denied"

# Fraction of `update_interval` budgeted for one update cycle: a backstop
# against a hung cycle overlapping the next, not a per-request timeout.
UPDATE_TIMEOUT_FACTOR = 0.8

# The fields this integration holds back, in three layers because they do not
# carry the same risk. One declaration with three readers: the diagnostics
# redaction, the level a log point is allowed to write at, and the scrubber
# in `redact.py`. A field added here is a field whose log points are reviewed
# in the same change.

# Credentials. They leave the process nowhere: not in the diagnostics dump,
# not in a log record, not at DEBUG.
SECRETS = frozenset(
    {
        "password",
        "IdToken",
        "AccessToken",
        "RefreshToken",
    }
)

# They name a person. Redacted in the dump and kept out of the log at every
# level, DEBUG included: `entry_id` already tells two entries apart without
# naming whoever owns them. A config entry's own `unique_id` is the
# normalised username, which is why the key is here.
PERSONAL = frozenset(
    {
        "username",
        "unique_id",
        "title",
    }
)

# They name an organisation or one physical device. Redacted in the dump, and
# written to the log at DEBUG only, which is a level a user turns on
# deliberately. An entity's `unique_id` carries the serial, so it falls under
# this rule by its value while its key sits in `PERSONAL`.
TENANT = frozenset(
    {
        "domain_prefix",
        "domain_id",
        "serial",
        "serial_number",
        "device_id",
    }
)

# AWS Cognito defaults. Not secrets: a public app client is distributed with
# every client that talks to it, and rotating it is a release.
DEFAULT_POOL_ID = "eu-west-1_zD4CSIZ6i"
DEFAULT_POOL_REGION = "eu-west-1"
DEFAULT_CLIENT_ID = "61ckd0c4qoq0ov7mmphrj7kstj"


def _load_manifest() -> dict[str, str]:
    """Read `manifest.json` next to this file, falling back to an empty dict."""
    manifest_path = Path(__file__).with_name("manifest.json")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LOGGER.warning("Unable to read manifest.json to build the User-Agent")
        return {}


_MANIFEST = _load_manifest()

# Read from manifest.json so they cannot drift from what HACS and Home
# Assistant report for this integration.
INTEGRATION_VERSION = _MANIFEST.get("version", "0.0.0")
INTEGRATION_DOCUMENTATION_URL = _MANIFEST.get(
    "documentation", "https://github.com/radoff/ha-radoff-integration"
)

# Identifies this integration's traffic instead of impersonating the app.
USER_AGENT = (
    f"HomeAssistant-Radoff/{INTEGRATION_VERSION} (+{INTEGRATION_DOCUMENTATION_URL})"
)
