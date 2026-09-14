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

    `base_url` (card M-02) is the environment to validate against. It
    defaults to `DEFAULT_BASE_URL`, which is the right answer during initial
    setup - there is no entry yet, so there is no override to read. The two
    callers that *do* have an entry (the re-auth step below and the Repairs
    fix flow, `repairs.py`) pass that entry's `CONF_BASE_URL` option
    instead: validating credentials and discovering domains against a
    different environment from the one the entry actually polls would be
    checking the wrong thing.

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
        # Card M-07: the already-authenticated client, so a caller that
        # needs one more call (the "has this domain any device?" check in
        # `async_step_user`) can reuse this session instead of logging in
        # to Cognito a second time.
        "api": api,
    }


def domain_choices(domains: list[dict[str, Any]]) -> dict[str, str]:
    """
    Return `{domain_prefix: label}` for the domains of a `list_domains()` response.

    Card M-07, and the whole of what this card had to add to the discovery
    M-02 already moved onto the right path. The arch 2.0 response is not a
    list of flat domain objects the way arch 1.x's was: each element wraps
    the domain next to the caller's role in it -
    `{"domain": {"prefix", "name", "type", "parent_domain_prefix"}, "role":
    {...}}` (M-01, V6; `tests/fixtures/dev/auth_domains__pool_dev.json`).

    The value persisted on the entry is `prefix`, because that is what every
    domain-scoped call takes as a query parameter (T-02 D-03). The *label*
    is `name`, with the prefix as a fallback, because on dev 14 prefixes out
    of 15 are UUID fragments while the name is a readable string (M-01,
    reserve 20): a user choosing between `875fe89b` and `fdffb4d9` is not
    choosing, they are guessing. A domain with no name at all falls back to
    the prefix - unhelpful, but honest, and better than an empty line.

    An element without a prefix is skipped rather than raising: the prefix
    is the only part this integration cannot work without, and a single
    malformed entry in a list of fifteen is not a reason to refuse the other
    fourteen. Nothing else here is validated; the API is the authority on
    what a domain looks like.
    """
    choices: dict[str, str] = {}

    for element in domains:
        domain = element.get("domain") or {}
        prefix = domain.get("prefix")
        if not prefix:
            _LOGGER.debug("Skipping a domain with no prefix: %s", element)
            continue
        choices[prefix] = domain.get("name") or prefix

    return choices


class ConfigPatternFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for radoff."""

    # Card M-07. A major bump, and the last one of this migration: the
    # entry's `data` changes shape (`domain_id` -> `domain_prefix`, holding
    # a value of a different kind, not a renamed one) and every entity
    # identifier under it is rewritten. `MINOR_VERSION` goes back to 1 with
    # it - the T-06/F2 AQI step it counted has been folded into the
    # version-3 migration, and a freshly created entry has nothing to
    # migrate.
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
        """
        Create the options flow (card S-11).

        `config_entry` is passed explicitly to `RadoffOptionsFlow.__init__`
        rather than relying on `OptionsFlow` to set `self.config_entry`
        automatically - same baseline-compatibility reasoning already
        applied to the re-auth flow in card S-08 (see that step's note on
        `async_update_reload_and_abort`). Note that reasoning cited a
        minimum of 2024.6.0: `hacs.json` actually declares **2025.1.4**
        (corrected while working on RT-2926, where the same claim would
        have ruled out a reconfigure flow that is in fact available). The
        explicit argument is kept anyway - it costs one line and depends on
        nothing.
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
                choices = domain_choices(info["domains"])

                if not choices:
                    return self.async_abort(reason="no_domains")

                self._user_input = user_input
                self._username = info["username"]
                self._api = info["api"]

                if len(choices) == 1:
                    # Card M-07 / T-08 D-32: one domain is not a question,
                    # it is an answer. The step is skipped entirely rather
                    # than shown with a single pre-selected option.
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
        Answer whether the chosen domain holds any device at all (T-08 D-32).

        Card M-07. A domain with no device produces an integration that sets
        up cleanly and then shows nothing, with no explanation anywhere -
        the "errore generico" this card exists to replace. One call answers
        it, on the session `validate_input` already opened.

        Only an *empty* answer is treated as an answer. Any failure - the
        call did not happen, was rate-limited, timed out - returns `True`,
        i.e. "do not block this setup": at that point the check has no
        opinion, and the coordinator's own first refresh handles a broken
        backend far better than this form can (it retries, with a backoff,
        and says what went wrong). Refusing to create the entry over a
        transient 429 would be a worse failure than the one being prevented.
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

        data = {**self._user_input, CONF_DOMAIN_PREFIX: domain_prefix}
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
        `_async_update_data` raises `ConfigEntryAuthFailed`). `entry_data` is
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
        entry's data - this avoids depending on `async_update_reload_and_abort`.
        S-08 justified that by a declared minimum of 2024.6.0; `hacs.json`
        actually declares **2025.1.4**, which does have that helper (noted
        while working on RT-2926). The listener-driven reload is kept - it
        is what the options flow relies on too - but it is a choice, not a
        compatibility constraint.

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
    Handle an options flow for radoff (card S-11).

    Two fields, both already consumed by `RadoffCoordinator.__init__`
    (`coordinator.py`) before this flow existed - it read `CONF_SCAN_INTERVAL`
    and `CONF_INDEX` from `config_entry.options` from the start, but nothing
    could ever write either one (finding F6: `MIN_SCAN_INTERVAL` was dead
    code, and disabling index entities required removing and re-adding the
    integration). This flow is the missing write path; no coordinator change
    was needed beyond passing the resolved interval through to `API` for the
    429 message (see `coordinator.py`, `api/client.py`).

    A third field for S-07's staleness multiplier was considered, per this
    card's own "COSA FARE" ("o la sua esposizione va valutata, vedi S-07"),
    and deliberately left out - decided with Piero. Card M-06 settled the
    question by removing the multiplier itself: entity availability comes
    from the device's `connection_status`, so there is no longer a
    freshness threshold for a user to tune, exposed or internal.

    Card M-02 adds a third field after all, but an **advanced** one: the API
    base URL (`CONF_BASE_URL`), shown only when Home Assistant's advanced
    mode is on. In arch 2.0 the environment *is* the host (the version lives
    in the hostname), so this single field is what lets an installation move
    between dev, stg and prod without a code change. A normal user has no
    reason to see it and none to change it - and, unlike the Cognito
    parameters S-02 removed from the config flow, it deliberately does not
    come back as a setup-time question: it belongs to a working entry's
    options, not to its identity.

    Card M-05 changes no code here and that is the point: the form's bounds
    are `MIN_SCAN_INTERVAL`/`DEFAULT_SCAN_INTERVAL` (const.py), so moving
    the floor to 60s and the default to 300s moved the form with them. Only
    the field's description had to be rewritten - it now says what the two
    numbers mean, since "how often to poll" is not what a user needs to
    know when the honest answer is "the device only speaks once a minute".
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
            # Card M-02. `show_advanced_options` is the same flag Home
            # Assistant uses for its own advanced fields: with advanced mode
            # off, the field is not in the schema at all, so the form a
            # normal user sees is byte-for-byte the S-11 one.
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

        Card M-02, and the reason this is a merge rather than the plain
        `user_input` S-11 saved: with advanced mode off, `CONF_BASE_URL` is
        not in the schema, so it is not in `user_input` either - saving that
        dict as-is would silently drop a base URL an advanced user had set,
        just because someone later changed the poll interval.

        A base URL equal to the default, or blanked out, is removed instead
        of stored: `DEFAULT_BASE_URL` (const.py) then stays the single
        authority, and an entry that never overrode it keeps following it
        when the constant itself moves (dev -> prod, at the end of this
        migration) instead of freezing today's value forever.
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
