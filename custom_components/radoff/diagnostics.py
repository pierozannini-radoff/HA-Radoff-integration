"""
Diagnostics support for the radoff integration (card S-17).

Before this card, the only support channel was pasting raw Home Assistant
logs into an issue - and the raw log contains, today, the full device
payload (finding S5), error response bodies including 401s (S6), and
domain UUIDs at INFO level (S7). Card S-03 removes those from the log, but
that leaves support with no diagnostic tool at all: this module is the
replacement, not an addition.

`async_get_config_entry_diagnostics` dumps the config entry (`entry.as_dict()`)
and the coordinator's last successful `RadoffData`, then redacts everything
that identifies the user or their account via `TO_REDACT` before returning -
`async_redact_data` walks the whole structure recursively, so a leftover
identifier nested anywhere in `entry.data`/`entry.options`/the runtime dump
is still caught, not just at the top level.

`RadoffData.devices[*].readings` is dumped field by field rather than with
`dataclasses.asdict()`: only `value` and `measured_at` are needed to
diagnose the runbook's three cases (zero entities, entity unavailable, auth
error), and the card's own instruction is explicit that this is allowed ("i
VALORI dei sensori si possono includere, non sono dati personali; gli
identificatori no").

Card M-03 follows the model through. Readings are keyed by telemetry field
name, since that is the key now (no more buckets, no more
`reading_key_slug`), and the device dump carries what arch 2.0 actually
reports - the serial as the only identity, plus the connection and firmware
fields the payload gained. `connection_status` in particular is here
*before* anything consumes it (M-06 does): a support dump that shows a
device `connected` with no telemetry, or `disconnected` with fresh
telemetry, is what tells those two situations apart.

Card M-06 adds `status` beside it, and now both are worth having for a
second reason: entity availability is decided from `connection_status`
alone, so a dump is where you check whether a device reported unavailable
really said `disconnected` - or said something this version has never seen
and was kept available on purpose (`ConnectionState.INDETERMINATE`,
`api/models.py`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from . import RadoffConfigEntry
    from .api.models import RadoffDevice, Reading

# Keys redacted anywhere they appear in the dumped structure (S-17 "COSA
# FARE"). `serial_number` is the dict key `_dump_device` below emits, chosen
# to match this set rather than a translation table, so `async_redact_data`'s
# plain key-name matching catches it.
#
# Card M-03 adds `serial_number` and keeps `device_id`/`serial`, which
# nothing emits any more: arch 2.0 has no device UUID and the model field is
# named `serial_number` now. They stay because this set is also walked over
# `entry.as_dict()`, whose contents come from whatever a config entry
# happened to be written with - including entries created before this
# migration.
#
# Card M-07 adds `domain_prefix` for the same reason `domain_id` was here:
# it identifies the customer's tenant, which is not this integration's to
# put in a file a user attaches to a public issue. `domain_id` stays next to
# it under the rule above - an entry written before the version-3 migration
# still has one on disk, and a diagnostics dump is taken from whatever is
# actually there.
#
# The third field S-17's successor list named - coordinates - has nothing to
# redact: the arch 2.0 device model carries none (see `api/models.py`), and
# `_dump_device` emits no location of any kind. Noted rather than guessed
# at, so the next reader does not go looking for the omission.
TO_REDACT = {
    "password",
    "username",
    "domain_prefix",
    "domain_id",
    "device_id",
    "serial",
    "serial_number",
    "IdToken",
    "AccessToken",
    "RefreshToken",
    "unique_id",
    "title",
}

_MANIFEST_PATH = Path(__file__).parent / "manifest.json"


def _integration_version() -> str:
    """Read this integration's version statically from manifest.json."""
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest["version"]


def _iso(value: datetime | None) -> str | None:
    """Return an ISO-8601 string for a timestamp, or None."""
    return value.isoformat() if value else None


def _dump_readings(readings: dict[str, Reading]) -> dict[str, Any]:
    """Return only the value and measured_at of each reading, keyed by field."""
    return {
        field: {
            "value": reading.value,
            "measured_at": _iso(reading.measured_at),
        }
        for field, reading in readings.items()
    }


def _dump_device(device: RadoffDevice) -> dict[str, Any]:
    """Return the diagnostic-relevant fields of one device (pre-redaction)."""
    return {
        "serial_number": device.serial_number,
        "device_type": device.device_type,
        "stale": device.stale,
        "connection_status": device.connection_status,
        "connection_status_updated_at": _iso(device.connection_status_updated_at),
        "status": device.status,
        "firmware_version": device.firmware_version,
        "telemetry_timestamp": _iso(device.telemetry_timestamp),
        "readings": _dump_readings(device.readings),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: RadoffConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry, redacted of sensitive data."""
    coordinator = entry.runtime_data
    data = coordinator.data

    diagnostics: dict[str, Any] = {
        "integration_version": _integration_version(),
        "entry": entry.as_dict(),
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
        "last_update_success": coordinator.last_update_success,
        "last_exception": (
            str(coordinator.last_exception) if coordinator.last_exception else None
        ),
        "data": None,
    }

    if data is not None:
        diagnostics["data"] = {
            "controller_name": data.controller_name,
            "generate_index": data.generate_index,
            "devices": [_dump_device(device) for device in data.devices],
        }

    return async_redact_data(diagnostics, TO_REDACT)
