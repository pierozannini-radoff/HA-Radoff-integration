"""
Shared fixtures/helpers for the S-18 test suite.

The Radoff API is mocked at the TRANSPORT level, not per-function: HTTP calls
made through `api/client.py`'s `requests.Session` are intercepted with
`requests_mock` (provided by `pytest-homeassistant-custom-component`), and
the Cognito SRP handshake is intercepted at `pycognito.aws_srp.AWSSRP.
authenticate_user` - the one seam `api/auth.py::authenticate_user` calls
through, regardless of which config-flow/coordinator code path triggers it.
This way every test exercises auth, discovery, entity construction and
migration exactly as Home Assistant does, not as a shortcut around it.

Fixture JSON payloads under `tests/fixtures/` are anonymized: device/serial
ids and domain ids are synthetic placeholders, not values captured from a
real account.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pycognito.aws_srp import AWSSRP

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# Fixture REALI, catturate su dev da `scripts/probe_arch2.py` (card M-01) e
# redatte prima di toccare il disco - distinte da quelle sintetiche che
# stanno un livello sopra. Vedi `fixtures/dev/README.md`.
DEV_FIXTURES_DIR = FIXTURES_DIR / "dev"
BASE_DOMAIN = "https://api.iot.radoff.life/api/v1/core"


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make Home Assistant discover `custom_components.radoff` in every test."""


def load_fixture(name: str) -> dict[str, Any]:
    """Return the parsed JSON body of `tests/fixtures/<name>`."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_dev_fixture(name: str) -> Any:
    """
    Return the parsed body of a real fixture captured on dev (card M-01).

    `name` is the file's stem as recorded in `_manifest.json`, with or
    without the `.json` suffix - e.g. `load_dev_fixture("devices__full")`.
    Unlike `load_fixture` above these payloads were captured from the real
    arch 2.0 API rather than written by hand, which is the whole point:
    from M-02 onwards the client is written against what the API actually
    returns, not against a payload we imagined.
    """
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((DEV_FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def load_device_fixture(name: str) -> dict[str, Any]:
    """
    Return a `device_*.json` fixture with a freshly-computed `lastDataReceivedAt`.

    The fixture files carry a fixed, anonymized-looking timestamp - not a
    real captured value, just illustrative. Overwriting it with "now" at
    load time is what makes `RadoffEntity.available`'s freshness check
    (entity.py, based on `coordinator.stale_after`) see these readings as
    fresh regardless of when the suite actually runs.
    """
    payload = load_fixture(name)
    payload["data"]["lastDataReceivedAt"] = datetime.now(UTC).isoformat()
    return payload


def make_id_token(domain_ids: list[str]) -> str:
    """
    Build a syntactically-valid, unsigned Cognito IdToken carrying `d_<uuid>` claims.

    Only the payload segment is real base64url JSON - `_extract_domain_claims`
    (api/client.py) only ever reads that segment without verifying the
    signature, matching how a real, signed IdToken would be handled by this
    integration (it never verifies Cognito's signature either).
    """
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = {f"d_{domain_id}": True for domain_id in domain_ids}
    return f"{header}.{_b64url(payload)}.fake-signature"


def make_malformed_id_token() -> str:
    """Return a token with a payload segment that is not valid base64/JSON."""
    return "header.not-valid-base64!!!.signature"


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
    """
    Patch the one seam `api/auth.py::authenticate_user` calls: the SRP handshake.

    `AWSSRP.__init__` does no network I/O by itself (it only prepares the SRP
    math), so tests never need to patch it - only `authenticate_user`, the
    method that would otherwise talk to Cognito.
    """

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
    """Mock `GET /auth/user/me/domains`."""
    requests_mock.get(f"{BASE_DOMAIN}/auth/user/me/domains", json=payload)


def register_search(requests_mock: Any, payload: dict[str, Any]) -> None:
    """Mock `POST /data/devices/search`."""
    requests_mock.post(f"{BASE_DOMAIN}/data/devices/search", json=payload)


def register_device(
    requests_mock: Any,
    device_id: str,
    payload: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> None:
    """Mock `GET /data/devices/{device_id}`."""
    requests_mock.get(
        f"{BASE_DOMAIN}/data/devices/{device_id}",
        json=payload or {},
        status_code=status_code,
    )


@pytest.fixture
def config_entry_v1_data() -> dict[str, Any]:
    """Config entry `data` shaped as VERSION 1 (pre-S-02) - Cognito fields still present."""
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
    """Config entry `data` shaped as VERSION 2 (post-S-02) - no Cognito fields."""
    return {
        "username": "user@example.com",
        "password": "hunter2",
        "domain_id": "aaaaaaaa-0000-0000-0000-000000000001",
    }


@pytest.fixture(autouse=True)
def _fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep `CognitoSession`'s expiry math deterministic across the whole suite.

    Not strictly required by every test, but avoids any flakiness from a
    token minted with `expires_in=3600` crossing its own expiry margin
    (`_TOKEN_EXPIRY_MARGIN_SECONDS` = 300s) during a slow test run.
    """
    frozen = time.time()
    monkeypatch.setattr(time, "time", lambda: frozen)
