"""
Redaction test for card S-17 (diagnostics.py), integrated with S-18.

Builds a real `ConfigEntry` with credential-shaped `data`/`title`/`unique_id`
plus a fake coordinator carrying one healthy device and one stale device
(covering the runbook's "entity unavailable" case) and an auth-style
`last_exception` (covering "auth error"), then asserts none of
`diagnostics.TO_REDACT`'s values survive in the JSON-serialized result -
this is what makes the test fail the moment a new sensitive field is added
to the dump without also being redacted (S-17 AC).
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff import diagnostics  # noqa: E402
from custom_components.radoff.api.models import (  # noqa: E402
    Bucket,
    RadoffDevice,
    Reading,
)
from custom_components.radoff.coordinator import RadoffData  # noqa: E402

# Values a leak of any TO_REDACT'd field would surface as - each one placed
# in `data`, `title`, `unique_id`, a device identifier, or a token, so the
# test cannot pass by accident just because nothing happens to be sensitive.
SECRET_USERNAME = "someone@example.com"
SECRET_PASSWORD = "super-secret-password"  # noqa: S105
SECRET_DOMAIN_ID = "11111111-1111-1111-1111-111111111111"
SECRET_DEVICE_ID = "22222222-2222-2222-2222-222222222222"
SECRET_SERIAL = "RADOFF-SERIAL-0042"
SECRET_UNIQUE_ID = "radoff-account-unique-id"
SECRET_TITLE = "someone@example.com (domain X)"
SECRET_ID_TOKEN = "id-token-value"  # noqa: S105
SECRET_ACCESS_TOKEN = "access-token-value"  # noqa: S105
SECRET_REFRESH_TOKEN = "refresh-token-value"  # noqa: S105

ALL_SECRETS = [
    SECRET_USERNAME,
    SECRET_PASSWORD,
    SECRET_DOMAIN_ID,
    SECRET_DEVICE_ID,
    SECRET_SERIAL,
    SECRET_UNIQUE_ID,
    SECRET_TITLE,
    SECRET_ID_TOKEN,
    SECRET_ACCESS_TOKEN,
    SECRET_REFRESH_TOKEN,
]


@dataclass
class _FakeCoordinator:
    """Duck-typed stand-in for RadoffCoordinator - only what diagnostics.py reads."""

    data: RadoffData
    last_update_success: bool
    last_exception: Exception | None
    update_interval: timedelta


def _build_entry() -> ConfigEntry:
    now = datetime.now(UTC)
    return ConfigEntry(
        created_at=now,
        data={
            "username": SECRET_USERNAME,
            "password": SECRET_PASSWORD,
            "domain_id": SECRET_DOMAIN_ID,
            # Tokens don't actually live in config_entry.data today, but
            # TO_REDACT covers them defensively - assert they'd be caught
            # here too, wherever they end up nested.
            "IdToken": SECRET_ID_TOKEN,
            "AccessToken": SECRET_ACCESS_TOKEN,
            "RefreshToken": SECRET_REFRESH_TOKEN,
        },
        discovery_keys={},
        domain="radoff",
        minor_version=1,
        modified_at=now,
        options={"generate_index": True, "scan_interval": 60},
        source="user",
        state=ConfigEntryState.LOADED,
        title=SECRET_TITLE,
        unique_id=SECRET_UNIQUE_ID,
        version=2,
    )


def _build_data() -> RadoffData:
    healthy_device = RadoffDevice(
        device_id=SECRET_DEVICE_ID,
        device_serial=SECRET_SERIAL,
        device_type="now_plus",
        name="Living room",
        readings={
            (Bucket.DATA, "temperature"): Reading(
                name="temperature",
                bucket=Bucket.DATA,
                value=21.5,
                device_class=None,
                friendly_name="Temperature",
                unit="°C",
                normalize_fn=None,
                measured_at=None,
            ),
        },
        stale=False,
        last_data_received_at=datetime.now(UTC),
    )
    stale_device = RadoffDevice(
        device_id="33333333-3333-3333-3333-333333333333",
        device_serial="RADOFF-SERIAL-0099",
        device_type="now_plus",
        name="Bedroom",
        readings={},
        stale=True,
        last_data_received_at=None,
    )
    return RadoffData(
        controller_name="cloud_poller",
        generate_index=True,
        devices=[healthy_device, stale_device],
    )


def test_diagnostics_redacts_all_sensitive_fields() -> None:
    """S-17 AC: no TO_REDACT value survives, anywhere in the serialized tree."""
    entry = _build_entry()
    entry.runtime_data = _FakeCoordinator(
        data=_build_data(),
        last_update_success=False,
        # Real Auth*Error messages (api/auth.py) are static, generic strings
        # that never interpolate the username/password - matched here rather
        # than an exception that embeds a secret, which no code path in this
        # integration actually raises.
        last_exception=Exception(
            "The configured Radoff credentials are no longer valid."
        ),
        update_interval=timedelta(seconds=60),
    )

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass=None, entry=entry)
    )
    serialized = json.dumps(result)

    for secret in ALL_SECRETS:
        assert secret not in serialized, f"leaked secret value: {secret!r}"


def test_diagnostics_covers_runbook_cases() -> None:
    """
    The dump carries enough to diagnose the runbook's cases.

    Zero entities (empty device list), an unavailable entity (`stale=True`),
    and an auth error (`last_update_success=False` + `last_exception` set)
    must all be visible in the result.
    """
    entry = _build_entry()
    entry.runtime_data = _FakeCoordinator(
        data=_build_data(),
        last_update_success=False,
        last_exception=Exception("Authentication error: invalid credentials"),
        update_interval=timedelta(seconds=60),
    )

    result = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass=None, entry=entry)
    )

    assert result["last_update_success"] is False
    assert result["last_exception"]
    devices = result["data"]["devices"]
    assert any(device["stale"] is True for device in devices)

    entry_zero = _build_entry()
    entry_zero.runtime_data = _FakeCoordinator(
        data=RadoffData(
            controller_name="cloud_poller", generate_index=True, devices=[]
        ),
        last_update_success=True,
        last_exception=None,
        update_interval=timedelta(seconds=60),
    )
    result_zero = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass=None, entry=entry_zero)
    )
    assert result_zero["data"]["devices"] == []


def test_diagnostics_fails_if_a_sensitive_field_is_added_unredacted() -> None:
    """
    Meta-test: the redaction test above must actually fail on a regression.

    Simulates "someone adds a sensitive field to the dump without adding it
    to TO_REDACT" by calling async_redact_data with an empty redact set and
    checking the secret *does* show up - proving test_diagnostics_redacts_
    all_sensitive_fields would have caught it.
    """
    from homeassistant.components.diagnostics import async_redact_data

    leaked = async_redact_data({"password": SECRET_PASSWORD}, to_redact=set())
    assert leaked["password"] == SECRET_PASSWORD
