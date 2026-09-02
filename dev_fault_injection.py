#!/usr/bin/env python3
"""
Temporary dev launcher: inject a simulated poll fault, then start Home Assistant.

Not shipped code (same convention as the probe scripts - see
`.ruff.toml`'s per-file-ignores): a drop-in replacement for
`./scripts/develop` for manually exercising card S-07's AC1-AC4 without
physically disconnecting a device or waiting real wall-clock time for a
stale sample.

HISTORY (why this looks the way it does):

v1 patched `radoff.api.client.API.get_devices` once, before starting Home
Assistant, using a fixed RADOFF_FAULT env var. It patched the wrong object
in practice (proven by a real run where entities kept reporting live
values under RADOFF_FAULT=missing_device) - see v2's fix below.

v2 (previous version of this file) fixed that by polling `sys.modules` from
a background thread for whichever module Home Assistant actually loads
matching "...radoff.api.client" - robust to whatever name the loader gives
it - and patching the `API` class found there. This part is unchanged and
correct; kept as-is.

v3 (this version) replaces the fixed, env-var-only fault with a CONTROL
FILE (`fault_control.json`, next to this script) that the patched
`get_devices()` re-reads on *every* call. This unlocks two things AC3/AC4
need that a fixed env var cannot give you:

  - Changing (or clearing) the fault while Home Assistant keeps running,
    in the same process, with no restart - which is the literal thing
    AC4 ("torna disponibile... senza reload") has to prove.
  - Targeting ONE specific device (by its real API device_id, printed
    every poll - see below) while a second device on a different account
    keeps polling normally as an unaffected control - useful now that two
    real devices are available, to verify on real hardware (not just in
    `verify_s07.py`'s synthetic stubs) that a fault on one device leaves
    the other alone.

Every poll, the patched `get_devices()` also prints the account's domain
id and the (device_id, serial, name) of every device it fetched - this is
how a driving script (or you) discovers device ids to target, without ever
needing the Home Assistant UI.

Fault control file format (`fault_control.json`, JSON object):

    {"mode": "none"}
    {"mode": "missing_device", "target_device_id": "<uuid or omit for all>"}
    {"mode": "missing_property", "property": "pm1", "target_device_id": "..."}
    {"mode": "stale_reading", "stale_seconds": 99999, "target_device_id": "..."}

`target_device_id` is optional in every mode; omitted (or null) means
"apply to every device from every account", matching the old env-var-only
behaviour. The file does not need to exist at startup - missing or
unparsable is treated as `{"mode": "none"}`. Write it with a temp-file +
`os.replace` (atomic on POSIX) so the patched code never reads a half
-written file; see `verify_ac_suite.py` for a worked example.

For simple one-off use (no dynamic changes needed), RADOFF_FAULT is still
accepted as before and is used to *seed* the control file once at startup,
applying to all devices:

    RADOFF_FAULT=missing_device python3 dev_fault_injection.py
    RADOFF_FAULT=missing_property:pm1 python3 dev_fault_injection.py
    python3 dev_fault_injection.py   # no fault, like ./scripts/develop

The fault is injected in the SAME process that then runs HA - patching in a
separate process (e.g. one that then shells out to `hass`) would have no
effect, since the patch only exists in the process that applies it. This
script calls `homeassistant.__main__.main()` directly for that reason,
exactly like the installed `hass` command does internally.

Usage: from the repo root, same working directory as ./scripts/develop; run
`./scripts/develop` at least once first so `.devcontainer/config/` exists.
Ctrl+C stops HA. Nothing to "undo" afterwards: the patch and the control
file's effect both live only in this process's memory / this throwaway
file - your checkout and your real devices are never touched.

Delete this file (and fault_control.json, verify_ac_suite.py) once S-18
lands proper automated degraded-poll tests.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
HASS_CONFIG_DIR = REPO_ROOT / ".devcontainer" / "config"
FAULT_CONTROL_FILE = REPO_ROOT / "fault_control.json"

_TARGET_MODULE_RE = re.compile(r"(^|\.)radoff\.api\.client$")
_PATCH_POLL_INTERVAL_SECONDS = 0.1
_PATCH_TIMEOUT_SECONDS = 60
_DEFAULT_STALE_SECONDS = 86400  # 24h - comfortably above any real stale_after


def _read_fault_control() -> dict[str, Any]:
    """Read the current fault, tolerating a missing or mid-write file."""
    try:
        return json.loads(FAULT_CONTROL_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"mode": "none"}


def _write_fault_control(data: dict[str, Any]) -> None:
    """Write the control file atomically (temp file + replace)."""
    tmp = FAULT_CONTROL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(FAULT_CONTROL_FILE)


def _seed_control_file_from_env(fault: str) -> None:
    """Translate the legacy RADOFF_FAULT env var into an initial control file."""
    if fault == "missing_device":
        _write_fault_control({"mode": "missing_device"})
    elif fault.startswith("missing_property:"):
        _write_fault_control(
            {"mode": "missing_property", "property": fault.split(":", 1)[1]}
        )
    else:
        print(f"[fault injection] unknown RADOFF_FAULT={fault!r}, ignoring")
        _write_fault_control({"mode": "none"})


def _matches_target(device: Any, target_device_id: str | None) -> bool:
    return target_device_id is None or device.device_id == target_device_id


_poll_tick = 0


def _apply_patch(api_cls: type) -> None:
    if getattr(api_cls.get_devices, "_radoff_fault_patched", False):
        return  # already patched

    original_get_devices = api_cls.get_devices

    def patched_get_devices(self):  # noqa: ANN001
        global _poll_tick  # noqa: PLW0603
        devices = original_get_devices(self)

        _poll_tick += 1
        inventory = [
            {"device_id": d.device_id, "serial": d.device_serial, "name": d.name}
            for d in devices
        ]
        # JSON, not repr(): verify_ac_suite.py parses this line by line to
        # discover real device ids and to know when a fresh poll happened.
        print(
            "[fault injection] poll_tick "
            + json.dumps(
                {"tick": _poll_tick, "domain": self.domain, "devices": inventory}
            )
        )

        control = _read_fault_control()
        mode = control.get("mode", "none")
        target = control.get("target_device_id")

        if mode == "none":
            return devices

        if mode == "missing_device":
            kept = [d for d in devices if not _matches_target(d, target)]
            hidden = len(devices) - len(kept)
            if hidden:
                print(
                    f"[fault injection] hiding {hidden} device(s) (target={target!r})"
                )
            return kept

        if mode == "missing_property":
            prop = control.get("property")
            for device in devices:
                if (
                    _matches_target(device, target)
                    and device.readings.pop(prop, None) is not None
                ):
                    print(
                        f"[fault injection] removed {prop!r} from device {device.device_id}"
                    )
            return devices

        if mode == "stale_reading":
            stale_seconds = control.get("stale_seconds", _DEFAULT_STALE_SECONDS)
            forced = datetime.now(UTC) - timedelta(seconds=stale_seconds)
            for device in devices:
                if _matches_target(device, target):
                    device.last_data_received_at = forced
                    print(
                        f"[fault injection] forced last_data_received_at={forced.isoformat()} "
                        f"on device {device.device_id}"
                    )
            return devices

        print(f"[fault injection] unknown mode={mode!r} in control file, ignoring")
        return devices

    patched_get_devices._radoff_fault_patched = True  # noqa: SLF001
    api_cls.get_devices = patched_get_devices


def _patch_when_loaded() -> None:
    """Poll sys.modules until Home Assistant imports the API class, then patch it once."""
    deadline = time.monotonic() + _PATCH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for name, module in list(sys.modules.items()):
            if module is not None and _TARGET_MODULE_RE.search(name):
                api_cls = getattr(module, "API", None)
                if api_cls is not None and hasattr(api_cls, "get_devices"):
                    _apply_patch(api_cls)
                    print(
                        f"[fault injection] patched module {name!r} (API.get_devices)"
                    )
                    return
        time.sleep(_PATCH_POLL_INTERVAL_SECONDS)
    print(
        f"[fault injection] TIMEOUT after {_PATCH_TIMEOUT_SECONDS}s - never found a "
        "'...radoff.api.client' module to patch. No fault machinery installed."
    )


def main() -> int:
    """Seed the control file (legacy RADOFF_FAULT support), patch, then start Home Assistant."""
    import os

    if not HASS_CONFIG_DIR.exists():
        print(
            f"Expected config dir at {HASS_CONFIG_DIR} - run ./scripts/develop once first."
        )
        return 1

    # Same PYTHONPATH trick as scripts/develop: required for Home Assistant
    # to find the "radoff" integration at all under this dev harness.
    sys.path.insert(0, str(REPO_ROOT / "custom_components"))

    fault = os.environ.get("RADOFF_FAULT")
    if fault:
        _seed_control_file_from_env(fault)
    elif not FAULT_CONTROL_FILE.exists():
        _write_fault_control({"mode": "none"})

    threading.Thread(target=_patch_when_loaded, daemon=True).start()

    sys.argv = ["hass", "--config", str(HASS_CONFIG_DIR), "--debug"]
    from homeassistant.__main__ import main as hass_main

    return hass_main()


if __name__ == "__main__":
    raise SystemExit(main())
