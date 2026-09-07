#!/usr/bin/env python3
"""
Live AC verification launcher for card S-13, against a real Home Assistant
dev harness and (at least) two real Now+ devices - not the offline,
stubbed-homeassistant checks `verify_s13.py` already runs.

Same trick as S-07's `dev_fault_injection.py` (see `s-07-implementazione.md`):
this script monkeypatches `custom_components.radoff.api.client.API` in the
SAME process that then starts Home Assistant, by calling
`homeassistant.__main__.main()` directly instead of the `hass` command - a
patch applied in a separate process that then shells out to `hass` would
have no effect, since the patch only lives in the process that applies it.

IMPORTANT: this integration is imported by Home Assistant as
`custom_components.radoff...`, NOT as a bare `radoff...` - confirmed
empirically from a real dev-harness run's own log line, which names the
logger `custom_components.radoff.coordinator` (every module here builds its
logger from `__name__`, so the logger name IS the actual import path). An
earlier version of this script added `<repo>/custom_components` to
`sys.path` and imported `radoff.api.client` (bare), which - despite
`scripts/develop` exporting a `PYTHONPATH` that looks like it should make
that the right form - patches a *different* module object than the one
Home Assistant actually runs: Python caches modules by their full dotted
name, so `radoff.api.client.API` and `custom_components.radoff.api.client.API`
are two unrelated classes even though they come from the same file on disk,
and only the second one is what `coordinator.py`'s own `from .api import
API` resolves to once Home Assistant has imported the package under that
name. That version's patch silently did nothing - `get_devices()` kept
running unpatched, which is why every poll logged "0 stale" regardless of
`s13_fault_control.json`. This version imports and patches
`custom_components.radoff.api.client.API` directly, with the *repository
root* (not `<repo>/custom_components`) on `sys.path`, so that
`custom_components` resolves as the same namespace package Home Assistant
itself will import from.

Usage - run instead of `./scripts/develop`, with the exact same arguments:

    python3 verify_s13_live.py --config "$PWD/.devcontainer/config" --debug

The fault to inject is controlled by `s13_fault_control.json` (repo root,
git-ignored, not shipped), read FRESH on every poll cycle - not once at
process start - specifically so you can flip it while Home Assistant keeps
running, with no reload and no restart (needed for AC4: "Il dispositivo in
errore torna disponibile al primo poll riuscito, senza reload"). Missing or
unreadable file = no fault injected, i.e. a completely normal poll.

    {"mode": "none"}
    {"mode": "device_500", "target_device_id": "<a real device id>"}
    {"mode": "device_auth_expired", "target_device_id": "<a real device id>"}
    {"mode": "search_500"}
    {"mode": "slow", "slow_seconds": 90}

`target_device_id` is the Radoff device `id` (not the serial) as returned
by the `search` endpoint - the same value `_get_data`'s own DEBUG log line
("device %s: %d readings, keys %s") already prints for every device on a
normal poll, so run once with `{"mode": "none"}` (or no control file at
all) first if you don't have it noted down.

`device_auth_expired` (added after the first live round, 2026-09-07) is
NOT the same check as `device_500`: `device_500` raises `APIAuthError`,
which is in `client.py`'s `_ISOLATABLE_DEVICE_ERRORS` tuple, so
`get_devices()` isolates it - only that one device goes stale, the cycle
otherwise completes normally (card S-13's main AC). `device_auth_expired`
instead raises `AuthExpiredError` - deliberately NOT in that tuple (see
`_ISOLATABLE_DEVICE_ERRORS`'s own comment in `api/client.py`) - so it
propagates out of `get_devices()` uncaught and aborts the *whole* cycle via
`coordinator.py`'s `except AuthExpiredError` clause (card S-13, "COSA FARE"
step 6: "un errore auth su un device NON va isolato"). With a single device
per account both modes end up making that one device `unavailable`, so the
entity state alone cannot tell them apart live - the distinguishing signal
is in the log:

  - `device_500`: `"Device <id> (<type>) marked stale after a per-device
    fetch error: ..."` (DEBUG, from `_merge_device_errors`) followed by the
    normal `"Radoff poll finished: %d device(s) updated, %d stale"` line -
    the cycle completed, one device just came back stale.
  - `device_auth_expired`: `"Radoff authentication token expired, will
    retry: ..."` (DEBUG, from `coordinator.py`) and NO "poll finished" line
    at all for that cycle - the whole cycle was aborted before
    `_merge_device_errors` ever ran.

`verify_s13.py` (offline, `test_auth_error_on_one_device_is_not_isolated`)
already proves this at the code level; this mode is what lets the same
distinction be observed against a real HA instance and real log output.

Not shipped code - same convention as `dev_fault_injection.py`,
`verify_s13.py` and the other root-level dev/verify scripts (see
`.ruff.toml`'s per-file-ignores).
"""

