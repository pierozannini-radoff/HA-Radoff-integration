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
from http import HTTPStatus
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pycognito.aws_srp import AWSSRP

from custom_components.radoff.const import DEFAULT_BASE_URL

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# Fixture REALI, catturate su dev da `scripts/probe_arch2.py` (card M-01) e
# redatte prima di toccare il disco - distinte da quelle sintetiche che
# stanno un livello sopra. Vedi `fixtures/dev/README.md`.
DEV_FIXTURES_DIR = FIXTURES_DIR / "dev"

# Base URL the mocked transport answers on (card M-02). Read from const.py
# instead of being spelled out again: the integration is allowed exactly one
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


def load_devices_fixture(name: str) -> dict[str, Any]:
    """
    Return a `devices_*.json` fixture with freshly-computed telemetry timestamps.

    The synthetic fixtures carry a fixed, illustrative
    `telemetry.timestamp`. Overwriting it with "now" at load time is what
    makes `RadoffEntity.available`'s freshness check (entity.py, based on
    `coordinator.stale_after`) see these readings as fresh regardless of
    when the suite actually runs.

    Card M-03 renamed this from `load_device_fixture` along with what it
    loads: a `GET /data/devices` page holding every device with its
    telemetry inline, instead of one arch 1.x per-device response.
    """
    payload = load_fixture(name)
    now = datetime.now(UTC).isoformat()
    for device in payload.get("devices", []):
        if isinstance(device.get("telemetry"), dict):
            device["telemetry"]["timestamp"] = now
    return payload


def make_id_token(domain_ids: list[str]) -> str:
    """
    Build a syntactically-valid, unsigned Cognito IdToken carrying `d_<uuid>` claims.

    Only the payload segment is real base64url JSON, which was enough while
    the client decoded its own token to bootstrap the arch 1.x domain header
    (`_extract_domain_claims`, removed by card M-02 - arch 2.0's discovery
    endpoint needs nothing but the bearer token). Nothing reads the claims
    any more: the tokens these tests build now only have to be shaped like a
    JWT and carry a plausible payload, and the `d_*` claims are kept because
    a real Radoff IdToken has them.
    """
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
    """
    Mock `GET /data/user/me/domains`.

    Under `/data/`, not `/auth/`: that is where the discovery endpoint
    actually lives in arch 2.0 (M-01 probed both - see
    `custom_components/radoff/api/client.py::DISCOVERY_PATH`), and pinning
    it here is what makes a regression to the documented-but-absent path
    fail the suite instead of only failing against dev.
    """
    requests_mock.get(f"{BASE_URL}/data/user/me/domains", json=payload)


def register_devices(
    requests_mock: Any,
    *pages: dict[str, Any],
    status_code: int = 200,
    base_url: str = BASE_URL,
    with_schema: bool = True,
) -> None:
    """
    Mock the one call a poll cycle makes: `GET /data/devices` (card M-03).

    Replaces `register_search` + `register_device`, the two mocks the 1 + N
    pattern of arch 1.x needed. Pass one payload per page, in order; the
    mock answers each request with the page its `page` query parameter asks
    for, so a test that pins the pagination loop registers two pages and a
    test that does not registers one and never thinks about it again.

    With a non-200 `status_code`, the first payload is the error body and
    every page answers with it - there is nothing to paginate through when
    the call itself fails.
    """
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
    """
    Mock `GET /analytics/measures-ranges`, the schema call of card M-04.

    Answers each `device_type` with the real payload M-01 captured for it
    (`tests/fixtures/dev/measures_ranges__<type>.json`), a type with no
    fixture with the real 404 body carrying `available`, and a call with no
    `device_type` with the merged `measures_ranges__all`. `life` needs no
    special case: the fixture captured for it *is* a 404 body, because that
    is what dev answered.

    Registered by `register_devices` for every test that sets an entry up,
    because from M-04 on a setup makes this call as surely as it makes the
    device one. Pass `payloads`/`status_code` to override that (a schema
    the suite invents, or an endpoint that is down), or re-register the same
    URL afterwards - the last registration wins.
    """

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

        # The `life` fixture is an error body, not a schema (M-01: that type
        # has no schema on dev), and is recognisable by `available`.
        if isinstance(payload, dict) and "available" in payload:
            context.status_code = HTTPStatus.NOT_FOUND
        return payload

    requests_mock.get(f"{base_url}/analytics/measures-ranges", json=_schema_body)


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
