"""
Repairs fix flow tests (RT-2926, M-07): giving an entry a `domain_prefix`.

The flow is driven through the real repairs flow manager - the same object
the frontend talks to - rather than by calling `DomainRepairFlow` directly,
so issue registration, platform discovery (`repairs.py` being found at all)
and the manager's own "delete the issue unless the flow aborts" rule are all
part of what these tests cover. The Radoff API is mocked at the transport
level exactly as everywhere else in this suite.

Card M-07 adds the second way in. The tests above the divider drive the
repair RT-2926 wrote it for - an entry that has no domain - and the ones
below drive the 403 one: an entry that has a domain the account has lost
access to, which used to leave "remove the integration and add it again" as
the only remedy.
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
    CONF_BASE_URL,
    CONF_DOMAIN_PREFIX,
    CONF_INDEX,
    DOMAIN,
    ISSUE_DOMAIN_ACCESS_DENIED,
    ISSUE_MISSING_DOMAIN_PREFIX,
)

from .conftest import (
    BASE_URL,
    auth_result,
    load_dev_fixture,
    load_fixture,
    make_id_token,
    patch_authenticate_user,
    register_domains,
    register_devices,
    register_measures_ranges,
)

DOMAIN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
DOMAIN_PREFIX = "home1234"
OTHER_DOMAIN_PREFIX = "office56"


async def _setup_broken_entry(
    hass: HomeAssistant,
    config_entry_v1_data: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Add a real-shaped v1 entry (no domain at all) and let its setup fail."""
    await async_setup_component(hass, "repairs", {})

    entry = MockConfigEntry(
        domain=DOMAIN, version=1, data=config_entry_v1_data, options=options or {}
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return entry


async def _start_fix_flow(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    """Open the fix flow for this entry's issue, as the frontend would."""
    issue_id = f"{ISSUE_MISSING_DOMAIN_PREFIX}_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    flow_manager = hass.data["repairs"]["flow_manager"]
    return await flow_manager.async_init(DOMAIN, data={"issue_id": issue_id})


async def _configure(hass: HomeAssistant, flow_id: str, user_input: Any) -> Any:
    return await hass.data["repairs"]["flow_manager"].async_configure(
        flow_id, user_input
    )


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_MISSING_DOMAIN_PREFIX}_{entry.entry_id}"
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
    created by the released version, with no domain, ends up loaded without
    the user ever retyping a credential.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_devices(requests_mock, load_fixture("devices_empty.json"))

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await _configure(hass, result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN_PREFIX] == DOMAIN_PREFIX
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
        monkeypatch, result=auth_result(make_id_token([DOMAIN_ID, OTHER_DOMAIN_PREFIX]))
    )
    register_domains(requests_mock, load_fixture("domains_multi.json"))
    register_devices(requests_mock, load_fixture("devices_empty.json"))

    entry = await _setup_broken_entry(hass, config_entry_v1_data)

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "domain"

    result = await _configure(
        hass, result["flow_id"], {CONF_DOMAIN_PREFIX: OTHER_DOMAIN_PREFIX}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN_PREFIX] == OTHER_DOMAIN_PREFIX
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is None


