"""
Config flow tests (S-18): auth outcomes and domain discovery.

The Cognito SRP handshake is mocked at `AWSSRP.authenticate_user` (the seam
`api/auth.py::authenticate_user` calls through); HTTP calls (`/auth/user/me/
domains`, `/data/devices/search`, `/data/devices/{id}`) are mocked at the
`requests` transport level via `requests_mock`. Both are exercised exactly as
Home Assistant exercises them - through the config flow and the coordinator,
never by calling api/client.py's methods directly.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.api.auth import (
    AuthInvalidError,
    AuthUnavailableError,
    CognitoSession,
)
from custom_components.radoff.api.client import API
from custom_components.radoff.const import CONF_DOMAIN_ID, DOMAIN

from .conftest import (
    BASE_DOMAIN,
    auth_result,
    load_fixture,
    make_id_token,
    make_malformed_id_token,
    patch_authenticate_user,
    patch_cognito_refresh,
    register_device,
    register_domains,
    register_search,
)

USER_INPUT = {"username": "user@example.com", "password": "hunter2"}


# ---------------------------------------------------------------------------
# Auth: happy path, wrong credentials, NEW_PASSWORD_REQUIRED challenge
# ---------------------------------------------------------------------------


async def test_auth_happy_path_single_domain(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A single accessible domain creates the entry in one step, no extra form."""
    id_token = make_id_token(["11111111-1111-1111-1111-111111111111"])
    patch_authenticate_user(monkeypatch, result=auth_result(id_token))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_search(requests_mock, {"devices": []})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The created entry's domain_id comes from the discovery response body
    # (domains_single.json), not from the IdToken's own "d_*" claim - that
    # claim is only used to bootstrap the discovery call's x-domain header.
    assert result["data"][CONF_DOMAIN_ID] == "aaaaaaaa-0000-0000-0000-000000000001"


