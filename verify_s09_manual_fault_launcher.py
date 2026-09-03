#!/usr/bin/env python3
"""
One-off, throwaway harness for manually verifying card S-09's AC1 and AC3
without needing a real Cognito account stuck in a challenge state, or a way
to reliably break your own network mid-test.

Monkeypatches pycognito's `AWSSRP.authenticate_user` so EVERY Cognito login
attempt made through the Home Assistant instance this script starts returns
a fixed, simulated outcome, then starts Home Assistant exactly like
`scripts/develop` does (same config dir, same PYTHONPATH). Whatever
username/password you type into the config flow while this is running is
never actually sent to Cognito: the network call itself is replaced by the
patch below.

NOT shipped code, NOT meant to be committed - same convention as
`dev_fault_injection.py` (S-07). Delete it (or just leave it untracked)
once you're done with S-09's manual verification.

Usage (run from the repo root, same venv you already use for scripts/develop):

    python3 verify_s09_manual_fault_launcher.py challenge   # AC1: NEW_PASSWORD_REQUIRED
    python3 verify_s09_manual_fault_launcher.py mfa          # AC1 variant: SMS_MFA
    python3 verify_s09_manual_fault_launcher.py network      # AC3: Cognito unreachable

Then open http://localhost:8123, add the Radoff integration, and submit
ANY username/password (they are never sent anywhere - see above). Stop with
Ctrl+C when done; nothing about this patch persists after the process exits.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).parent
HASS_CONFIG_DIR = REPO_ROOT / ".devcontainer" / "config"

_SCENARIOS = {
    "challenge": {
        "description": "Cognito NEW_PASSWORD_REQUIRED challenge (AC1)",
        "kind": "return_value",
        "value": {"ChallengeName": "NEW_PASSWORD_REQUIRED", "ChallengeParameters": {}},
    },
    "mfa": {
        "description": "Cognito SMS_MFA challenge (AC1 variant)",
        "kind": "return_value",
        "value": {"ChallengeName": "SMS_MFA", "ChallengeParameters": {}},
    },
    "network": {
        "description": "Cognito endpoint unreachable (AC3)",
        "kind": "side_effect",
        "value": None,  # filled in after import, see main()
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=sorted(_SCENARIOS))
    args = parser.parse_args()
    scenario = _SCENARIOS[args.scenario]

    if not HASS_CONFIG_DIR.is_dir():
        HASS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["hass", "--config", str(HASS_CONFIG_DIR), "--script", "ensure_config"],
            check=True,
        )

    sys.path.insert(0, str(REPO_ROOT / "custom_components"))

    from pycognito.aws_srp import AWSSRP

    if args.scenario == "network":
        from botocore.exceptions import EndpointConnectionError

        scenario["value"] = EndpointConnectionError(
            endpoint_url="https://cognito-idp.eu-west-1.amazonaws.com/"
        )

    patch_kwargs = {scenario["kind"]: scenario["value"]}
    mock.patch.object(AWSSRP, "authenticate_user", **patch_kwargs).start()

    print(
        f"\n*** verify_s09_manual_fault_launcher: every Cognito login attempt "
        f"will simulate: {scenario['description']} ***\n"
        f"*** Open http://localhost:8123 and add the Radoff integration. "
        f"Any credentials you type are NOT sent anywhere. ***\n"
    )

    # Re-exec hass's own entrypoint in-process, same as `hass --config ... --debug`.
    from homeassistant.__main__ import main as hass_main

    sys.argv = ["hass", "--config", str(HASS_CONFIG_DIR), "--debug"]
    return hass_main()


if __name__ == "__main__":
    sys.exit(main())