#!/usr/bin/env python3
"""
Automated AC1/AC2 check: run dev_fault_injection.py, capture its output,
query the running HA instance's REST API, and print a PASS/FAIL verdict -
so you don't have to watch the terminal log or the Developer Tools UI by
hand while it runs.

Not shipped code (same convention as the other dev/probe scripts - see
.ruff.toml's per-file-ignores).

CORRECTED after a real run caught two problems with the first version:

1. dev_fault_injection.py's patch wasn't reaching the class Home Assistant
   actually used (see that file's own docstring for the fix - this script
   now waits for its "patched module '...'" confirmation line in the log
   before starting the wait-for-a-poll-cycle countdown, instead of assuming
   the patch was already active the moment HA answered on the network).
2. `RADOFF_FAULT=missing_property:internal_temperature` against a device
   that doesn't expose that property produced a **false PASS**: with zero
   matched entities for that key, "no entity that should be unavailable
   isn't" and "no entity that shouldn't be unavailable is" are both
   vacuously true. This script now requires at least one matched entity for
   the requested key and reports INCONCLUSIVE (exit 2), not PASS, otherwise.

One-time setup: this instance needs a Home Assistant Long-Lived Access
Token (Profile page, bottom, "Long-lived access tokens" -> Create Token,
after logging into http://localhost:8123 at least once). Never hardcode
it - export it as an environment variable instead. NEVER paste the token
itself into a chat, ticket, or log you share with anyone - it is a live
credential; revoke and recreate it if it ever leaves your machine.

    export HASS_TOKEN=...
    export HASS_URL=http://localhost:8123   # optional, this is the default

Usage (from the repo root, same working directory as dev_fault_injection.py):

    # first, list the property keys this device actually exposes, so you
    # pick one that exists (see custom_components/radoff/properties.py, or
    # just look at the "--- entity states ---" table from a baseline run)
    python3 verify_fault_injection.py

    # AC1 - device vanishes from every poll
    RADOFF_FAULT=missing_device python3 verify_fault_injection.py

    # AC2 - one property vanishes from every poll (device stays present) -
    # use a key this device actually has, e.g. pm1 or pm10
    RADOFF_FAULT=missing_property:pm1 python3 verify_fault_injection.py

Optional: WAIT_SECONDS (default 90) - how long to wait, after the fault is
confirmed patched in, before querying /api/states; should comfortably
exceed one update_interval. PATCH_TIMEOUT_SECONDS (default 60) - how long
to wait for dev_fault_injection.py's "patched module" confirmation line.

Exit codes: 0 = PASS, 1 = FAIL (or a startup/network problem), 2 =
INCONCLUSIVE (the scenario was never actually exercised, e.g. the
requested property doesn't exist on this device - fix the input and rerun,
this is not a verdict on the code).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent
LOG_FILE = REPO_ROOT / "verify_fault_injection.log"

# Mirrors custom_components/radoff/properties.py::MAPPING's property keys.
# Kept as a literal list here (not imported) so this script has no
# dependency on the integration's own package layout - see the module
# docstring for why these throwaway scripts stay outside custom_components/.
PROPERTY_KEYS = [
    "airqualityindex",
    "eco2",
    "internal_temperature",
    "pm1",
    "pm10",
    "pm25",
    "pressure",
    "relative_humidity",
    "tvoc",
]

DEFAULT_WAIT_SECONDS = 90
STARTUP_TIMEOUT_SECONDS = 90
DEFAULT_PATCH_TIMEOUT_SECONDS = 60


def _entity_property_key(entity_id: str) -> str | None:
    """Return which known property key this entity_id refers to, if any."""
    for key in PROPERTY_KEYS:
        # Word-boundary match on "_" so "pm1" doesn't also match "pm10".
        if re.search(rf"(^|_){re.escape(key)}($|_)", entity_id):
            return key
    return None


def _wait_for_hass(base_url: str) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/", timeout=3)
            if resp.status_code in (200, 401):  # 401 = up, just unauthenticated
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    return False


def _wait_for_patch_confirmation(fault: str, timeout: int) -> str | None:
    """
    Tail LOG_FILE until dev_fault_injection.py confirms its patch landed.

    Returns None on success, or an error message to report as FAIL. Skipped
    entirely (returns None immediately) when no fault was requested - there
    is nothing to confirm for a plain baseline run.
    """
    if not fault:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else ""
        if "[fault injection] TIMEOUT" in text:
            return (
                "dev_fault_injection.py never found a module to patch - the "
                "integration likely never loaded 'radoff.api.client'. See the "
                "log below."
            )
        if "[fault injection] patched module" in text:
            return None
        time.sleep(0.5)
    return (
        f"Never saw a 'patched module' confirmation within {timeout}s - "
        "the fault may not actually be active. See the log below."
    )


def main() -> int:  # noqa: PLR0911
    """Run one fault-injection scenario end-to-end and report PASS/FAIL/INCONCLUSIVE."""
    token = os.environ.get("HASS_TOKEN")
    if not token:
        print("HASS_TOKEN not set - see this script's docstring.", file=sys.stderr)
        return 1

    base_url = os.environ.get("HASS_URL", "http://localhost:8123").rstrip("/")
    wait_seconds = int(os.environ.get("WAIT_SECONDS", str(DEFAULT_WAIT_SECONDS)))
    patch_timeout = int(
        os.environ.get("PATCH_TIMEOUT_SECONDS", str(DEFAULT_PATCH_TIMEOUT_SECONDS))
    )
    fault = os.environ.get("RADOFF_FAULT", "")

    print(f"Starting dev_fault_injection.py (RADOFF_FAULT={fault!r})...")
    with LOG_FILE.open("w", encoding="utf-8") as log:
        child_env = os.environ.copy()
        # Force the child's stdout to be unbuffered: Python fully block-
        # buffers stdout when it isn't a TTY (i.e. once redirected to this
        # log file), so without this its "patched module" print can sit in
        # an internal buffer - invisible to our live polling below - until
        # the process exits and flushes. That's exactly what produced a
        # false "Never saw a 'patched module' confirmation" FAIL against a
        # real run where the patch had, in fact, already succeeded (visible
        # only in the final dump, after SIGINT forced the flush).
        child_env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", str(REPO_ROOT / "dev_fault_injection.py")],
            cwd=REPO_ROOT,
            env=child_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

        patch_error = None
        try:
            print("Waiting for Home Assistant to come up...")
            if not _wait_for_hass(base_url):
                print("Home Assistant never answered - see the log below.")
                return 1

            if fault:
                print("Up. Waiting for the fault-injection patch to be confirmed...")
                patch_error = _wait_for_patch_confirmation(fault, patch_timeout)

            if patch_error is None:
                print(f"Waiting {wait_seconds}s to cover at least one poll cycle...")
                time.sleep(wait_seconds)

                resp = requests.get(
                    f"{base_url}/api/states",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10,
                )
                resp.raise_for_status()
                states = resp.json()
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    print("\n--- [fault injection] log lines ---")
    injection_lines = [
        line
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines()
        if "[fault injection]" in line
    ]
    print("\n".join(injection_lines) or "(none found - see verify_fault_injection.log)")

    if patch_error is not None:
        print(f"\nFAIL: {patch_error}")
        return 1

    matched = [
        (s["entity_id"], s["state"], _entity_property_key(s["entity_id"]))
        for s in states
        if s["entity_id"].startswith("sensor.") and _entity_property_key(s["entity_id"])
    ]
    if not matched:
        print(
            "\nNo sensor entities matched a known Radoff property key - "
            "is the integration actually configured in this HA instance?"
        )
        return 1

    print("\n--- entity states ---")
    for entity_id, state, key in sorted(matched):
        print(f"  {entity_id:55s} key={key:22s} state={state}")

    print("\n--- verdict ---")
    if fault == "missing_device":
        bad = [(e, s) for e, s, _ in matched if s != "unavailable"]
        if bad:
            print(f"FAIL: {len(bad)} entity/ies still not unavailable: {bad}")
            return 1
        print(f"PASS (AC1): all {len(matched)} matched entities are unavailable.")
        return 0

    if fault.startswith("missing_property:"):
        missing_key = fault.split(":", 1)[1]
        affected = [(e, s) for e, s, k in matched if k == missing_key]
        if not affected:
            print(
                f"INCONCLUSIVE: no entity on this device matches key={missing_key!r} - "
                "this device may not expose that property. Check the table above for "
                "keys that DO appear, and rerun with one of those."
            )
            return 2
        should_be_unavailable = [(e, s) for e, s in affected if s != "unavailable"]
        should_stay_available = [
            (e, s) for e, s, k in matched if k != missing_key and s == "unavailable"
        ]
        if should_be_unavailable or should_stay_available:
            print(
                f"FAIL: expected only key={missing_key!r} unavailable. "
                f"Not unavailable but should be: {should_be_unavailable}. "
                f"Unavailable but should not be: {should_stay_available}."
            )
            return 1
        print(
            f"PASS (AC2): only key={missing_key!r} is unavailable, "
            f"the other {len(matched) - len(affected)} matched entities are not."
        )
        return 0

    # No fault requested: baseline sanity check.
    bad = [(e, s) for e, s, _ in matched if s == "unavailable"]
    if bad:
        print(f"FAIL (baseline): unexpected unavailable entities: {bad}")
        return 1
    print(
        f"PASS (baseline): none of the {len(matched)} matched entities is unavailable."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
