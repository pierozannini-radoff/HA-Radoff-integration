"""
The two Repairs issues that carry a user into the domain re-selection flow.

Both say the same thing to `repairs.py` - "this entry does not know which
Radoff domain to poll, and only a human can settle it" - and differ in the
sentence they show and in who raises them:

- `ISSUE_MISSING_DOMAIN_PREFIX` (card RT-2926, widened by M-07) is raised at
  setup, by `__init__.py::async_setup_entry`, for an entry that has no
  usable `domain_prefix` at all: one created by the released version, which
  predates domain discovery entirely, or one created in QA against arch 1.x,
  whose `domain_id` UUID arch 2.0 cannot use and nothing can translate
  offline.
- `ISSUE_DOMAIN_ACCESS_DENIED` (card M-07) is raised during a poll, by
  `coordinator.py`, when the API answers 403: the entry *has* a prefix and
  the account no longer belongs to it.

This module exists so that both `__init__.py` and `coordinator.py` can raise
them. The helpers used to live in `__init__.py`, which imports
`coordinator.py`, so the coordinator could not import them back.

Both ids are scoped per config entry, not per integration: `manifest.json`
declares `single_config_entry: false`, so two Radoff accounts can be
configured side by side and each needs its own repair - resolving the domain
of one says nothing about the other. The entry id also travels in the
issue's `data`, which is what `repairs.py::async_create_fix_flow` reads, so
the id itself stays an opaque string there.
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
    """
    Return the placeholders every domain repair card is rendered with.

    The account's username, not `config_entry.title`: every Radoff entry is
    titled "Radoff" (see `config_flow.py`), so two configured accounts would
    otherwise produce two repair cards with the same words on them and
    nothing to tell which is which - seen while checking the rendered
    strings against the two-entry verification instance (card RT-2926). The
    re-auth step already identifies an entry the same way
    (`reauth_confirm`'s `{username}`).
    """
    return {"username": config_entry.data.get(CONF_USERNAME, config_entry.title)}


@callback
def async_create_missing_domain_prefix_issue(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """
    Raise the fixable repair for an entry with no usable domain (RT-2926, M-07).

    `is_persistent=False`: the issue is re-created by `async_setup_entry` on
    every start for as long as the entry actually needs it, so there is no
    value in keeping a stale copy across restarts.
    """
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
    """
    Raise the fixable repair for an entry the API answers 403 on (card M-07).

    Raised from the poll path, next to the `ConfigEntryError` that stops the
    entry (`coordinator.py`). The two are complementary rather than
    redundant: the error is what shows on the integration card, where a
    Repairs issue is not visible, and the issue is what can actually do
    something about it - before this card the only remedy was to remove the
    integration and add it again, losing every entity's history in the
    process.
    """
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
    """
    Clear both domain repairs for an entry that no longer needs them.

    Deleting an issue that was never created is a no-op, so this is called
    unconditionally rather than after working out which of the two - if any
    - is currently open. Either one is stale the moment the entry has a
    domain it can actually poll.
    """
    for issue in (ISSUE_MISSING_DOMAIN_PREFIX, ISSUE_DOMAIN_ACCESS_DENIED):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(issue, config_entry))
