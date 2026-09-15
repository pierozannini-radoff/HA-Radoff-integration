"""
Shared fixtures and helpers for the suite.

The API is mocked at the transport level: `requests_mock` for HTTP,
`AWSSRP.authenticate_user` for the Cognito handshake.
"""

from __future__ import annotations

import base64
import json
import time
from http import HTTPStatus
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pycognito.aws_srp import AWSSRP

from custom_components.radoff.const import DEFAULT_BASE_URL

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# Fixture reali, catturate su dev da `scripts/probe_arch2.py` e redatte
# prima di toccare il disco - distinte da quelle sintetiche che stanno un
# livello sopra. Vedi `fixtures/dev/README.md`.
DEV_FIXTURES_DIR = FIXTURES_DIR / "dev"

# Base URL the mocked transport answers on. Read from const.py instead of
# being spelled out again: the integration is allowed exactly one
# host, and a suite that hardcoded its own copy would keep passing after a
# change that never reached the client. The *paths* below stay literal on
# purpose - those are what the tests are pinning.
BASE_URL = DEFAULT_BASE_URL


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make Home Assistant discover `custom_components.radoff` in every test."""


def load_fixture(name: str) -> dict[str, Any]:
    """Return the parsed JSON body of `tests/fixtures/<name>`."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_dev_fixture(name: str) -> Any:
    """Return the parsed body of a real fixture captured on dev, by file stem."""
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((DEV_FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def load_devices_fixture(name: str) -> dict[str, Any]:
    """Return a `devices_*.json` page with `telemetry.timestamp` and
    `connection_status_updated_at` moved to now, so no assertion depends on
    the fixture's age. A test wanting a specific age sets it itself."""
    payload = load_fixture(name)
    now = datetime.now(UTC).isoformat()
    for device in payload.get("devices", []):
        if isinstance(device.get("telemetry"), dict):
            device["telemetry"]["timestamp"] = now
        if device.get("connection_status_updated_at") is not None:
            device["connection_status_updated_at"] = now
    return payload


def make_id_token(domain_ids: list[str]) -> str:
    """Build an unsigned, JWT-shaped Cognito IdToken carrying `d_<uuid>` claims."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = {f"d_{domain_id}": True for domain_id in domain_ids}
    return f"{header}.{_b64url(payload)}.fake-signature"


def _b64url(obj: dict[str, Any]) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def auth_result(
    id_token: str,
    *,
    access_token: str = "access-token-0",
    refresh_token: str = "refresh-token-0",
    expires_in: int = 3600,
) -> dict[str, Any]:
    """Build a Cognito `AuthenticationResult`-shaped dict for a successful login."""
    return {
        "AuthenticationResult": {
            "IdToken": id_token,
            "AccessToken": access_token,
            "RefreshToken": refresh_token,
            "ExpiresIn": expires_in,
            "TokenType": "Bearer",
        }
    }


def patch_authenticate_user(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    exception: Exception | None = None,
) -> None:
    """Patch the SRP handshake, the one seam that would otherwise talk to Cognito."""

    def _fake_authenticate_user(self: AWSSRP) -> dict[str, Any]:  # noqa: ARG001
        if exception is not None:
            raise exception
        return result

    monkeypatch.setattr(AWSSRP, "authenticate_user", _fake_authenticate_user)


def patch_cognito_refresh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    exception: Exception | None = None,
) -> None:
    """Patch `CognitoSession._refresh`'s `boto3.client(...).get_tokens_from_refresh_token`."""
    import custom_components.radoff.api.auth as auth_module

    class _FakeCognitoIdpClient:
        def get_tokens_from_refresh_token(self, **_kwargs: Any) -> dict[str, Any]:
            if exception is not None:
                raise exception
            return result

    def _fake_boto3_client(*_args: Any, **_kwargs: Any) -> _FakeCognitoIdpClient:
        return _FakeCognitoIdpClient()

    monkeypatch.setattr(auth_module.boto3, "client", _fake_boto3_client)


def register_domains(requests_mock: Any, payload: dict[str, Any]) -> None:
    """Mock the discovery call, `GET /data/user/me/domains`."""
    requests_mock.get(f"{BASE_URL}/data/user/me/domains", json=payload)


def register_devices(
    requests_mock: Any,
    *pages: dict[str, Any],
    status_code: int = 200,
    base_url: str = BASE_URL,
    with_schema: bool = True,
) -> None:
    """Mock `GET /data/devices`, one payload per page in order; with a non-200
    status the first payload is the error body every page answers with."""
    bodies = list(pages) or [{}]

    def _page_body(request: Any, context: Any) -> dict[str, Any]:
        context.status_code = status_code
        if status_code != HTTPStatus.OK:
            return bodies[0]
        requested = int(request.qs.get("page", ["1"])[0])
        return bodies[min(requested, len(bodies)) - 1]

    requests_mock.get(f"{base_url}/data/devices", json=_page_body)

    if with_schema:
        register_measures_ranges(requests_mock, base_url=base_url)


def register_measures_ranges(
    requests_mock: Any,
    *,
    base_url: str = BASE_URL,
    payloads: dict[str, Any] | None = None,
    status_code: int | None = None,
) -> None:
    """Mock `GET /analytics/measures-ranges` from the captured dev fixtures;
    `register_devices` registers it, and `payloads`/`status_code` override it."""

    def _schema_body(request: Any, context: Any) -> Any:
        device_type = request.qs.get("device_type", [None])[0]
        if status_code is not None:
            context.status_code = status_code
        if payloads is not None:
            context.status_code = status_code or HTTPStatus.OK
            return payloads.get(device_type, {})

        name = (
            "measures_ranges__all"
            if device_type is None
            else f"measures_ranges__{device_type}"
        )
        try:
            payload = load_dev_fixture(name)
        except FileNotFoundError:
            context.status_code = HTTPStatus.NOT_FOUND
            return load_dev_fixture("error__measures_ranges_unknown_type")

        # The `life` fixture is an error body, not a schema - that type has
        # no schema on dev - and is recognisable by `available`.
        if isinstance(payload, dict) and "available" in payload:
            context.status_code = HTTPStatus.NOT_FOUND
        return payload

    requests_mock.get(f"{base_url}/analytics/measures-ranges", json=_schema_body)


@pytest.fixture
def config_entry_v1_data() -> dict[str, Any]:
    """Config entry `data` shaped as VERSION 1: Cognito fields still present."""
    return {
        "username": "user@example.com",
        "password": "hunter2",
        "client_id": "legacy-client-id",
        "pool_id": "legacy-pool-id",
        "pool_region": "eu-west-1",
        "generate_index": False,
    }


@pytest.fixture
def config_entry_v2_data() -> dict[str, Any]:
    """Config entry `data` shaped as VERSION 2: no Cognito fields, domain as a UUID."""
    return {
        "username": "user@example.com",
        "password": "hunter2",
        "domain_id": "aaaaaaaa-0000-0000-0000-000000000001",
    }


@pytest.fixture
def config_entry_v3_data() -> dict[str, Any]:
    """Config entry `data` shaped as VERSION 3: a `domain_prefix`, the current shape."""
    return {
        "username": "user@example.com",
        "password": "hunter2",
        "domain_prefix": "home1234",
    }


# The entity/device registry of a real installation running the released
# version (30e0cde), measured entry by entry: two devices, sixteen entities
# each, 32 `unique_id`s of the form `radoff-{device_uuid}-{slug}`.
#
# The *shape* is the measured one - the exact slug set, the `-index` suffix
# on the seven qualitative siblings, the Italian `entity_id`s the released
# version's friendly names slugified to, and the fact that the AQI has no
# `-index` sibling while eco2 does. The identifiers themselves are
# synthetic: what the migration has to get right is the structure, and a
# real account's device UUIDs are not something to commit to a public
# repository.
REGISTRY_RELEASED_BACKUP = FIXTURES_DIR / "registry_released_backup.json"


def seed_released_registry(
    hass: Any,
    config_entry: Any,
    *,
    slugs: dict[str, list[str]] | None = None,
    unit_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Recreate a released installation's registries and return
    `{unique_id: entity_id}`. `slugs` narrows what is created per serial,
    `unit_overrides` sets a user display-unit override on the named entities."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.radoff.const import DOMAIN

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    payload = json.loads(REGISTRY_RELEASED_BACKUP.read_text(encoding="utf-8"))

    created: dict[str, str] = {}

    for device in payload["devices"]:
        serial = device["serial_number"]
        wanted = None if slugs is None else set(slugs.get(serial, []))

        device_entry = device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, serial)},
            name=device["name"],
            manufacturer="Radoff",
            model=device["device_type"],
        )

        for entity in device["entities"]:
            if wanted is not None and entity["slug"] not in wanted:
                continue

            entry = entity_registry.async_get_or_create(
                "sensor",
                DOMAIN,
                entity["unique_id"],
                suggested_object_id=entity["entity_id"].removeprefix("sensor."),
                config_entry=config_entry,
                device_id=device_entry.id,
            )
            created[entity["unique_id"]] = entry.entity_id

            override = (unit_overrides or {}).get(entity["slug"])
            if override:
                entity_registry.async_update_entity_options(
                    entry.entity_id, "sensor", {"unit_of_measurement": override}
                )

    return created


@pytest.fixture(autouse=True)
def _fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the clock so `CognitoSession`'s expiry math is deterministic."""
    frozen = time.time()
    monkeypatch.setattr(time, "time", lambda: frozen)
