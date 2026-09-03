#!/usr/bin/env python3
"""
Diagnostic (not shipped): inspect a Home Assistant log for the radoff
integration to see (a) how long ago it last successfully polled Radoff, and
(b) whether the auth-error -> re-auth transition happened once (expected)
or is looping (S-08 AC5 regression).

Usage:
    python3 check_reauth_log.py [path/to/home-assistant.log]

Defaults to .devcontainer/config/home-assistant.log (this repo's dev
harness). Requires custom_components.radoff logged at least at INFO (DEBUG
gives the most detail); the dev harness's configuration.yaml already sets
DEBUG for it.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(".devcontainer/config/home-assistant.log")
)

TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")

SUCCESS_RE = re.compile(r"Successfully fetched data for \d+ device")
AUTH_INVALID_RE = re.compile(r"Radoff credentials are no longer valid, starting re-auth")
AUTH_EXPIRED_RE = re.compile(r"Radoff authentication token expired, will retry")
GENERIC_AUTH_ERROR_RE = re.compile(r"^.*Authentication error$")
RECONNECT_RE = re.compile(r"API not connected, attempting to connect")


def parse_ts(line: str) -> datetime | None:
    match = TIMESTAMP_RE.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f")


def main() -> None:
    if not LOG_PATH.exists():
        print(f"Log file not found: {LOG_PATH}")
        sys.exit(1)

    lines = [
        line
        for line in LOG_PATH.read_text(errors="replace").splitlines()
        if "custom_components.radoff" in line
    ]
    if not lines:
        print("No radoff log lines found in this file.")
        return

    last_success = None
    events = []  # (timestamp, kind)

    for line in lines:
        ts = parse_ts(line)
        if ts is None:
            continue
        if SUCCESS_RE.search(line):
            last_success = ts
        elif AUTH_INVALID_RE.search(line):
            events.append((ts, "AUTH_INVALID (definitivo -> ConfigEntryAuthFailed)"))
        elif AUTH_EXPIRED_RE.search(line):
            events.append((ts, "AUTH_EXPIRED (recuperabile -> UpdateFailed)"))
        elif RECONNECT_RE.search(line):
            events.append((ts, "RECONNECT attempt"))
        elif GENERIC_AUTH_ERROR_RE.search(line):
            events.append((ts, "AUTH_ERROR generico (403/429/5xx)"))

    now = datetime.now()

    print("=== Ultimo poll riuscito ===")
    if last_success:
        print(f"{last_success}  ->  {now - last_success} fa")
    else:
        print("Nessun poll riuscito trovato nel log.")

    print(f"\n=== Eventi di autenticazione trovati: {len(events)} ===")
    for ts, kind in events:
        print(f"{ts}  {kind}")

    invalid_events = [ts for ts, kind in events if kind.startswith("AUTH_INVALID")]

    print(f"\nAUTH_INVALID visto {len(invalid_events)} volta/e.")
    if len(invalid_events) > 1:
        gaps = [b - a for a, b in zip(invalid_events, invalid_events[1:])]
        print("LOOP SOSPETTO: ConfigEntryAuthFailed sollevato più di una volta.")
        print("Intervalli tra le occorrenze:", ", ".join(str(g) for g in gaps))
    elif len(invalid_events) == 1:
        print("OK: nessun loop, il coordinator ha smesso di ripetere l'errore auth.")
    else:
        print("Nessun AUTH_INVALID ancora nel log (password non ancora rifiutata).")


if __name__ == "__main__":
    main()