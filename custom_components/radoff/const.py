"""Constants for the radoff integration."""

DOMAIN = "radoff"
CONF_POOL_ID = "pool_id"
CONF_POOL_REGION = "pool_region"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 10
CONF_INDEX = "generate_index"
CONF_DOMAIN_ID = "domain_id"

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