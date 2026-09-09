"""
Repairs fix flow for config entries created before `domain_id` existed.

Card RT-2926, finding T-06/F1. `domain_id` was introduced by the
multi-domain discovery of S-01/RT-2803, in the same milestone that bumped
the config entry to VERSION 2: an entry created by the released version
(30e0cde) cannot contain it, so after the update every existing
installation would fail setup - originally with a bare `KeyError`, now with
an explicit `ConfigEntryError` (see `__init__.py::async_setup_entry`) and
the fixable issue this module resolves.

Why a repair rather than doing it inside `async_migrate_entry`: a migration
runs during Home Assistant startup, must not block it on network I/O, and -
decisively - has no way to ask anything of anyone. An account with access to
more than one domain has no correct answer that this code could pick on the
user's behalf (that is the very reason the config flow grew its own `domain`
step in S-01). The repair moves both the discovery call and the choice to a
moment where the user is present.

Nothing here is a new authentication path: `config_flow.py::validate_input`
is reused verbatim, so every Cognito outcome is classified exactly as it is
during setup and re-auth (S-09), and the credentials used are the ones
already persisted on the entry - the user is never asked to retype them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow

from .config_flow import (
    CannotConnectError,
    InvalidAuthError,
    UnsupportedChallengeError,
    validate_input,
)
from .const import CONF_DOMAIN_ID

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)

# The three outcomes `config_flow.py::validate_input` already classifies
# (S-09), mapped to this flow's own abort reasons - same texts the config
# flow shows, reworded for someone standing in front of a repair rather
# than in front of the setup form (see `strings.json`, issues section).
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
    Build the fix flow for one `missing_domain_id` issue.

    The entry id travels in the issue's own `data` (see
    `__init__.py::_async_create_missing_domain_issue`) rather than being
    parsed back out of `issue_id`, so the issue id stays an opaque string.
    The entry may legitimately be gone by the time the user clicks the
    repair (they removed and re-added the integration instead); that case
    aborts with its own reason instead of raising.
    """
    entry_id = (data or {}).get("entry_id")
    entry = (
        hass.config_entries.async_get_entry(str(entry_id))
        if entry_id is not None
        else None
    )
    return MissingDomainIdRepairFlow(entry)


class MissingDomainIdRepairFlow(RepairsFlow):
    """Discover - and if ambiguous, ask for - the `domain_id` of one entry."""

    def __init__(self, config_entry: ConfigEntry | None) -> None:
        """Store the entry to repair (`None` if it no longer exists)."""
        self._config_entry = config_entry
        self._domains: list[dict[str, Any]] = []

    async def async_step_init(
        self,
        user_input: dict[str, str] | None = None,  # noqa: ARG002
    ) -> FlowResult:
        """Handle the first step of the fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """
        Explain what is about to happen, then run discovery on confirmation.

        Deliberately a confirmation form with no fields: the credentials are
        already on the entry, and the only thing this step needs from the
        user is the go-ahead to contact the Radoff API on their behalf - the
        repair must not fire a login the moment the issue is opened.
        """
        if self._config_entry is None:
            return self.async_abort(reason="entry_not_found")

        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"title": self._config_entry.title},
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
            info = await validate_input(self.hass, dict(self._config_entry.data))
        except (
            UnsupportedChallengeError,
            InvalidAuthError,
            CannotConnectError,
        ) as err:
            return self.async_abort(reason=_ABORT_REASONS[type(err)])
        except Exception:
            _LOGGER.exception("Unexpected exception while repairing the domain")
            return self.async_abort(reason="unknown")

        domains: list[dict[str, Any]] = info["domains"]

        if not domains:
            return self.async_abort(reason="no_domains")

        if len(domains) == 1:
            return self._apply(domains[0]["id"])

        self._domains = domains
        return await self.async_step_domain()

    async def async_step_domain(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Ask which domain to use, for accounts that can reach more than one."""
        if user_input is not None:
            return self._apply(user_input[CONF_DOMAIN_ID])

        domain_options = {
            domain["id"]: domain.get("name") or domain["id"] for domain in self._domains
        }

        return self.async_show_form(
            step_id="domain",
            data_schema=vol.Schema(
                {vol.Required(CONF_DOMAIN_ID): vol.In(domain_options)}
            ),
        )

    def _apply(self, domain_id: str) -> FlowResult:
        """
        Write the resolved `domain_id` onto the entry and reload it.

        The reload is scheduled explicitly rather than left to the update
        listener `__init__.py` registers on every entry: that listener is
        attached during a *successful* setup, and this entry has none - its
        setup is precisely what failed. `async_schedule_reload` also keeps
        the reload off this flow's own await path, so the repair dialog
        closes immediately instead of waiting for the first poll.

        No issue deletion here: the repairs flow manager removes the issue
        itself for any flow that ends in anything other than an abort. If
        the reload fails again for some other reason, `async_setup_entry`
        raises a fresh one.
        """
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            data={**self._config_entry.data, CONF_DOMAIN_ID: domain_id},
        )
        self.hass.config_entries.async_schedule_reload(self._config_entry.entry_id)

        _LOGGER.debug(
            "Repaired config entry %s with %s=%s",
            self._config_entry.entry_id,
            CONF_DOMAIN_ID,
            domain_id,
        )

        return self.async_create_entry(data={})
