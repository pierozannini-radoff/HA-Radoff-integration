"""
Reliable, automated tests for card S-08's AC2, AC3 and AC4.

Unlike `verify_s08.py` (which only exercises `api/auth.py`'s Cognito
error-code classification in isolation, with no `homeassistant` dependency
at all), these three tests drive the *real* Home Assistant config-entry and
config-flow machinery via `pytest-homeassistant-custom-component` - the
standard harness for testing HA custom integrations. Nothing here talks to
the real Radoff API: `API.connect`/`get_devices`/`list_domains` are replaced
with a small deterministic `_Backend` (see below) that each test drives
explicitly, so the outcome depends only on this integration's own code, not
on network conditions or a real Radoff account.

Run all three:
    pytest tests/test_reauth.py -v

Run one AC at a time:
    pytest tests/test_reauth.py::test_ac2_reauth_success_no_duplicate_no_history_loss -v
    pytest tests/test_reauth.py::test_ac3_reauth_wrong_password_shows_invalid_auth -v
    pytest tests/test_reauth.py::test_ac4_network_error_does_not_trigger_reauth -v

AC1 and AC5 are NOT covered here (see the conversation/`s-08-implementazione.md`
for why): they depend on the real Radoff backend's session-invalidation
timing, which nothing running in this test process can stand in for
credibly - they need a real account and a real password change.

Verified against pytest-homeassistant-custom-component 0.13.205 /
Home Assistant 2025.1.4 on Python 3.12 (this sandbox's Python 3.11 install
can't run pytest-homeassistant-custom-component - it requires 3.12+ - and
the repo's own dev container pins Home Assistant 2024.6.0 specifically: the
APIs exercised here - SOURCE_REAUTH, ConfigEntryState, FlowResultType,
async_progress_by_handler(match_context=...), entity registry lookups - are
all long-stable, but running this same file inside the real dev container
is still the authoritative check against the exact pinned version).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.radoff.api.auth import AuthInvalidError
from custom_components.radoff.api.models import RadoffDevice, Reading
from custom_components.radoff.const import CONF_DOMAIN_ID, CONF_INDEX, DOMAIN

if TYPE_CHECKING:
    import requests
    from homeassistant.core import HomeAssistant

UNIQUE_ID = "user@example.com"
DEVICE_ID = "device-1"
DEVICE_SERIAL = "SERIAL-1"
READING_KEY = "internal_temperature"
SENSOR_UNIQUE_ID = f"{DOMAIN}-{DEVICE_ID}-{READING_KEY}"


class _Backend:
    """
    A deterministic stand-in for the real Radoff cloud API.

    Each test configures the handful of knobs below and installs it in place
    of `custom_components.radoff.api.client.API`'s three network-calling
    methods (see `_install_backend`). No test in this file makes a real HTTP
    or Cognito call.
    """

    def __init__(self) -> None:
        self.wrong_passwords: set[str] = set()
        self.devices: list[RadoffDevice] = []
        self.get_devices_raises: Exception | None = None

    def connect(self, api_self) -> bool:  # noqa: ANN001
        """Stand-in for `API.connect`."""
        if api_self.password in self.wrong_passwords:
            msg = "synthetic: Cognito rejected this password"
            raise AuthInvalidError(msg)
        api_self.connected = True
        api_self.tokens = {"IdToken": "fake-id-token", "ExpiresIn": 3600}
        api_self._token_expires_at = time.time() + 3600  # noqa: SLF001
        return True

    def get_devices(self, api_self) -> list[RadoffDevice]:  # noqa: ANN001, ARG002
        """Stand-in for `API.get_devices`."""
        if self.get_devices_raises is not None:
            raise self.get_devices_raises
        return self.devices

    def list_domains(
        self,
        api_self,  # noqa: ANN001, ARG002
        bearer_token: str | None = None,  # noqa: ARG002
    ) -> list[dict]:
        """Stand-in for `API.list_domains` (called by `validate_input`)."""
        return [{"id": "dom-1", "name": "Test domain"}]


def _install_backend(backend: _Backend):
    """
    Return a context manager patching `API`'s network methods with `backend`.

    Plain top-level functions (not bound methods of `backend`) so the normal
    function-descriptor protocol still applies when Home Assistant calls
    `some_api_instance.connect()` etc. - `self` inside each closure below is
    the real `API` instance, exactly like the unpatched code.
    """

    def connect(self):  # noqa: ANN001, ANN202
        return backend.connect(self)

    def get_devices(self):  # noqa: ANN001, ANN202
        return backend.get_devices(self)

    def list_domains(self, bearer_token=None):  # noqa: ANN001, ANN202
        return backend.list_domains(self, bearer_token)

    return patch.multiple(
        "custom_components.radoff.api.client.API",
        connect=connect,
        get_devices=get_devices,
        list_domains=list_domains,
    )


def _make_device() -> RadoffDevice:
    """Build one canned device with one reading, enough to produce an entity."""
    return RadoffDevice(
        device_id=DEVICE_ID,
        device_serial=DEVICE_SERIAL,
        device_type="Now+",
        name="Test Now+",
        readings={
            READING_KEY: Reading(
                name=READING_KEY,
                value=2500,
                device_class=SensorDeviceClass.TEMPERATURE,
                friendly_name="Temperature",
                unit="°C",
                normalize_fn=lambda value: round(float(value) * 0.00835, 1),
            ),
        },
    )


def _make_entry(hass: HomeAssistant, *, password: str = "correct-password") -> MockConfigEntry:
    """Add (but do not set up) a Radoff config entry to `hass`."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Radoff",
        data={
            CONF_USERNAME: UNIQUE_ID,
            CONF_PASSWORD: password,
            CONF_DOMAIN_ID: "dom-1",
        },
        options={CONF_INDEX: True},
        unique_id=UNIQUE_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def test_ac2_reauth_success_no_duplicate_no_history_loss(
    hass: HomeAssistant,
) -> None:
    """
    AC2: a correct new password on the re-auth form reconnects in place.

    No new config entry is created (no duplicate) and the sensor entity that
    existed before the auth failure keeps the exact same `entity_id`
    afterwards (proxy for "no history lost": Home Assistant's long-term
    statistics and history are keyed by entity_id, and nothing here ever
    creates a second entity or a second device for the same reading).
    """
    backend = _Backend()
    backend.devices = [_make_device()]

    with _install_backend(backend):
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        entity_registry = er.async_get(hass)
        entity_id_before = entity_registry.async_get_entity_id(
            "sensor", DOMAIN, SENSOR_UNIQUE_ID
        )
        assert entity_id_before is not None, "setup should have created the sensor"

        # The stored password is no longer accepted by Cognito (card S-08's
        # "credenziali non più valide" case) - simulate the session having
        # already dropped (as a real 401 would leave it, see
        # api/client.py::_check_response_status) so the next refresh goes
        # straight through API.connect().
        backend.wrong_passwords.add(entry.data[CONF_PASSWORD])
        coordinator = hass.data[DOMAIN][entry.entry_id].coordinator
        coordinator.api.connected = False

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        reauth_flows = hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        )
        assert len(reauth_flows) == 1, "ConfigEntryAuthFailed should start one reauth flow"
        flow_id = reauth_flows[0]["flow_id"]

        # The user enters a new, correct password.
        backend.wrong_passwords.clear()
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_PASSWORD: "new-correct-password"}
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"

        # No duplicate entry.
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        assert entry.data[CONF_PASSWORD] == "new-correct-password"
        assert entry.state is ConfigEntryState.LOADED

        # Same entity, same entity_id: nothing was removed and re-added.
        entity_id_after = entity_registry.async_get_entity_id(
            "sensor", DOMAIN, SENSOR_UNIQUE_ID
        )
        assert entity_id_after == entity_id_before