async def test_repair_keeps_generate_index_option_from_the_migration(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """The repair only writes the domain: what the migration moved stays put."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_domains(requests_mock, load_fixture("domains_single.json"))
    register_devices(requests_mock, load_fixture("devices_empty.json"))

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
    assert CONF_DOMAIN_PREFIX not in entry.data
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
    requests_mock.get(f"{BASE_URL}/data/user/me/domains", status_code=500, json={})

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
    issue_id = f"{ISSUE_MISSING_DOMAIN_PREFIX}_{entry.entry_id}"

    flow_manager = hass.data["repairs"]["flow_manager"]
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    result = await flow_manager.async_init(DOMAIN, data={"issue_id": issue_id})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_found"


async def test_repair_discovers_on_the_environment_the_entry_points_at(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v1_data: dict[str, Any],
) -> None:
    """
    Card M-02: the repair looks the domains up on this entry's own base URL.

    An entry carrying the advanced `base_url` option polls that
    environment, so discovering its domains against the default one would
    be checking a different account universe - a domain that exists on dev
    and not on the entry's environment would be written onto it and fail on
    the very next poll. Nothing is registered on the default host here, so a
    repair that ignored the option would abort instead of passing.
    """
    other_host = "https://api.int.iot.radoff.life"
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    requests_mock.get(
        f"{other_host}/data/user/me/domains", json=load_fixture("domains_single.json")
    )
    register_devices(
        requests_mock, load_fixture("devices_empty.json"), base_url=other_host
    )

    entry = await _setup_broken_entry(
        hass, config_entry_v1_data, options={CONF_BASE_URL: other_host}
    )

    result = await _start_fix_flow(hass, entry)
    result = await _configure(hass, result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN_PREFIX] == DOMAIN_PREFIX
    assert entry.state is ConfigEntryState.LOADED
    assert {request.netloc for request in requests_mock.request_history} == {
        "api.int.iot.radoff.life"
    }


# ---------------------------------------------------------------------------
# Card M-07: the 403 repair - an entry whose domain the account has lost
# ---------------------------------------------------------------------------


def _access_denied_issue(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_DOMAIN_ACCESS_DENIED}_{entry.entry_id}"
    )


async def _setup_entry_refused_with_403(
    hass: HomeAssistant,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> tuple[MockConfigEntry, dict[str, bool]]:
    """
    Load an entry whose domain the API refuses, and return a switch to stop refusing.

    The switch is what makes this a repair rather than a snapshot: the fix
    flow has to be able to reach a state where the entry works again, and
    the API answering 403 forever would only ever test the abort.
    """
    await async_setup_component(hass, "repairs", {})

    state = {"refused": True}

    def _devices(request: Any, context: Any) -> Any:  # noqa: ARG001
        if state["refused"]:
            context.status_code = 403
            return load_dev_fixture("error__devices_foreign_domain")
        context.status_code = 200
        return load_fixture("devices_empty.json")

    requests_mock.get(f"{BASE_URL}/data/devices", json=_devices)
    register_measures_ranges(requests_mock)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        minor_version=1,
        data=config_entry_v3_data,
        options={CONF_INDEX: True},
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    return entry, state


async def test_a_403_raises_a_fixable_repair_beside_its_setup_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """
    Card M-07: the 403 stops being a dead end.

    M-02 made the refusal legible - a translated `ConfigEntryError` saying
    the domain, not the password, is the problem - and left the user with
    one way to act on it: remove the integration and add it again, which
    throws away every entity's history. The error stays (it is what shows on
    the integration card, where a repair is not visible) and the repair is
    what can actually be acted on.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))

    entry, _ = await _setup_entry_refused_with_403(
        hass, requests_mock, config_entry_v3_data
    )

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "domain_access_denied"

    issue = _access_denied_issue(hass, entry)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": entry.entry_id}
    assert issue.translation_placeholders == {"username": "user@example.com"}


async def test_the_403_repair_reselects_a_domain_and_reloads_the_entry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """
    The point of the whole thing: a new domain, the same entities, the same history.

    The entry starts on `home1234`, which the account can no longer reach,
    and ends on `office56` without anything being removed and re-added -
    which is what preserves the `entity_id`s every dashboard and automation
    refers to.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))
    register_domains(requests_mock, load_fixture("domains_multi.json"))

    entry, state = await _setup_entry_refused_with_403(
        hass, requests_mock, config_entry_v3_data
    )
    assert entry.data[CONF_DOMAIN_PREFIX] == DOMAIN_PREFIX

    issue_id = f"{ISSUE_DOMAIN_ACCESS_DENIED}_{entry.entry_id}"
    flow_manager = hass.data["repairs"]["flow_manager"]
    result = await flow_manager.async_init(DOMAIN, data={"issue_id": issue_id})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"

    result = await _configure(hass, result["flow_id"], {})
    assert result["step_id"] == "domain"

    # The account can reach the new domain, which is the situation the user
    # is repairing their way into.
    state["refused"] = False

    result = await _configure(
        hass, result["flow_id"], {CONF_DOMAIN_PREFIX: OTHER_DOMAIN_PREFIX}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_DOMAIN_PREFIX] == OTHER_DOMAIN_PREFIX
    assert entry.state is ConfigEntryState.LOADED
    assert _access_denied_issue(hass, entry) is None


async def test_a_successful_setup_clears_a_stale_403_repair(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    config_entry_v3_data: dict[str, Any],
) -> None:
    """
    A 403 that stops happening takes its repair with it, without a fix flow.

    Access can come back on the backend's side - the account is put back
    into the domain - and a repair card still sitting there afterwards would
    be pointing at a problem that no longer exists.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))

    entry, state = await _setup_entry_refused_with_403(
        hass, requests_mock, config_entry_v3_data
    )
    assert _access_denied_issue(hass, entry) is not None

    state["refused"] = False
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert _access_denied_issue(hass, entry) is None
