"""
The config flow and the options flow.

Covers: auth outcomes, domain discovery and the domain step, the base URL
option, and the polling interval bounds.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.api.auth import (
    AuthInvalidError,
    AuthUnavailableError,
    CognitoSession,
)
from custom_components.radoff.api.client import API
from custom_components.radoff.const import (
    CONF_BASE_URL,
    CONF_DOMAIN_PREFIX,
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

from .conftest import (
    auth_result,
    load_devices_fixture,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    patch_cognito_refresh,
    register_domains,
    register_devices,
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
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The entry's domain comes from the discovery response body
    # (domains_single.json) and is the readable `prefix`, not a UUID.
    assert result["data"][CONF_DOMAIN_PREFIX] == "home1234"


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
    """Two consecutive rejected refreshes raise `AuthInvalidError`."""
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
# Discovery: single domain, multiple domains, zero domains
# ---------------------------------------------------------------------------


async def test_config_flow_multiple_domains(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """More than one domain shows the domain step, and the choice scopes every call."""
    id_token = make_id_token(
        [
            "aaaaaaaa-0000-0000-0000-000000000001",
            "bbbbbbbb-0000-0000-0000-000000000002",
        ]
    )
    patch_authenticate_user(monkeypatch, result=auth_result(id_token))
    register_domains(requests_mock, load_fixture("domains_multi.json"))
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

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
        {CONF_DOMAIN_PREFIX: "office56"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.data[CONF_DOMAIN_PREFIX] == "office56"

    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    devices_request = next(
        req for req in requests_mock.request_history if req.path == "/data/devices"
    )
    assert devices_request.qs["domain_prefix"] == ["office56"]
    assert "x-domain" not in devices_request.headers


async def test_discovery_no_longer_needs_a_domain_claim(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """An IdToken with no `d_*` claim still reaches discovery and creates the entry."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DOMAIN_PREFIX] == "home1234"
    assert any(
        req.path == "/data/user/me/domains" for req in requests_mock.request_history
    )


# ---------------------------------------------------------------------------
# The arch 2.0 discovery response, and what the flow does with it
# ---------------------------------------------------------------------------


