#!/usr/bin/env python3
"""
Full AC1-AC4 verification suite (card S-07) against two real devices on two
real accounts, in ONE continuous Home Assistant run - fully scripted, no UI.

Not shipped code (same convention as the other dev/probe scripts - see
.ruff.toml's per-file-ignores).

Why one continuous run: AC4 explicitly requires recovery "senza reload".
The older verify_fault_injection.py started a fresh `dev_fault_injection.py`
process per scenario, which can prove AC1/AC2 but can never exercise AC4's
"no reload" half. This script starts Home Assistant exactly once, then
drives `dev_fault_injection.py`'s dynamic fault_control.json (see that
file's docstring) to move through every scenario - including clearing the
fault and confirming recovery - while HA keeps running throughout.

Why two devices: with two real devices (two accounts) already configured,
every fault below targets ONE device only (by its real device_id, learned
automatically from `dev_fault_injection.py`'s per-poll log line - no need
to look anything up by hand). The second device is the live control: it
must stay fully unaffected at every step, which is the "a different
device's entity is unaffected" half of AC1 that only synthetic tests had
covered until now.

One-time setup: same Home Assistant Long-Lived Access Token as
verify_fault_injection.py (Profile page -> Long-lived access tokens).
Never hardcode it or paste it anywhere shared - export it as an env var.

    export HASS_TOKEN=...
    export HASS_URL=http://localhost:8123   # optional, this is the default

Usage (from the repo root, after ./scripts/develop has been run at least
once so .devcontainer/config/ exists):

    python3 verify_ac_suite.py

No RADOFF_FAULT env var needed or used - this script drives the fault
control file directly and moves through every scenario by itself.

Exit codes: 0 = every scenario PASSED, 1 = at least one FAILED or a
startup/network problem, 2 = INCONCLUSIVE (e.g. fewer than two devices
were discovered - this script needs two to exercise the "other device is
unaffected" checks; use verify_fault_injection.py for a single device).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent
LOG_FILE = REPO_ROOT / "verify_ac_suite.log"
FAULT_CONTROL_FILE = REPO_ROOT / "fault_control.json"

STARTUP_TIMEOUT_SECONDS = 90
DISCOVERY_TIMEOUT_SECONDS = 60
# NOT the real per-step wait: DEFAULT_SCAN_INTERVAL (const.py) is 60s, but a
# fault set right after a poll just fired can leave the *next* one nearly a
# full interval away, plus network jitter (Cognito auth + HTTP) - a fixed
# 60s timeout here previously caused every step past baseline to time out
# for exactly this reason. See the calibration step in main(): the real
# per-step timeout is measured from the account's own observed poll cadence
# instead of assumed.
CALIBRATION_TIMEOUT_SECONDS = 400
FALLBACK_TICK_TIMEOUT_SECONDS = 400
POST_TICK_SETTLE_SECONDS = 3
STALE_SECONDS = 86400

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

_POLL_TICK_RE = re.compile(r"\[fault injection\] poll_tick (\{.*\})")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _entity_property_key(entity_id: str) -> str | None:
    for key in PROPERTY_KEYS:
        if re.search(rf"(^|_){re.escape(key)}($|_)", entity_id):
            return key
    return None


def _set_fault(mode: str, **kwargs) -> None:
    data = {"mode": mode, **kwargs}
    tmp = FAULT_CONTROL_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(FAULT_CONTROL_FILE)


def _read_log() -> str:
    return LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else ""


def _parse_ticks(text: str) -> list[dict]:
    ticks = []
    for match in _POLL_TICK_RE.finditer(text):
        try:
            ticks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return ticks


def _wait_for_hass(base_url: str) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{base_url}/api/", timeout=3)
            if resp.status_code in (200, 401):
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    return False


def _discover_devices() -> dict[str, dict] | None:
    """Poll the log until at least two distinct devices have reported in."""
    deadline = time.monotonic() + DISCOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        devices: dict[str, dict] = {}
        for tick in _parse_ticks(_read_log()):
            for dev in tick["devices"]:
                devices[dev["device_id"]] = {**dev, "domain": tick["domain"]}
        if len(devices) >= 2:  # noqa: PLR2004
            return devices
        time.sleep(1)
    return None


def _wait_for_fresh_tick(
    domain: str, after_tick: int, timeout: int = FALLBACK_TICK_TIMEOUT_SECONDS
) -> bool:
    """Wait for a poll_tick for `domain` numbered strictly after `after_tick`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tick in _parse_ticks(_read_log()):
            if tick["domain"] == domain and tick["tick"] > after_tick:
                return True
        time.sleep(1)
    return False


