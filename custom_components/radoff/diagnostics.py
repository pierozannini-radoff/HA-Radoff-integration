"""
Diagnostics support for the radoff integration.

The config entry and the coordinator's last data, with everything
identifying the user redacted. Sensor values are kept: not personal data.
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

# Redacted anywhere they appear, matched by plain key name. Keys nothing
# emits now are kept: this also walks whatever the entry was written with.
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
