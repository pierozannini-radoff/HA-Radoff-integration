"""Config flow for radoff integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import (
    API,
    APIConnectionError,
    AuthChallengeRequiredError,
    AuthInvalidError,
    AuthUnavailableError,
)
from .const import (
    CONF_DOMAIN_ID,
    CONF_INDEX,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

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
    """
    Validate the user input allows us to connect, and discover the user's domains.

    Card S-09: every outcome of `api.connect()`/`api.list_domains()` that
    this module knows how to interpret is now mapped to a distinct local
    error before it can reach the generic `except Exception` in
    `async_step_user`/`async_step_reauth_confirm` (finding C9 - `unknown`
    was being shown for cases that have a perfectly good, already-translated
    message: wrong credentials, a network/Cognito-availability problem, or
    an unsupported Cognito challenge):

    - `AuthChallengeRequiredError` (api/auth.py): Cognito wants a challenge
      this flow cannot complete (`NEW_PASSWORD_REQUIRED`, MFA, ...) - this is
      the fix for finding C8: previously `api.connect()` returned `True` for
      this case too, so a config entry could be created for an account that
      would never be able to authenticate. Translated to
      `UnsupportedChallengeError`, which both callers below turn into a
      dedicated `async_abort(reason="unsupported_challenge")` - no config
      entry is ever created for this case.
    - `AuthInvalidError` (api/auth.py): credentials rejected outright by
      Cognito, or no authentication data returned at all (see that module's
      docstring). Translated to this module's `InvalidAuthError` ->
      `errors["base"] = "invalid_auth"`.
    - `AuthUnavailableError` (api/auth.py) / `APIConnectionError`
      (api/exceptions.py, raised by `list_domains()` on a non-200 response):
      neither says anything about the credentials themselves. Both translate
      to this module's `CannotConnectError` -> `errors["base"] =
      "cannot_connect"`, which used to be declared but never actually
      raised (finding C9).
    """
    api = API(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
    )

    try:
        await hass.async_add_executor_job(api.connect)
        domains = await hass.async_add_executor_job(api.list_domains)
    except AuthChallengeRequiredError as err:
        raise UnsupportedChallengeError(err.challenge_name) from err
    except AuthInvalidError as err:
        raise InvalidAuthError from err
    except (AuthUnavailableError, APIConnectionError) as err:
        raise CannotConnectError from err

    return {"title": "Radoff", "username": data[CONF_USERNAME], "domains": domains}


class ConfigPatternFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for radoff."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._username: str = ""
        self._domains: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RadoffOptionsFlow:
        """
        Create the options flow (card S-11).

        `config_entry` is passed explicitly to `RadoffOptionsFlow.__init__`
        rather than relying on `OptionsFlow` to set `self.config_entry`
        automatically: that automatic assignment is a newer `ConfigFlow`
        convenience not guaranteed present on the oldest Home Assistant
        version this integration declares support for (`hacs.json`:
        2024.6.0) - same baseline-compatibility reasoning already applied to
        the re-auth flow in card S-08 (see that step's note on
        `async_update_reload_and_abort`).
        """
        return RadoffOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except UnsupportedChallengeError:
                # Card S-09 / finding C8: never create a config entry for an
                # account Cognito is asking a challenge for - abort with a
                # dedicated, actionable reason instead of the generic
                # invalid_auth error a bare exception would previously have
                # produced (or, before that, no rejection at all).
                return self.async_abort(reason="unsupported_challenge")
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
        # Card S-09 / finding C16: normalize before using the username as the
        # entry's unique_id, so "Mario@x" and "mario@x" are recognised as the
        # same account (`already_configured`) instead of producing two
        # duplicate entries. The *unnormalized* username from `self._user_input`
        # is still what gets persisted into `data` and used to authenticate -
        # Cognito usernames are not guaranteed case-insensitive server-side,
        # so only the HA-local identity key is normalized here, not the
        # credential itself.
        await self.async_set_unique_id(self._username.strip().lower())
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

        Card S-09 adds the same `UnsupportedChallengeError` handling used by
        `async_step_user`: a password change that leaves the account on a
        Cognito challenge (e.g. it now requires MFA) must abort cleanly here
        too, rather than falling through to the generic `except Exception`.
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
            except UnsupportedChallengeError:
                return self.async_abort(reason="unsupported_challenge")
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


class RadoffOptionsFlow(OptionsFlow):
    """
    Handle an options flow for radoff (card S-11).

    Two fields, both already consumed by `RadoffCoordinator.__init__`
    (`coordinator.py`) before this flow existed - it read `CONF_SCAN_INTERVAL`
    and `CONF_INDEX` from `config_entry.options` from the start, but nothing
    could ever write either one (finding F6: `MIN_SCAN_INTERVAL` was dead
    code, and disabling index entities required removing and re-adding the
    integration). This flow is the missing write path; no coordinator change
    was needed beyond passing the resolved interval through to `API` for the
    429 message (see `coordinator.py`, `api/client.py`).

    A third field for the staleness multiplier (`DEFAULT_STALE_MULTIPLIER`,
    const.py) was considered, per this card's own "COSA FARE" ("o la sua
    esposizione va valutata, vedi S-07"), and deliberately left out - decided
    with Piero: no acceptance criterion requires it, and it already tracks a
    changed scan_interval automatically via `RadoffCoordinator.stale_after`.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Store the config entry this flow edits the options of."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and process the single options form."""
        if user_input is not None:
            # `NumberSelector` already enforces min/max client- and
            # server-side (see the schema below), and voluptuous re-validates
            # server-side regardless of what the frontend sent - a value
            # below MIN_SCAN_INTERVAL never reaches this point as a saved
            # option (S-11 AC: "Un valore sotto il minimo viene rifiutato dal
            # form.").
            return self.async_create_entry(data=user_input)

        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_generate_index = self.config_entry.options.get(CONF_INDEX, True)

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current_scan_interval
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=10,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Required(
                    CONF_INDEX, default=current_generate_index
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)


class CannotConnectError(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class UnsupportedChallengeError(HomeAssistantError):
    """
    Error to indicate Cognito requires a challenge this flow cannot complete.

    Card S-09 / finding C8. Carries the raw Cognito `challenge_name` (e.g.
    `NEW_PASSWORD_REQUIRED`, `SMS_MFA`) purely for logging/diagnostics - the
    abort reason shown to the user (`unsupported_challenge`, see
    `strings.json`/`translations/*.json`) is deliberately generic and
    actionable rather than naming the specific challenge, since actually
    supporting any of them is out of scope for this card.
    """

    def __init__(self, challenge_name: str) -> None:
        """Store the Cognito challenge name that triggered this abort."""
        super().__init__(challenge_name)
        self.challenge_name = challenge_name
