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
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    API,
    APIConnectionError,
    AuthChallengeRequiredError,
    AuthInvalidError,
    AuthUnavailableError,
)
from .const import (
    CONF_BASE_URL,
    CONF_DOMAIN_PREFIX,
    CONF_INDEX,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_BASE_URL,
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


async def validate_input(
    hass: HomeAssistant,
    data: dict[str, Any],
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """
    Validate the user input allows us to connect, and discover the user's domains.

    `base_url` is the environment to validate against: a caller with an entry
    passes that entry's own, since checking credentials against a different
    environment from the one it polls would be checking the wrong thing.

    Every outcome this module can interpret becomes a distinct local error -
    `UnsupportedChallengeError`, `InvalidAuthError`, `CannotConnectError` -
    so none of them reaches the callers' generic clause as "unknown".
    """
    api = API(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        base_url=base_url,
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

    return {
        "title": "Radoff",
        "username": data[CONF_USERNAME],
        "domains": domains,
        # The already-authenticated client, so a caller needing one more
        # request reuses this session instead of logging in a second time.
        "api": api,
    }


def domain_choices(domains: list[dict[str, Any]]) -> dict[str, str]:
    """
    Return `{domain_prefix: label}` for the domains of a `list_domains()` response.

    Each element wraps the domain next to the caller's role in it. The value
    persisted is `prefix`, what every domain-scoped call takes as a query
    parameter; the label is `name`, since most prefixes are UUID fragments a
    user would be guessing between, falling back to the prefix.

    An element without a prefix is skipped rather than raising: one
    malformed entry is no reason to refuse the rest of the list.
    """
    choices: dict[str, str] = {}

    for element in domains:
        domain = element.get("domain") or {}
        prefix = domain.get("prefix")
        if not prefix:
            # Its keys, not the element: what it holds is the API's to
            # decide, and a domain this integration cannot use is exactly the
            # one whose contents are unknown.
            _LOGGER.debug(
                "Skipping a domain with no prefix, carrying the keys %s",
                sorted(element),
            )
            continue
        choices[prefix] = domain.get("name") or prefix

    return choices


class ConfigPatternFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for radoff."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._username: str = ""
        self._api: API | None = None
        self._domain_choices: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RadoffOptionsFlow:
        """Create the options flow."""
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
                # Never create an entry for an account Cognito is asking a
                # challenge for: it could never authenticate.
                return self.async_abort(reason="unsupported_challenge")
            except CannotConnectError:
                errors["base"] = "cannot_connect"
            except InvalidAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                choices = domain_choices(info["domains"])

                if not choices:
                    return self.async_abort(reason="no_domains")

                self._user_input = user_input
                self._username = info["username"]
                self._api = info["api"]

                if len(choices) == 1:
                    # One domain is an answer, not a question: the step is
                    # skipped rather than shown pre-selected.
                    return await self._async_create_entry(next(iter(choices)))

                self._domain_choices = choices
                return await self.async_step_domain()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_domain(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle domain selection for accounts with access to more than one domain."""
        if user_input is not None:
            return await self._async_create_entry(user_input[CONF_DOMAIN_PREFIX])

        return self.async_show_form(
            step_id="domain",
            data_schema=vol.Schema(
                {vol.Required(CONF_DOMAIN_PREFIX): vol.In(self._domain_choices)}
            ),
        )

    async def _async_has_devices(self, domain_prefix: str) -> bool:
        """
        Answer whether the chosen domain holds any device at all.

        A domain with no device sets up cleanly and then shows nothing, with
        no explanation anywhere. Only an empty answer counts: any failure
        returns `True`, because the check then has no opinion and the first
        refresh handles a broken backend far better than this form can.
        """
        if self._api is None:
            return True

        self._api.domain_prefix = domain_prefix

        try:
            devices = await self.hass.async_add_executor_job(self._api.get_devices)
        except Exception:  # noqa: BLE001 - see the docstring: no failure of
            # this optional check may block a setup, so every one of them is
            # caught and answered the same way.
            _LOGGER.debug(
                "Could not check whether domain %s has devices; "
                "continuing with the setup",
                domain_prefix,
                exc_info=True,
            )
            return True

        return bool(devices)

    async def _async_create_entry(self, domain_prefix: str) -> ConfigFlowResult:
        """Persist the config entry with the chosen domain_prefix."""
        if not await self._async_has_devices(domain_prefix):
            return self.async_abort(reason="no_devices")

        # Normalized only as the identity key. What is persisted and
        # authenticated with stays as typed: Cognito may be case-sensitive.
        await self.async_set_unique_id(self._username.strip().lower())
        self._abort_if_unique_id_configured()

        data = {**self._user_input, CONF_DOMAIN_PREFIX: domain_prefix}
        return self.async_create_entry(
            title="Radoff", data=data, options={CONF_INDEX: True}
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002
    ) -> ConfigFlowResult:
        """Handle re-authentication triggered by `ConfigEntryAuthFailed`."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """
        Ask for the new password and validate it.

        A single-field form: the username is shown but not editable, since
        changing the account is a reconfigure. `unique_id` is never touched,
        so re-auth cannot create a duplicate entry - the password is updated
        on the same entry and the flow aborts as successful, leaving the
        entity history intact. The reload comes from the entry's own update
        listener.
        """
        errors: dict[str, str] = {}

        reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        username = reauth_entry.data[CONF_USERNAME]

        if user_input is not None:
            data = {**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await validate_input(
                    self.hass,
                    data,
                    reauth_entry.options.get(CONF_BASE_URL, DEFAULT_BASE_URL),
                )
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
    Handle an options flow for radoff.

    The poll interval and the index entities, plus an advanced third field
    for the API base URL: the environment is the host, so that one value is
    what moves an installation between environments without a code change.
    Its bounds come from the constants, so moving those moves the form.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Store the config entry this flow edits the options of."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and process the single options form."""
        if user_input is not None:
            # Voluptuous re-validates server-side whatever the frontend
            # sent, so a value below the floor never reaches here.
            return self.async_create_entry(data=self._merged_options(user_input))

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

        if self.show_advanced_options:
            # With advanced mode off the field is not in the schema at all,
            # so a normal user's form is unchanged by its existence.
            options_schema = options_schema.extend(
                {
                    vol.Optional(
                        CONF_BASE_URL,
                        default=self.config_entry.options.get(
                            CONF_BASE_URL, DEFAULT_BASE_URL
                        ),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
                }
            )

        return self.async_show_form(step_id="init", data_schema=options_schema)

    def _merged_options(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """
        Return the options to save, merging `user_input` onto the stored ones.

        A merge, not a replacement: with advanced mode off the base URL is
        not in `user_input`, and saving that dict as-is would drop an
        override someone set. A URL equal to the default, or blank, is
        removed rather than stored, so an entry that never overrode it keeps
        following the constant when that moves.
        """
        merged = {**self.config_entry.options, **user_input}

        base_url = str(merged.get(CONF_BASE_URL, "")).strip().rstrip("/")
        if not base_url or base_url == DEFAULT_BASE_URL:
            merged.pop(CONF_BASE_URL, None)
        else:
            merged[CONF_BASE_URL] = base_url

        return merged


class CannotConnectError(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuthError(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class UnsupportedChallengeError(HomeAssistantError):
    """Cognito requires a challenge this flow cannot complete."""

    def __init__(self, challenge_name: str) -> None:
        """Store the Cognito challenge name that triggered this abort."""
        super().__init__(challenge_name)
        self.challenge_name = challenge_name
