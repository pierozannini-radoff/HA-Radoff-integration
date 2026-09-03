"""Config flow for radoff integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError

from .api import API, AuthInvalidError
from .const import CONF_DOMAIN_ID, CONF_INDEX, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_REAUTH_CONFIRM_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect, and discover the user's domains."""
    api = API(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
    )

    try:
        await hass.async_add_executor_job(api.connect)
    except AuthInvalidError as err:
        # Card S-08: same "wrong/definitively-rejected credentials" signal
        # used by the coordinator's re-auth path, translated here to the
        # config-flow-local error this module's callers already know how to
        # show (`errors["base"] = "invalid_auth"`), on both the initial setup
        # form (async_step_user) and the re-auth form (async_step_reauth_confirm)
        # below, since both call this same function.
        raise InvalidAuthError from err

    if not api.connected:
        raise InvalidAuthError

    domains = await hass.async_add_executor_job(api.list_domains)

    return {"title": "Radoff", "username": data[CONF_USERNAME], "domains": domains}


class ConfigPatternFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for radoff."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._username: str = ""
        self._domains: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                domains: list[dict[str, Any]] = info["domains"]

                if not domains:
                    return self.async_abort(reason="no_domains")

                self._user_input = user_input
                self._username = info["username"]

                if len(domains) == 1:
                    return await self._async_create_entry(domains[0]["id"])

                self._domains = domains
                return await self.async_step_domain()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_domain(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle domain selection for accounts with access to more than one domain."""
        if user_input is not None:
            return await self._async_create_entry(user_input[CONF_DOMAIN_ID])

        domain_options = {
            domain["id"]: domain.get("name") or domain["id"] for domain in self._domains
        }

        return self.async_show_form(
            step_id="domain",
            data_schema=vol.Schema(
                {vol.Required(CONF_DOMAIN_ID): vol.In(domain_options)}
            ),
        )

    async def _async_create_entry(self, domain_id: str) -> ConfigFlowResult:
        """Persist the config entry with the chosen domain_id."""
        await self.async_set_unique_id(self._username)
        self._abort_if_unique_id_configured()

        data = {**self._user_input, CONF_DOMAIN_ID: domain_id}
        return self.async_create_entry(
            title="Radoff", data=data, options={CONF_INDEX: True}
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002
    ) -> ConfigFlowResult:
        """
        Handle re-authentication triggered by `ConfigEntryAuthFailed` (card S-08).

        Home Assistant calls this with the failing entry's own `data` as
        `entry_data`, and - crucially - has already put the entry id in
        `self.context["entry_id"]` (set by `ConfigEntry.async_start_reauth`,
        which is what the coordinator calls internally when
        `async_update_data` raises `ConfigEntryAuthFailed`). `entry_data` is
        not used directly: `async_step_reauth_confirm` below re-reads the
        entry fresh from `self.context["entry_id"]` instead, so it always
        shows the username currently on the entry rather than a snapshot
        that could theoretically be stale.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Ask for the new password and validate it (card S-08, steps 3-4).

        Deliberately a single-field form: username is shown (read-only, via
        `description_placeholders`) but not editable here - changing the
        account itself is a reconfigure flow, explicitly out of scope for
        this card. `unique_id` is never touched in this whole flow, so
        re-auth can never create a duplicate entry (card S-08, step 6): we
        only ever update the password on the *same* entry found via
        `self.context["entry_id"]`.

        On success, the entry's `data` is updated in place and
        `async_abort(reason="reauth_successful")` ends the flow - no
        `async_create_entry` call, so no new entry, no lost entity history.
        The reload itself is not triggered explicitly here: it already
        happens through the `update_listener` `__init__.py` registers on
        every config entry (`config_entry.add_update_listener(...)`), which
        fires on any `async_update_entry` call that actually changes the
        entry's data - this avoids depending on `async_update_reload_and_abort`,
        a newer ConfigFlow convenience not guaranteed present on the oldest
        Home Assistant version this integration declares support for
        (`hacs.json`: 2024.6.0).
        """
        errors: dict[str, str] = {}

        reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        username = reauth_entry.data[CONF_USERNAME]

        if user_input is not None:
            data = {**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await validate_input(self.hass, data)
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception during reauth")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_CONFIRM_DATA_SCHEMA,
            description_placeholders={"username": username},
            errors=errors,
        )


class CannotConnectError(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""