async def test_auth_invalid_credentials(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cognito rejecting the password surfaces as errors.base == invalid_auth."""
    patch_authenticate_user(
        monkeypatch,
        exception=ClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "bad password"}},
            "InitiateAuth",
        ),
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_auth_challenge_new_password_required(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NEW_PASSWORD_REQUIRED challenge aborts cleanly, no entry created."""
    patch_authenticate_user(
        monkeypatch,
        result={"ChallengeName": "NEW_PASSWORD_REQUIRED", "ChallengeParameters": {}},
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_challenge"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_auth_refresh_succeeds_without_srp_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid RefreshToken is used via GetTokensFromRefreshToken, not a new SRP login."""
    session = CognitoSession(
        username="user@example.com",
        password="hunter2",
        client_id="client-id",
        pool_id="pool-id",
        pool_region="eu-west-1",
    )
    session.tokens = {"IdToken": "stale-token"}
    session._token_expires_at = 0  # noqa: SLF001 - force expiry
    session._refresh_token = "refresh-token-0"  # noqa: SLF001

    def _fail_if_called(*_a: Any, **_k: Any) -> None:
        msg = "SRP login must not be attempted when a refresh succeeds"
        raise AssertionError(msg)

    monkeypatch.setattr(session, "_srp_login", _fail_if_called)
    patch_cognito_refresh(monkeypatch, result=auth_result("new-id-token"))

    bearer = session.get_bearer()

    assert bearer == "new-id-token"


async def test_auth_refresh_rejected_twice_raises_auth_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two consecutive rejected refreshes raise AuthInvalidError (card S-12 AC).

    Cycle 1: `_refresh()` fails (counter -> 1, below the threshold), the
    same-cycle SRP fallback is ALSO unable to complete (a transient
    `AuthUnavailableError`) - this propagates as-is, the counter untouched by
    that second failure. Cycle 2: `_refresh()` fails again on the still-bad
    RefreshToken (counter -> 2, at the threshold) -> `AuthInvalidError`, which
    `coordinator.py` turns into `ConfigEntryAuthFailed` (opening re-auth).
    """
    session = CognitoSession(
        username="user@example.com",
        password="hunter2",
        client_id="client-id",
        pool_id="pool-id",
        pool_region="eu-west-1",
    )
    session.tokens = {"IdToken": "stale-token"}
    session._token_expires_at = 0  # noqa: SLF001
    session._refresh_token = "refresh-token-0"  # noqa: SLF001

    monkeypatch.setattr(
        session,
        "_refresh",
        _raise(ClientError({"Error": {"Code": "NotAuthorizedException"}}, "x")),
    )
    monkeypatch.setattr(session, "_srp_login", _raise(AuthUnavailableError("down")))

    with pytest.raises(AuthUnavailableError):
        session.get_bearer()

    with pytest.raises(AuthInvalidError):
        session.get_bearer()

    assert session._refresh_token is None  # noqa: SLF001


def _raise(exc: Exception):
    def _fn(*_a: Any, **_k: Any) -> None:
        raise exc

    return _fn


# ---------------------------------------------------------------------------
# Discovery (S-01 note): single domain, multiple domains, zero domains
# ---------------------------------------------------------------------------


async def test_config_flow_multiple_domains(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    Discovery returning >1 domain shows the domain step.

    The selected domain_id both ends up in the entry's data AND is used as
    the `x-domain` header of every subsequent API call - asserted directly on
    the mocked search request, not only inferred from the entry.
    """
    id_token = make_id_token(
        [
            "aaaaaaaa-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        ]
    )
    patch_authenticate_user(monkeypatch, result=auth_result(id_token))
    register_domains(requests_mock, load_fixture("domains_multi.json"))
    register_search(requests_mock, {"devices": []})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "domain"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_DOMAIN_ID: "bbbbbbbb-0000-0000-0000-000000000002"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_DOMAIN_ID] == "bbbbbbbb-0000-0000-0000-000000000002"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    search_request = next(
        req
        for req in requests_mock.request_history
        if req.url == f"{BASE_DOMAIN}/data/devices/search"
    )
    assert search_request.headers["x-domain"] == "bbbbbbbb-0000-0000-0000-000000000002"


async def test_config_flow_no_domains_no_claims(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """No `d_*` claim in the IdToken at all -> abort, and no discovery call is made."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_domains"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert not any(
        req.url == f"{BASE_DOMAIN}/auth/user/me/domains"
        for req in requests_mock.request_history
    )


async def test_config_flow_no_domains_empty_discovery(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A claim exists, but discovery itself returns zero domains -> abort."""
    id_token = make_id_token(["aaaaaaaa-0000-0000-0000-000000000001"])
    patch_authenticate_user(monkeypatch, result=auth_result(id_token))
    register_domains(requests_mock, load_fixture("domains_empty.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_domains"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_no_discovery_on_reload(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v2_data: dict[str, Any],
) -> None:
    """An entry with domain_id already set never calls /auth/user/me/domains."""
    patch_authenticate_user(
        monkeypatch, result=auth_result(make_id_token(["should-not-be-used"]))
    )
    register_search(requests_mock, {"devices": []})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v2_data,
        options={"generate_index": True},
        version=2,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not any(
        req.url == f"{BASE_DOMAIN}/auth/user/me/domains"
        for req in requests_mock.request_history
    )


def test_domain_id_survives_session_invalidate() -> None:
    """
    Regression: `CognitoSession.invalidate()` must never clear `API.domain`.

    S-12 originally introduced, then fixed, a bug where reconnecting cleared
    the tenant domain - `api.domain` lives on `API` itself, entirely separate
    from the token state `invalidate()` touches.
    """
    api = API(
        username="user@example.com",
        password="hunter2",
        domain_id="aaaaaaaa-0000-0000-0000-000000000001",
    )
    api._session.tokens = {"IdToken": "some-token"}  # noqa: SLF001

    api._session.invalidate()  # noqa: SLF001

    assert api.domain == "aaaaaaaa-0000-0000-0000-000000000001"


# ---------------------------------------------------------------------------
# Pure unit test: _extract_domain_claims
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("id_token", "expected"),
    [
        (
            make_id_token(["b-domain", "a-domain", "c-domain"]),
            ["a-domain", "b-domain", "c-domain"],
        ),
        (make_id_token([]), []),
        (make_malformed_id_token(), []),
        ("not-even-two-dots", []),
    ],
)
def test_extract_domain_claims(id_token: str, expected: list[str]) -> None:
    """Deterministic (sorted) order; no claims, and malformed tokens both -> []."""
    api = API(username="u", password="p")
    assert api._extract_domain_claims(id_token) == expected  # noqa: SLF001
