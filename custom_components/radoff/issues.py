"""
The two Repairs issues that carry a user into the domain re-selection flow.

One is raised at setup for an entry with no usable domain, the other on a 403
during a poll. Both ids are scoped per entry: two accounts sit side by side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_DOMAIN_ACCESS_DENIED, ISSUE_MISSING_DOMAIN_PREFIX

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def _issue_id(issue: str, config_entry: ConfigEntry) -> str:
    """Return the per-entry id of one of this module's issues."""
    return f"{issue}_{config_entry.entry_id}"


def _placeholders(config_entry: ConfigEntry) -> dict[str, str]:
    """Return the placeholders every domain repair is rendered with."""
    return {"username": config_entry.data.get(CONF_USERNAME, config_entry.title)}


@callback
def async_create_missing_domain_prefix_issue(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Raise the fixable repair for an entry with no usable domain."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_MISSING_DOMAIN_PREFIX, config_entry),
        data={"entry_id": config_entry.entry_id},
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_MISSING_DOMAIN_PREFIX,
        translation_placeholders=_placeholders(config_entry),
    )


@callback
def async_create_domain_access_denied_issue(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Raise the fixable repair for an entry the API answers 403 on."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(ISSUE_DOMAIN_ACCESS_DENIED, config_entry),
        data={"entry_id": config_entry.entry_id},
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_DOMAIN_ACCESS_DENIED,
        translation_placeholders=_placeholders(config_entry),
    )


@callback
def async_delete_domain_issues(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Clear both domain repairs for an entry that no longer needs them."""
    for issue in (ISSUE_MISSING_DOMAIN_PREFIX, ISSUE_DOMAIN_ACCESS_DENIED):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(issue, config_entry))
