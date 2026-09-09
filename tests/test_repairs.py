"""
Repairs fix flow tests (RT-2926): resolving a missing `domain_id`.

The flow is driven through the real repairs flow manager - the same object
the frontend talks to - rather than by calling `MissingDomainIdRepairFlow`
directly, so issue registration, platform discovery (`repairs.py` being
found at all) and the manager's own "delete the issue unless the flow
aborts" rule are all part of what these tests cover. The Radoff API is
mocked at the transport level exactly as everywhere else in this suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    DOMAIN,
    ISSUE_MISSING_DOMAIN_ID,
)

from .conftest import (
    BASE_DOMAIN,
    auth_result,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    register_domains,
    register_search,
)

DOMAIN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER_DOMAIN_ID = "bbbbbbbb-0000-0000-0000-000000000002"


async def _setup_broken_entry(
    hass: HomeAssistant, config_entry_v1_data: dict[str, Any]
) -> MockConfigEntry:
    """Add a real-shaped v1 entry (no `domain_id`) and let its setup fail."""
    await async_setup_component(hass, "repairs", {})

    entry = MockConfigEntry(domain=DOMAIN, version=1, data=config_entry_v1_data)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return entry


async def _start_fix_flow(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Open the fix flow for this entry's issue, as the frontend would."""
    issue_id = f"{ISSUE_MISSING_DOMAIN_ID}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    flow_manager = hass.data["repairs"]["flow_manager"]
    return await flow_manager.async_init(DOMAIN, data={"issue_id": issue_id})


async def _configure(hass: HomeAssistant, flow_id: str, user_input: Any) -> Any:
    return await hass.data["repairs"]["flow_manager"].async_configure(
        flow_id, user_input
    )


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_MISSING_DOMAIN_ID}_{entry.entry_id}"
    )


async def test_repair_single_domain_loads_the_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    The nominal upgrade path: one accessible domain, one confirmation, done.

    This is the acceptance criterion of RT-2926 in one test - an entry
    created by the released version, with no `domain_id`, ends up loaded
    without the user ever retyping a credential.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_search(requests_mock, {"devices": []})

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await _configure(hass, result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN_ID] == DOMAIN_ID
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is None


async def test_repair_multiple_domains_asks_which_one(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """More than one accessible domain: the choice is put to the user, not guessed."""
    patch_authenticate_user(
        monkeypatch, result=auth_result(make_id_token([DOMAIN_ID, OTHER_DOMAIN_ID]))
    )
    register_domains(requests_mock, load_fixture("domains_multi.json"))
    register_search(requests_mock, {"devices": []})

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "domain"

    result = await _configure(
        hass, result["flow_id"], {CONF_DOMAIN_ID: OTHER_DOMAIN_ID}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN_ID] == OTHER_DOMAIN_ID
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is None


async def test_repair_keeps_generate_index_option_from_the_migration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """The repair only writes `domain_id`: what the migration moved stays put."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_search(requests_mock, {"devices": []})

    entry = await _setup_broken_entry(hass, config_entry_v1_data)
    result = await _start_fix_flow(hass, entry)
    await _configure(hass, result["flow_id"], {})
    await hass.async_block_till_done()

    # config_entry_v1_data carries generate_index=False in `data`; the
    # migration moved it into `options` and the repair must not undo that.
    assert entry.options[CONF_INDEX] is False
    assert CONF_INDEX not in entry.data


async def test_repair_aborts_and_keeps_the_issue_on_invalid_auth(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    A rejected password aborts with its own reason and leaves the repair open.

    The credentials come from the entry, so the user has to fix them
    elsewhere (re-auth) and come back: the issue must survive the abort, or
    the only pointer to a still-unusable entry would be gone.
    """
    patch_authenticate_user(
        monkeypatch,
        exception=ClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "bad password"}},
            "InitiateAuth",
        ),
    )

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"
    assert CONF_DOMAIN_ID not in entry.data
    assert _issue(hass, entry) is not None


async def test_repair_aborts_when_the_account_has_no_domain(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """An account with no accessible domain has nothing to repair with."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_domains(requests_mock, load_fixture("domains_empty.json"))

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_domains"
    assert _issue(hass, entry) is not None


async def test_repair_aborts_when_the_backend_is_unreachable(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """A failing discovery call is a transient problem: abort, keep the repair."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    requests_mock.get(f"{BASE_DOMAIN}/auth/user/me/domains", status_code=500, json={})

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    assert _issue(hass, entry) is not None


async def test_repair_aborts_on_an_unsupported_cognito_challenge(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """A challenge this integration cannot complete (S-09/C8) aborts here too."""
    patch_authenticate_user(
        monkeypatch,
        result={"ChallengeName": "NEW_PASSWORD_REQUIRED", "ChallengeParameters": {}},
    )

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_challenge"
    assert _issue(hass, entry) is not None


async def test_repair_aborts_when_the_entry_is_gone(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    Removing the entry instead of repairing it is a legitimate way out.

    Until RT-2926 it was the *only* one (see the card's "AGGRAVANTE"). The
    stale issue must then abort cleanly rather than raise on a `None` entry.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))

    entry = await _setup_broken_entry(hass, config_entry_v1_data)
    issue_id = f"{ISSUE_MISSING_DOMAIN_ID}_{entry.entry_id}"

    flow_manager = hass.data["repairs"]["flow_manager"]
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    result = await flow_manager.async_init(DOMAIN, data={"issue_id": issue_id})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"
