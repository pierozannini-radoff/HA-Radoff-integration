"""Config flow for radoff integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.exceptions import HomeAssistantError

from .api import API
from .const import CONF_DOMAIN_ID, CONF_INDEX, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect, and discover the user's domains."""
    api = API(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
    )

    if not await hass.async_add_executor_job(api.connect):
        raise InvalidAuthError

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


class CannotConnectError(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""
