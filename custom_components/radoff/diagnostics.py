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

`RadoffData.devices[*].readings` is deliberately NOT dumped with
`dataclasses.asdict()`: `Reading.normalize_fn` is a `Callable`, which is not
JSON-serializable, and `Reading.device_class`/`Reading.unit` are internal
implementation detail, not needed to diagnose the runbook's three cases
(zero entities, entity unavailable, auth error). Only `value` and
`measured_at` are pulled out per reading - the card's own instruction ("i
VALORI dei sensori si possono includere, non sono dati personali; gli
identificatori no").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .entity import reading_key_slug

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import RadoffConfigEntry
    from .api.models import RadoffDevice, Reading, ReadingKey

# Keys redacted anywhere they appear in the dumped structure (S-17 "COSA
# FARE"). `serial` (not `device_serial`) and `device_id` are the dict keys
# `_dump_device` below actually emits - chosen to match this set rather than
# the dataclass's own field names, so `async_redact_data`'s plain key-name
# matching catches them without a translation table.
TO_REDACT = {
    "password",
    "username",
    "domain_id",
    "device_id",
    "serial",
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


def _dump_readings(readings: dict[ReadingKey, Reading]) -> dict[str, Any]:
    """Return only the value and measured_at for each reading, keyed by slug."""
    return {
        reading_key_slug(key): {
            "value": reading.value,
            "measured_at": (
                reading.measured_at.isoformat() if reading.measured_at else None
            ),
        }
        for key, reading in readings.items()
    }


def _dump_device(device: RadoffDevice) -> dict[str, Any]:
    """Return the diagnostic-relevant fields of one device (pre-redaction)."""
    return {
        "device_id": device.device_id,
        "serial": device.device_serial,
        "device_type": device.device_type,
        "stale": device.stale,
        "last_data_received_at": (
            device.last_data_received_at.isoformat()
            if device.last_data_received_at
            else None
        ),
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