async def test_a_single_domain_is_never_put_to_the_user(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """With one domain there is no domain step at all, not a prefilled one."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DOMAIN_PREFIX] == "home1234"


async def test_the_domain_choice_is_labelled_with_the_readable_name(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """The domain choice is labelled with `name`, falling back to the prefix."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))
    register_domains(requests_mock, load_fixture("domains_multi.json"))
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["step_id"] == "domain"
    choices = result["data_schema"].schema[CONF_DOMAIN_PREFIX].container
    assert choices == {
        "home1234": "Home",
        "office56": "Office",
        "nameless": "nameless",
    }


async def test_a_domain_with_no_device_aborts_with_its_own_reason(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A domain with no device aborts with its own reason, not with an empty setup."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_devices(requests_mock, load_fixture("devices_empty.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_failing_device_check_does_not_block_the_setup(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A failing device check does not veto the setup: it has no opinion to give."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_devices(requests_mock, {"message": "boom"}, status_code=500)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DOMAIN_PREFIX] == "home1234"


async def test_a_domain_without_a_prefix_is_skipped(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A domain element with no prefix is dropped; the intact ones still set up."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([])))
    payload = load_fixture("domains_single.json")
    payload["domains"].append({"domain": {"name": "Broken"}, "role": {}})
    register_domains(requests_mock, payload)
    register_devices(requests_mock, load_devices_fixture("devices_one_device.json"))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DOMAIN_PREFIX] == "home1234"


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
    config_entry_v3_data: dict[str, Any],
) -> None:
    """An entry with a domain_prefix already set never calls /data/user/me/domains."""
    patch_authenticate_user(
        monkeypatch, result=auth_result(make_id_token(["should-not-be-used"]))
    )
    register_devices(requests_mock, load_fixture("devices_empty.json"))

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_v3_data,
        options={"generate_index": True},
        version=3,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not any(
        req.path == "/data/user/me/domains" for req in requests_mock.request_history
    )


def test_the_domain_prefix_survives_session_invalidate() -> None:
    """`CognitoSession.invalidate()` clears the tokens; `domain_prefix` survives."""
    api = API(
        username="user@example.com",
        password="hunter2",
        domain_prefix="home1234",
    )
    api._session.tokens = {"IdToken": "some-token"}  # noqa: SLF001

    api._session.invalidate()  # noqa: SLF001

    assert not api._session.tokens  # noqa: SLF001
    assert api.domain_prefix == "home1234"


# ---------------------------------------------------------------------------
# Options flow: the advanced base URL field
# ---------------------------------------------------------------------------


async def _loaded_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    options: dict[str, Any],
) -> MockConfigEntry:
    """Set up a real, loaded entry with `options`, so its options flow can run."""
    patch_authenticate_user(
        monkeypatch, result=auth_result(make_id_token(["should-not-be-used"]))
    )
    # On whichever host these options point at: an entry carrying a
    # `base_url` override polls that one, and its first refresh has to
    # succeed for the entry to load and its options flow to be reachable.
    register_devices(
        requests_mock,
        load_fixture("devices_empty.json"),
        base_url=options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
    )

    entry = MockConfigEntry(
        domain=DOMAIN, data=config_entry_v3_data, options=options, version=3
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _open_options(
    hass: HomeAssistant, entry: MockConfigEntry, *, advanced: bool
) -> dict[str, Any]:
    return await hass.config_entries.options.async_init(
        entry.entry_id, context={"show_advanced_options": advanced}
    )


async def test_options_flow_shows_the_base_url_only_in_advanced_mode(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """With advanced mode off the field is not in the schema at all."""
    entry = await _loaded_entry(
        hass, monkeypatch, requests_mock, config_entry_v3_data, {"generate_index": True}
    )

    normal = await _open_options(hass, entry, advanced=False)
    assert CONF_BASE_URL not in {str(key) for key in normal["data_schema"].schema}

    advanced = await _open_options(hass, entry, advanced=True)
    assert CONF_BASE_URL in {str(key) for key in advanced["data_schema"].schema}


async def test_saving_options_without_the_field_keeps_the_base_url(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """Saving options without the base URL field keeps the override already stored."""
    other_host = "https://api.int.iot.radoff.life"
    entry = await _loaded_entry(
        hass,
        monkeypatch,
        requests_mock,
        config_entry_v3_data,
        {"generate_index": True, CONF_BASE_URL: other_host},
    )

    result = await _open_options(hass, entry, advanced=False)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 120, "generate_index": True}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BASE_URL] == other_host
    assert entry.options["scan_interval"] == 120


@pytest.mark.parametrize(
    "submitted",
    [f"{DEFAULT_BASE_URL}/", ""],
    ids=["the default with a trailing slash", "blank"],
)
async def test_a_base_url_equal_to_the_default_or_blank_is_not_stored(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
    submitted: str,
) -> None:
    """A base URL equal to the default, trailing slash included, or blank, is not stored."""
    entry = await _loaded_entry(
        hass,
        monkeypatch,
        requests_mock,
        config_entry_v3_data,
        {"generate_index": True, CONF_BASE_URL: "https://api.int.iot.radoff.life"},
    )

    result = await _open_options(hass, entry, advanced=True)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 60,
            "generate_index": True,
            CONF_BASE_URL: submitted,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_BASE_URL not in entry.options


async def test_the_form_refuses_an_interval_below_the_floor(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """The form refuses an interval below `MIN_SCAN_INTERVAL`, whatever the frontend sent."""
    entry = await _loaded_entry(
        hass, monkeypatch, requests_mock, config_entry_v3_data, {"generate_index": True}
    )

    result = await _open_options(hass, entry, advanced=False)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"scan_interval": MIN_SCAN_INTERVAL - 1, "generate_index": True},
        )

    assert "scan_interval" not in entry.options


async def test_an_entry_that_never_set_an_interval_polls_at_the_default(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """An entry that never set an interval polls at the default, and the form shows it."""
    entry = await _loaded_entry(
        hass, monkeypatch, requests_mock, config_entry_v3_data, {"generate_index": True}
    )

    assert DEFAULT_SCAN_INTERVAL == 300
    assert entry.runtime_data.update_interval.total_seconds() == DEFAULT_SCAN_INTERVAL

    result = await _open_options(hass, entry, advanced=False)
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if key.default
    }
    assert defaults["scan_interval"] == DEFAULT_SCAN_INTERVAL
