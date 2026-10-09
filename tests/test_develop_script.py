"""
What `scripts/develop` hands to the Home Assistant process it starts.

Covers: the Cognito pool variables exported from `.env`, and the account
credentials in the same file kept out of the process environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# A stand-in for `hass` that records only the names of the RADOFF_ variables
# it was started with, never their values.
FAKE_HASS = """#!/usr/bin/env bash
env | grep -o '^RADOFF_[A-Z_]*' | sort > "$(dirname "$0")/../seen_env"
"""


def _run_develop(tmp_path: Path, env_file: str) -> tuple[set[str], str]:
    """Run a copy of `scripts/develop` next to `env_file`; return what `hass` saw and stderr."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "develop", tmp_path / "scripts" / "develop")
    (tmp_path / ".devcontainer" / "config").mkdir(parents=True)
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hass = bin_dir / "hass"
    hass.write_text(FAKE_HASS, encoding="utf-8")
    hass.chmod(0o755)

    env = {
        key: value for key, value in os.environ.items() if not key.startswith("RADOFF_")
    }
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    completed = subprocess.run(  # noqa: S603
        ["bash", str(tmp_path / "scripts" / "develop")],  # noqa: S607
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    seen = set((tmp_path / "seen_env").read_text(encoding="utf-8").split())
    return seen, completed.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_develop_exports_the_pool_and_not_the_credentials(tmp_path: Path) -> None:
    """Only the two pool variables of `.env` reach `hass`; username and password stay behind."""
    seen, _ = _run_develop(
        tmp_path,
        "RADOFF_USERNAME=user@example.com\n"
        "RADOFF_PASSWORD=hunter2\n"
        "RADOFF_DEV_POOL_ID=eu-central-1_EnvPool01\n"
        "RADOFF_DEV_CLIENT_ID=env-client-id\n",
    )

    assert seen == {"RADOFF_DEV_POOL_ID", "RADOFF_DEV_CLIENT_ID"}


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_develop_without_the_pool_says_so_and_still_starts(tmp_path: Path) -> None:
    """A `.env` without the pool starts `hass` anyway, naming each missing variable."""
    seen, stderr = _run_develop(tmp_path, "RADOFF_USERNAME=user@example.com\n")

    assert seen == set()
    assert "RADOFF_DEV_POOL_ID" in stderr
    assert "RADOFF_DEV_CLIENT_ID" in stderr