async def test_ac3_reauth_wrong_password_shows_invalid_auth(
    hass: HomeAssistant,
) -> None:
    """AC3: a wrong password on the re-auth form shows invalid_auth and stays open."""
    backend = _Backend()

    with _install_backend(backend):
        entry = _make_entry(hass)
        # The reauth flow only reads `reauth_entry.data` - it does not
        # require the entry to have gone through a real async_setup, so this
        # test can isolate the form-validation behaviour without also
        # standing up a working coordinator.
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": SOURCE_REAUTH,
                "entry_id": entry.entry_id,
                "unique_id": entry.unique_id,
            },
            data=entry.data,
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        backend.wrong_passwords.add("totally-wrong-password")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "totally-wrong-password"}
        )

        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": "invalid_auth"}

        # The entry's password must NOT have been touched by the failed attempt.
        assert entry.data[CONF_PASSWORD] == "correct-password"


async def test_ac4_network_error_does_not_trigger_reauth(hass: HomeAssistant) -> None:
    """AC4: a network error during polling never opens the re-auth flow."""
    import requests  # noqa: PLC0415 - kept local, only this test needs it

    backend = _Backend()
    backend.devices = [_make_device()]

    with _install_backend(backend):
        entry = _make_entry(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        coordinator = hass.data[DOMAIN][entry.entry_id].coordinator
        backend.get_devices_raises = requests.exceptions.ConnectionError(
            "synthetic network failure"
        )

        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.last_update_success is False
        assert entry.state is ConfigEntryState.LOADED, (
            "a network error must not tear the entry down, only fail the poll"
        )

        reauth_flows = hass.config_entries.flow.async_progress_by_handler(
            DOMAIN,
            match_context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id},
        )
        assert reauth_flows == [], "a network error must NOT open the re-auth flow"
