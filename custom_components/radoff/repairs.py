"""
Repairs fix flow for a config entry that cannot say which domain to poll.

A repair rather than part of the migration, which runs at startup and cannot
ask anything of anyone. The config flow's own validation is reused here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import CONF_USERNAME

from .config_flow import (
    CannotConnectError,
    InvalidAuthError,
    UnsupportedChallengeError,
    domain_choices,
    validate_input,
)
from .const import CONF_BASE_URL, CONF_DOMAIN_PREFIX, DEFAULT_BASE_URL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)

# The outcomes the config flow's validation classifies, mapped to this flow's
# own abort reasons: same situations, worded for a repair not a setup form.
_ABORT_REASONS = {
    UnsupportedChallengeError: "unsupported_challenge",
    InvalidAuthError: "invalid_auth",
    CannotConnectError: "cannot_connect",
}


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,  # noqa: ARG001
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """
    Build the fix flow for either of this integration's two domain issues.

    The entry id travels in the issue's `data` rather than being parsed back
    out of `issue_id`, so one flow serves both issues: they differ in what
    the user read, not in what has to happen next. The entry may be gone by
    the time the repair is opened, which aborts rather than raises.
    """
    entry_id = (data or {}).get("entry_id")
    entry = (
        hass.config_entries.async_get_entry(str(entry_id))
        if entry_id is not None
        else None
    )
    return DomainRepairFlow(entry)


class DomainRepairFlow(RepairsFlow):
    """Discover - and if ambiguous, ask for - the `domain_prefix` of one entry."""

    def __init__(self, config_entry: ConfigEntry | None) -> None:
        """Store the entry to repair (`None` if it no longer exists)."""
        self._config_entry = config_entry
        self._domain_choices: dict[str, str] = {}

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002
    ) -> FlowResult:
        """Handle the first step of the fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Explain what is about to happen, then run discovery on confirmation."""
        if self._config_entry is None:
            return self.async_abort(reason="entry_not_found")

        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "username": self._config_entry.data.get(
                        CONF_USERNAME, self._config_entry.title
                    )
                },
            )

        return await self._async_discover()

    async def _async_discover(self) -> FlowResult:
        """
        Look the account's domains up and either apply one or ask which.

        Every abort here leaves the Repairs issue in place (the flow manager
        only clears it for a flow that ends in anything but an abort), which
        is what we want: none of these outcomes leaves the entry usable, so
        the user must keep a way back to this repair once they have dealt
        with the cause.
        """
        try:
            info = await validate_input(
                self.hass,
                dict(self._config_entry.data),
                # The environment this entry actually polls, not the default.
                self._config_entry.options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
            )
        except (
            UnsupportedChallengeError,
            InvalidAuthError,
            CannotConnectError,
        ) as err:
            return self.async_abort(reason=_ABORT_REASONS[type(err)])
        except Exception:
            _LOGGER.exception("Unexpected exception while repairing the domain")
            return self.async_abort(reason="unknown")

        choices = domain_choices(info["domains"])

        if not choices:
            return self.async_abort(reason="no_domains")

        if len(choices) == 1:
            return self._apply(next(iter(choices)))

        self._domain_choices = choices
        return await self.async_step_domain()

    async def async_step_domain(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Ask which domain to use, for accounts that can reach more than one."""
        if user_input is not None:
            return self._apply(user_input[CONF_DOMAIN_PREFIX])

        return self.async_show_form(
            step_id="domain",
            data_schema=vol.Schema(
                {vol.Required(CONF_DOMAIN_PREFIX): vol.In(self._domain_choices)}
            ),
        )

    def _apply(self, domain_prefix: str) -> FlowResult:
        """Write the resolved `domain_prefix` onto the entry and reload it."""
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data={**self._config_entry.data, CONF_DOMAIN_PREFIX: domain_prefix},
        )
        self.hass.config_entries.async_schedule_reload(self._config_entry.entry_id)

        _LOGGER.debug(
            "Repaired config entry %s with %s=%s",
            self._config_entry.entry_id,
            CONF_DOMAIN_PREFIX,
            domain_prefix,
        )

        return self.async_create_entry(data={})
