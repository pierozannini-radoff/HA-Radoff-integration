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
from homeassistant.const import CONF_USERNAME

from .const import CONF_DOMAIN_PREFIX, PERSONAL, SECRETS, TENANT
from .redact import scrub

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from . import RadoffConfigEntry
    from .api.models import RadoffDevice, Reading
    from .coordinator import RadoffData

# Redacted anywhere they appear, matched by plain key name: the three layers
# differ in what the log may write, not in what the dump holds back. Keys
# nothing emits now are kept, since this also walks whatever the entry was
# written with.
TO_REDACT = SECRETS | PERSONAL | TENANT

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


def _known_values(
    entry: RadoffConfigEntry, data: RadoffData | None
) -> list[str | None]:
    """Return the identifying values of one entry, for the scrubber to hide."""
    values: list[str | None] = [
        entry.data.get(CONF_DOMAIN_PREFIX),
        entry.data.get(CONF_USERNAME),
        entry.unique_id,
    ]
    if data is not None:
        values += [device.serial_number for device in data.devices]
    return values


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
        # Scrubbed rather than redacted: the key-name redaction below cannot
        # look inside a string, and an exception message can carry the values
        # it was raised about.
        "last_exception": (
            scrub(str(coordinator.last_exception), *_known_values(entry, data))
            if coordinator.last_exception
            else None
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
