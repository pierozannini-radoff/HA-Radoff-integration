#!/usr/bin/env python3
"""
Manual probe for whether a real Radoff payload carries a per-sample timestamp.

Not shipped code - same convention as the former `probe_domain_discovery.py`
used for card S-01 (see `.ruff.toml`'s per-file-ignores): a script meant to
be run by hand, once, against your own real Radoff account, outside
`custom_components/`. It answers card S-07's step 1 / task T-02: "verificare
sul payload reale il campo del timestamp e il suo fuso".

It authenticates the same way the integration does, fetches the RAW
`/data/devices/{id}` response for one Now+ device (bypassing the
integration's own `_get_data`, which today discards every field except
`propertyName` and `value`/`aggregationValue`), and prints:

  - every top-level scalar field of the response's `data` object (e.g.
    `lastDataReceivedAt`), not just the list-type buckets - the first
    version of this script only looked inside `data`/`aggregatedData`/
    `recalculatedData` and silently skipped scalar siblings, which is why
    it showed `lastDataReceivedAt`'s *name* but never its actual value;
  - every key seen on the objects in each list-type bucket (`data`,
    `aggregatedData`, `recalculatedData`);
  - for any key (top-level or per-object) whose name looks date/time-related,
    or whose value looks like an ISO-8601 string or a plausible Unix epoch
    number, the raw value, so you can tell which one is the actual per-sample
    measurement time (as opposed to some unrelated record/audit timestamp)
    and in what format and timezone it is expressed.

Nothing beyond what the API already sends back to your own account leaves
this process, and no credential is ever hardcoded: it reads them from the
environment, or prompts interactively.

Usage:
    export RADOFF_USERNAME=you@example.com
    export RADOFF_PASSWORD=...            # omit to be prompted (hidden input)
    export RADOFF_DEVICE_ID=...           # optional: probe this device id;
                                           # otherwise the first Now+ device
                                           # found via /data/devices/search
    PYTHONPATH=custom_components python3 probe_reading_timestamp.py

Report back (for card S-07 / T-02): the field name that IS the per-sample
measurement time, its format (ISO-8601 string? epoch seconds or
milliseconds?), and whether it is UTC or local time - the easiest way to
tell is to compare it against a value you know to be recent.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from datetime import datetime

from radoff.api.client import API, DEVICE_TYPES

_DATE_KEY_HINT = re.compile(r"(time|date|_at$|^at|stamp)", re.IGNORECASE)

# Plausible Unix epoch range (seconds or milliseconds), roughly
# 2001-01-01 .. 2100-01-01.
_EPOCH_LOWER_BOUND = 1_000_000_000
_EPOCH_UPPER_BOUND = 5_000_000_000_000


def _looks_like_timestamp(value: object) -> bool:
    """Heuristic only, used to decide what to print - never to parse for real."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return _EPOCH_LOWER_BOUND < abs(value) < _EPOCH_UPPER_BOUND
    if isinstance(value, str):
        candidate = value.replace("Z", "+00:00")
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            return bool(re.match(r"^\d{4}-\d{2}-\d{2}", value))
        return True
    return False


def main() -> int:
    """Authenticate, fetch one device's raw payload, and print timestamp candidates."""
    username = os.environ.get("RADOFF_USERNAME") or input("Radoff username: ")
    password = os.environ.get("RADOFF_PASSWORD") or getpass.getpass("Radoff password: ")

    api = API(username=username, password=password)
    api.connect()
    if not api.connected:
        print("Authentication did not complete (challenge/MFA?).", file=sys.stderr)
        return 1

    domains = api.list_domains()
    if not domains:
        print("No domains available for this account.", file=sys.stderr)
        return 1
    api.domain = domains[0]["id"]

    device_id = os.environ.get("RADOFF_DEVICE_ID")
    if not device_id:
        search_url = f"{api.BASE_DOMAIN}/data/devices/search"
        resp = api.session.post(
            search_url,
            headers=api._get_headers(  # noqa: SLF001 - probe script, see module docstring
                bearer_token=api._get_bearer_token(),  # noqa: SLF001
                x_domain=api.domain,
            ),
            json={"filter": {}, "take": 99},
            timeout=api.DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        devices = resp.json().get("devices", [])
        candidates = [d for d in devices if d.get("deviceTypeName") in DEVICE_TYPES]
        if not candidates:
            print("No Now+ device found on this account.", file=sys.stderr)
            return 1
        device_id = candidates[0]["id"]
        print(f"Using first Now+ device found: {candidates[0].get('name')!r}")

    url = f"{api.BASE_DOMAIN}/data/devices/{device_id}"
    resp = api.session.get(
        url,
        headers=api._get_headers(  # noqa: SLF001
            bearer_token=api._get_bearer_token(),  # noqa: SLF001
            x_domain=api.domain,
        ),
        timeout=api.DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json().get("data", {})

    if not payload:
        print("Empty 'data' object in the response - nothing to inspect.")
        return 0

    print(f"\n=== top-level keys of 'data': {sorted(payload.keys())} ===")

    scalar_candidates_found = False
    for key, value in payload.items():
        if isinstance(value, list):
            continue  # handled below, per-bucket
        if _DATE_KEY_HINT.search(key) or _looks_like_timestamp(value):
            scalar_candidates_found = True
            print(f"  candidate top-level timestamp field: {key!r} = {value!r}")
    if not scalar_candidates_found:
        print("  no top-level scalar field looks date/time-like.")

    for bucket, objs in payload.items():
        if not isinstance(objs, list):
            continue  # scalar field, already handled above

        if not objs:
            print(f"\n=== bucket: {bucket!r} — empty list, skipping ===")
            continue

        print(f"\n=== bucket: {bucket!r} — {len(objs)} object(s) ===")
        seen_keys: set[str] = set()
        for obj in objs:
            if isinstance(obj, dict):
                seen_keys.update(obj.keys())
        print(f"keys present across all objects: {sorted(seen_keys)}")

        sample = next((o for o in objs if isinstance(o, dict)), {})
        candidates_found = False
        for key, value in sample.items():
            if _DATE_KEY_HINT.search(key) or _looks_like_timestamp(value):
                candidates_found = True
                print(
                    f"  candidate timestamp field: {key!r} = {value!r} "
                    f"(sample object propertyName={sample.get('propertyName')!r})"
                )
        if not candidates_found:
            print("  no field on this bucket's sample object looks date/time-like.")

    print(
        "\nReport back for T-02 / card S-07: which field above (if any) is the "
        "actual per-sample measurement time, its format, and its timezone."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