import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
CONTROL_FILE = REPO_ROOT / "s13_fault_control.json"

# `custom_components` must resolve as the same namespace package Home
# Assistant's own component loader will import `radoff` from - i.e. rooted
# at the *repository root*, not at `<repo>/custom_components` itself (see
# the module docstring's "IMPORTANT" paragraph for how the earlier, wrong
# version of this script got this backwards).
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.api.auth import AuthExpiredError  # noqa: E402
from custom_components.radoff.api.client import API  # noqa: E402
from custom_components.radoff.api.exceptions import APIAuthError  # noqa: E402

_LOGGER = logging.getLogger("verify_s13_live")

_DEFAULT_CONTROL = {"mode": "none", "target_device_id": "", "slow_seconds": 0}

_ORIGINAL_GET_DATA = API._get_data  # noqa: SLF001
_ORIGINAL_GET_DEVICES = API.get_devices


def _read_control() -> dict:
    """Read the fault-control file fresh on every call - see module docstring."""
    if not CONTROL_FILE.exists():
        return dict(_DEFAULT_CONTROL)
    try:
        raw = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        _LOGGER.warning(
            "verify_s13_live: unreadable %s (%s), treating as 'none'", CONTROL_FILE, err
        )
        return dict(_DEFAULT_CONTROL)
    return {**_DEFAULT_CONTROL, **raw}


def _patched_get_data(self, device_id: str):  # noqa: ANN001
    control = _read_control()
    if control["mode"] == "device_500" and device_id == control["target_device_id"]:
        _LOGGER.warning(
            "verify_s13_live: forcing a simulated 500 for device %s (device_500 mode)",
            device_id,
        )
        msg = f"Simulated 500 for device {device_id} (verify_s13_live, device_500 mode)"
        raise APIAuthError(msg)
    if (
        control["mode"] == "device_auth_expired"
        and device_id == control["target_device_id"]
    ):
        _LOGGER.warning(
            "verify_s13_live: forcing a simulated session-level auth error "
            "(AuthExpiredError, NOT isolatable) for device %s "
            "(device_auth_expired mode)",
            device_id,
        )
        msg = (
            f"Simulated session-level auth error for device {device_id} "
            "(verify_s13_live, device_auth_expired mode)"
        )
        raise AuthExpiredError(msg)
    return _ORIGINAL_GET_DATA(self, device_id)


def _patched_get_devices(self):  # noqa: ANN001
    control = _read_control()
    if control["mode"] == "search_500":
        _LOGGER.warning(
            "verify_s13_live: forcing a simulated search failure (search_500 mode)"
        )
        msg = "Simulated search failure (verify_s13_live, search_500 mode)"
        raise APIAuthError(msg)
    if control["mode"] == "slow":
        slow_seconds = float(control["slow_seconds"] or 0)
        _LOGGER.warning(
            "verify_s13_live: sleeping %.1fs before the real get_devices() (slow mode)",
            slow_seconds,
        )
        time.sleep(slow_seconds)
    return _ORIGINAL_GET_DEVICES(self)


API._get_data = _patched_get_data  # noqa: SLF001
API.get_devices = _patched_get_devices

_LOGGER.warning(
    "verify_s13_live active - reading fault mode from %s on every poll", CONTROL_FILE
)

from homeassistant.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())