def _latest_tick(domain: str) -> int:
    ticks = [t["tick"] for t in _parse_ticks(_read_log()) if t["domain"] == domain]
    return max(ticks) if ticks else 0


def _get_states(base_url: str, token: str) -> list[dict]:
    resp = requests.get(
        f"{base_url}/api/states",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _entities_for(states: list[dict], prefix: str) -> list[tuple[str, str]]:
    return [
        (s["entity_id"], s["state"])
        for s in states
        if s["entity_id"].startswith(f"sensor.{prefix}")
    ]


class Scenario:
    """One named check; PASS/FAIL is recorded and printed at the end."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.ok: bool | None = None
        self.detail = ""

    def check(self, condition: bool, detail: str) -> None:  # noqa: FBT001
        self.ok = condition
        self.detail = detail
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {self.name}: {detail}")


def main() -> int:  # noqa: PLR0915
    """Run every AC1-AC4 scenario against two real devices, in one HA run."""
    token = os.environ.get("HASS_TOKEN")
    if not token:
        print("HASS_TOKEN not set - see this script's docstring.", file=sys.stderr)
        return 1
    base_url = os.environ.get("HASS_URL", "http://localhost:8123").rstrip("/")

    FAULT_CONTROL_FILE.unlink(missing_ok=True)

    print("Starting dev_fault_injection.py (clean, no initial fault)...")
    child_env = os.environ.copy()
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env.pop("RADOFF_FAULT", None)
    with LOG_FILE.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-u", str(REPO_ROOT / "dev_fault_injection.py")],
            cwd=REPO_ROOT,
            env=child_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

        scenarios: list[Scenario] = []
        try:
            print("Waiting for Home Assistant to come up...")
            if not _wait_for_hass(base_url):
                print("Home Assistant never answered - see the log below.")
                return 1

            print("Waiting to discover at least two devices...")
            devices = _discover_devices()
            if not devices:
                print(
                    "INCONCLUSIVE: fewer than two devices seen within "
                    f"{DISCOVERY_TIMEOUT_SECONDS}s - this suite needs two. "
                    "Use verify_fault_injection.py for a single device."
                )
                return 2

            (dev_a_id, dev_a), (dev_b_id, dev_b) = list(devices.items())[:2]
            prefix_a, prefix_b = _slugify(dev_a["name"]), _slugify(dev_b["name"])
            print(
                f"Device A: {dev_a['name']!r} ({dev_a_id}), entity prefix {prefix_a!r}"
            )
            print(
                f"Device B: {dev_b['name']!r} ({dev_b_id}), entity prefix {prefix_b!r}"
            )

            states = _get_states(base_url, token)
            ents_a = _entities_for(states, prefix_a)
            ents_b = _entities_for(states, prefix_b)
            if not ents_a or not ents_b:
                print(
                    "INCONCLUSIVE: couldn't match sensor entities to both devices by name."
                )
                return 2

            baseline = Scenario("baseline sanity")
            baseline.check(
                all(s != "unavailable" for _, s in ents_a + ents_b),
                f"A={ents_a} B={ents_b}",
            )
            scenarios.append(baseline)

            a_key = sorted({_entity_property_key(e) for e, _ in ents_a} - {None})[0]

            print("Calibrating device A's real poll cadence (no fault yet)...")
            first_tick_a = _latest_tick(dev_a["domain"])
            calib_start = time.monotonic()
            if _wait_for_fresh_tick(
                dev_a["domain"], first_tick_a, timeout=CALIBRATION_TIMEOUT_SECONDS
            ):
                measured_interval = time.monotonic() - calib_start
                tick_wait_timeout = max(60, round(measured_interval * 2.5))
                print(
                    f"Measured poll interval ~= {measured_interval:.0f}s; "
                    f"using {tick_wait_timeout}s as the per-step wait below."
                )
            else:
                tick_wait_timeout = FALLBACK_TICK_TIMEOUT_SECONDS
                print(
                    f"Could not observe a second poll within {CALIBRATION_TIMEOUT_SECONDS}s "
                    f"- falling back to a {tick_wait_timeout}s per-step wait."
                )

            def run_step(label: str, fault_kwargs: dict, expect) -> None:  # noqa: ANN001
                before = _latest_tick(dev_a["domain"])
                _set_fault(**fault_kwargs)
                if not _wait_for_fresh_tick(
                    dev_a["domain"], before, timeout=tick_wait_timeout
                ):
                    sc = Scenario(label)
                    sc.check(
                        False, "never observed a fresh poll for device A's account"
                    )
                    scenarios.append(sc)
                    return
                time.sleep(POST_TICK_SETTLE_SECONDS)
                states_now = _get_states(base_url, token)
                a_now = _entities_for(states_now, prefix_a)
                b_now = _entities_for(states_now, prefix_b)
                sc = Scenario(label)
                expect(sc, a_now, b_now)
                scenarios.append(sc)

            def expect_ac1(sc, a_now, b_now):  # noqa: ANN001
                sc.check(
                    all(s == "unavailable" for _, s in a_now)
                    and all(s != "unavailable" for _, s in b_now),
                    f"A(should be all unavailable)={a_now} B(should be unaffected)={b_now}",
                )

            def expect_recovered(sc, a_now, b_now):  # noqa: ANN001
                sc.check(
                    all(s != "unavailable" for _, s in a_now)
                    and all(s != "unavailable" for _, s in b_now),
                    f"A(should have recovered)={a_now} B(should still be fine)={b_now}",
                )

            def expect_ac2(sc, a_now, b_now):  # noqa: ANN001
                a_target = [
                    (e, s) for e, s in a_now if _entity_property_key(e) == a_key
                ]
                a_other = [(e, s) for e, s in a_now if _entity_property_key(e) != a_key]
                sc.check(
                    a_target
                    and all(s == "unavailable" for _, s in a_target)
                    and all(s != "unavailable" for _, s in a_other)
                    and all(s != "unavailable" for _, s in b_now),
                    f"A[{a_key}](should be unavailable)={a_target} "
                    f"A[other](should be fine)={a_other} B(should be unaffected)={b_now}",
                )

            run_step(
                "AC1: device A vanishes, device B unaffected",
                {"mode": "missing_device", "target_device_id": dev_a_id},
                expect_ac1,
            )
            run_step(
                "AC4: device A recovers after missing_device is cleared, no reload",
                {"mode": "none"},
                expect_recovered,
            )
            run_step(
                f"AC2: device A's {a_key!r} missing, siblings + device B unaffected",
                {
                    "mode": "missing_property",
                    "property": a_key,
                    "target_device_id": dev_a_id,
                },
                expect_ac2,
            )
            run_step(
                "AC4: device A recovers after missing_property is cleared, no reload",
                {"mode": "none"},
                expect_recovered,
            )
            run_step(
                "AC3: device A's last_data_received_at forced stale, device B unaffected",
                {
                    "mode": "stale_reading",
                    "stale_seconds": STALE_SECONDS,
                    "target_device_id": dev_a_id,
                },
                expect_ac1,  # same shape: all of A unavailable, all of B fine
            )
            run_step(
                "AC4: device A recovers after stale_reading is cleared, no reload",
                {"mode": "none"},
                expect_recovered,
            )
        finally:
            _set_fault("none")
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            FAULT_CONTROL_FILE.unlink(missing_ok=True)

    print("\n--- summary ---")
    for sc in scenarios:
        print(f"  [{'PASS' if sc.ok else 'FAIL'}] {sc.name}")
    return 0 if all(sc.ok for sc in scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
