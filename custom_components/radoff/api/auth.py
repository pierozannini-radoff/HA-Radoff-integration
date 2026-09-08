"""
Cognito authentication primitives for the Radoff API.

Pure move from the former `api.py::API.connect` (see S-06b): the two pieces
of that method which only depend on their own arguments, not on any state
of the `API` instance (tokens, connection status, session) - the SRP
handshake against Cognito, and the expiry computed from `ExpiresIn`.

Card S-08 added the first two exceptions below and the classification
performed in `authenticate_user`: distinguishing a *definitive* credential
failure (wrong password, deleted or disabled Cognito user) from anything
else lets `coordinator.py` raise Home Assistant's `ConfigEntryAuthFailed` -
which opens the re-auth flow - only for the former, never for a transient
or network problem (see that card's acceptance criteria: "Un errore di rete
NON apre il flusso di re-auth.").

Card S-09 extends that same classification to close three more gaps found
during config-flow validation (`config_flow.py::validate_input`):

- C8: `authenticate_user()` used to hand back Cognito's raw response even
  when it was a *challenge* (`NEW_PASSWORD_REQUIRED`, an MFA challenge, ...)
  rather than a completed login - a dict with no `AuthenticationResult` key
  at all. The caller (`API.connect()` in client.py) only ever looked for
  that key to decide whether to mark itself connected; it never rejected the
  challenge case outright, so a config entry could be created for an account
  that will never be able to authenticate through this integration. This
  module now raises `AuthChallengeRequiredError` for that case instead of
  returning the challenge payload, and `AuthInvalidError` for the `None`
  case pycognito's own type hints allow but never documented a cause for.
- C9: only `NotAuthorizedException`/`UserNotFoundException` were mapped to
  `AuthInvalidError`; every other `ClientError` (throttling, a transient
  Cognito-side outage, ...) - and anything that is not a `ClientError` at
  all, such as `EndpointConnectionError` when Cognito cannot be reached -
  used to propagate unclassified, which is what made the config flow's
  declared-but-dead `CannotConnectError` (`config_flow.py`) truly
  unreachable and left both cases surfacing as "unknown". `AuthUnavailableError`
  below covers exactly that: a Cognito-side condition that says nothing
  about whether the credentials are correct, only that the request could not
  be completed right now.

Card S-12 moves the *token state* itself here too, in a new `CognitoSession`
class, and adds the missing piece: using Cognito's `RefreshToken` instead of
replaying a full SRP handshake on every expiry. Before this card, the
`RefreshToken` Cognito returns alongside every login was sitting unused in
`self.tokens` on `API` (client.py): every ~55 minutes (later found to
actually be ~24h, see below) `API.get_devices()` called `disconnect()` +
`connect()`, i.e. a brand new SRP handshake with the stored password, for no
reason other than the token being close to expiry (finding F7). That is
strictly more expensive than necessary, more exposed to Cognito's own rate
limits on the SRP flow specifically, and turns a transient SRP hiccup into a
full outage instead of a degraded retry. `CognitoSession.get_bearer()` is now
the single entry point for a usable bearer token: it returns the current
token if still valid, tries a lightweight refresh call if the token is stale
and a `RefreshToken` is on hand, and only falls back to a full SRP handshake
(`_srp_login()`, built on `authenticate_user()` below, unchanged) if there is
no `RefreshToken` yet or the refresh attempt itself failed. `API`
(client.py) no longer holds any token state at all - it asks this session
for a bearer token every time and never inspects `tokens` directly; see that
module for the composition.

IMPORTANT, found during this card's own manual AC1 verification against a
real account (2026-09-04): the Radoff Cognito app client has **refresh token
rotation enabled**. That setting makes `InitiateAuth`/`AuthFlow=
REFRESH_TOKEN_AUTH` - the "obvious" refresh API, and this card's first
implementation - unconditionally fail with
`UnsupportedOperationException: This API does not support refresh token
rotation`, regardless of what `AuthParameters` are passed: AWS simply does
not support that flow for a rotating app client at all. The correct API for
this app client is `GetTokensFromRefreshToken`, which `_refresh()` below
uses instead. Rotation also means the assumption "a refresh response never
carries a new RefreshToken" - true for a non-rotating app client, and this
card's own first (wrong) assumption - does NOT hold here: every successful
`GetTokensFromRefreshToken` call returns a *new* `RefreshToken`, and the one
just used is invalidated (after a short grace period) once the new one is
issued. `_refresh()` therefore adopts whatever `RefreshToken` the response
carries for the next cycle to use, falling back to keeping the current one
only if a response is ever missing that field.

Historical context on the timing this card changes: the natural-expiry
ceiling for a full SRP re-login used to be assumed at ~1 hour (~55 minutes
with the 300-second expiry margin `CognitoSession._is_token_expired` still
uses) but was found, verified against a real account on 2026-09-03, to
actually be `ExpiresIn` = 86400 seconds (24 hours) for this app client. That
number bounded how quickly a changed Radoff password used to get detected
via a live 401 or the periodic SRP reconnect (see `s-08-implementazione.md`).
After this card, the periodic path is normally the refresh call, not a new
SRP login, so that specific ceiling no longer applies the same way: the
practical detection window is now bounded by how long a given (rotating)
`RefreshToken` chain stays valid for this app client - which, because of the
rotation above, is a chain of values rather than one fixed token, and its
overall lifetime has not been independently verified against a real account
(Cognito's own default session length is 30 days) - or, sooner, by a live
401 during polling (see `api/client.py::_check_response_status`) or two
consecutive rejected refresh attempts (see `CognitoSession.get_bearer`).
This is flagged here, not resolved: verifying that chain's actual TTL needs
the same kind of empirical check T-02/S-08 already did for `ExpiresIn`, and
is tracked as a follow-up rather than blocking this card.

UPDATE (2026-09-07), found during a retest of the `GetTokensFromRefreshToken`
fix above against a real account: Cognito can reject a *freshly minted*
`RefreshToken` as genuinely invalid (`NotAuthorizedException: Invalid
Refresh Token`) on its very first use, roughly 60 seconds after the SRP
login that issued it - not a client-code bug, since the request is
confirmed to be hitting the right operation with the right, just-issued
token. This matches a public, unresolved AWS SDK issue reporting the same
symptom for `InitiateAuth`-issued tokens under refresh token rotation
(`aws/aws-sdk-js-v3#7162`, "ADMIN_NO_SRP_AUTH + Rotating Refresh Tokens -->
Invalid Refresh Token"; our SRP login goes through `InitiateAuth` with
`USER_SRP_AUTH` rather than `ADMIN_NO_SRP_AUTH`, but the shape of the
failure - a same-service token rejected by `GetTokensFromRefreshToken`
under rotation - is the same), which points to a likely AWS-side
limitation rather than anything fixable from this integration's code.

Because this can make *every* refresh attempt fail for a given login
session, not just an occasional one, it changes what the consecutive-
failure counter (`_MAX_CONSECUTIVE_REFRESH_FAILURES` below) needs to
tolerate: if that counter only ever reset on a successful *refresh* (this
card's original design), a persistently broken refresh chain would trip
`AuthInvalidError` - and Home Assistant's re-auth flow - roughly every
other polling cycle, even though the configured password is still
perfectly correct and the same-cycle SRP fallback keeps succeeding. Piero
reviewed this trade-off and chose to reset the counter on *any* successful
login, refresh or SRP (see `_srp_login()` below): this trades a small
amount of the counter's original purpose - catching a refresh mechanism
that is broken but silently hidden behind an always-working SRP fallback -
for not turning a known, currently unfixable AWS-side quirk into
recurring, spurious re-auth prompts. `AuthInvalidError` is still reachable
in the case that matters most (both refresh and the same-cycle SRP
fallback fail), just not merely because refresh keeps failing while SRP
keeps saving the call.
"""

import logging
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from pycognito.aws_srp import AWSSRP

_LOGGER = logging.getLogger(__name__)

# Cognito error codes that mean the credentials themselves are no longer
# valid, as opposed to a transient failure. Both cases named by card S-08 -
# "credenziali non più valide" and "utente disabilitato" - surface as the
# same NotAuthorizedException: Cognito returns that identical code for a
# plain wrong password *and* for a user disabled via AdminDisableUser, there
# is no separate code for the latter. UserNotFoundException covers a deleted
# account. A wrong client_id/pool_id/pool_region is deliberately NOT in this
# set: that fails earlier, inside AWSSRP's own setup, with a different error
# shape, and no password the user could re-enter would fix it - it is not a
# credentials problem in the sense this card cares about.
_DEFINITIVE_AUTH_ERROR_CODES = frozenset(
    {"NotAuthorizedException", "UserNotFoundException"}
)

# Cognito error codes that say nothing about the credentials themselves -
# the request could not be completed right now, but retrying later (or
# after this integration's own retry/backoff) could well succeed (card
# S-09, C9): the caller must never treat these as "wrong password".
_THROTTLING_ERROR_CODES = frozenset({"TooManyRequestsException"})

# Margin (seconds) before a token's own expiry at which it is already
# treated as expired, so a request in flight never races the real Cognito
# deadline. Card S-12 moves this from `API._is_token_expired` (client.py)
# to `CognitoSession._is_token_expired`, unchanged in value or meaning.
_TOKEN_EXPIRY_MARGIN_SECONDS = 300

# How many consecutive refresh failures `CognitoSession.get_bearer` tolerates
# - by falling back to a full SRP login each time - before giving up on the
# refresh path and raising `AuthInvalidError` instead of trying again (card
# S-12 AC: "rifiutato due volte porta al flusso di re-auth"). Reset by ANY
# successful login, refresh or SRP (decided with Piero on 2026-09-07,
# revising this card's original design - see the module docstring's
# 2026-09-07 update for the full story): a real account was found to
# sometimes reject every refresh attempt for a whole login session (a
# likely AWS-side limitation, not a bug here), and resetting only on a
# successful refresh would have turned that into a false re-auth prompt
# roughly every other polling cycle despite a still-correct password. The
# counter still protects the case that actually matters - refresh AND the
# same-cycle SRP fallback both failing - just not the narrower case of a
# refresh mechanism that is broken but always rescued by SRP.
_MAX_CONSECUTIVE_REFRESH_FAILURES = 2


class AuthExpiredError(Exception):
    """
    The session behind an already-issued token is no longer valid.

    The configured credentials may still be correct - the caller should
    simply retry, e.g. by reconnecting on the next poll. Raised by
    `api/client.py` on an HTTP 401 from an authenticated call; never raised
    from this module for that reason. Card S-12 also lets it be raised
    indirectly: `CognitoSession.get_bearer()` may itself perform a fresh
    SRP login when a refresh attempt fails, and that login can hit the same
    Cognito challenge/availability cases `authenticate_user` has always
    classified - none of those overlap with `AuthExpiredError`, which stays
    exclusively an `api/client.py` concern.
    """


class AuthInvalidError(Exception):
    """
    The configured credentials themselves are no longer valid.

    The password changed, the Cognito user was disabled or deleted (both
    surfaced by Cognito as `NotAuthorizedException`/`UserNotFoundException`,
    see `_DEFINITIVE_AUTH_ERROR_CODES`), pycognito returned no authentication
    data at all for a reason it does not itself document, or (card S-12)
    `CognitoSession.get_bearer()` had two consecutive refresh attempts
    rejected in a row. Recovering requires the user to re-enter their
    password, i.e. Home Assistant's re-auth flow
    (`config_flow.py::async_step_reauth_confirm`), not a silent retry.
    """


class AuthChallengeRequiredError(Exception):
    """
    Cognito responded with a challenge instead of a completed authentication.

    Raised when the SRP handshake's result carries no `AuthenticationResult`
    key - e.g. `NEW_PASSWORD_REQUIRED` (an account still on Cognito's own
    first-login pathway) or an MFA challenge (`SMS_MFA`, `SOFTWARE_TOKEN_MFA`,
    ...). Neither is something this integration's config flow can complete on
    the user's behalf (card S-09, "OUT OF SCOPE": actual MFA/NEW_PASSWORD_REQUIRED
    support needs dedicated flow steps and a product decision). The fix is to
    finish the pending step in the Radoff mobile app first, then retry setup
    here - this is card S-09's fix for C8: previously, a challenge response
    was silently treated as "not yet connected" while `connect()` still
    returned `True`, so `validate_input` could pass and create a config
    entry for an account that would never be able to authenticate.
    """

    def __init__(self, challenge_name: str) -> None:
        """Store the Cognito challenge name so the caller can report it."""
        super().__init__(f"Cognito requires an unsupported challenge: {challenge_name}")
        self.challenge_name = challenge_name


class AuthUnavailableError(Exception):
    """
    Cognito could not be reached, or is throttling this request.

    Covers `TooManyRequestsException` (see `_THROTTLING_ERROR_CODES`) and
    `EndpointConnectionError` (DNS/network failure reaching the Cognito
    endpoint itself, not a `ClientError` at all). Neither says anything
    about whether the configured credentials are correct - the caller
    should surface this as a connectivity problem (`cannot_connect`), not
    an authentication one, and it must never be able to trigger Home
    Assistant's re-auth flow.
    """


def authenticate_user(
    username: str,
    password: str,
    client_id: str,
    pool_id: str,
    pool_region: str,
) -> dict[str, Any]:
    """
    Perform the Cognito SRP authentication and return its raw result.

    The return value is guaranteed to be a dict containing an
    `AuthenticationResult` key - every other outcome this function knows
    how to recognise is raised as one of the exceptions below instead of
    being handed back to the caller for inspection (card S-09):

    - `AuthInvalidError`: Cognito rejected the credentials outright (see
      `_DEFINITIVE_AUTH_ERROR_CODES`), or returned no authentication data
      (`None`) at all.
    - `AuthChallengeRequiredError`: Cognito returned a challenge instead of
      a completed login (no `AuthenticationResult` key).
    - `AuthUnavailableError`: Cognito was throttling the request (see
      `_THROTTLING_ERROR_CODES`) or could not be reached at all
      (`EndpointConnectionError`).

    Any other `ClientError` (a malformed request, an unrecognised Cognito
    error code, ...) is re-raised unchanged, since it says nothing about
    whether the password is still correct and must not be able to trigger
    the re-auth flow (card S-08 AC: "Un errore di rete NON apre il flusso
    di re-auth.") nor be misreported as `cannot_connect`/`invalid_auth`.
    """
    connection = AWSSRP(
        username=username,
        password=password,
        pool_id=pool_id,
        client_id=client_id,
        pool_region=pool_region,
    )
    try:
        auth_data = connection.authenticate_user()
    except EndpointConnectionError as err:
        _LOGGER.debug("Unable to reach the Cognito endpoint: %s", err)
        msg = "Unable to reach the Radoff authentication service."
        raise AuthUnavailableError(msg) from err
    except ClientError as err:
        error_code = err.response.get("Error", {}).get("Code", "")
        if error_code in _DEFINITIVE_AUTH_ERROR_CODES:
            _LOGGER.debug(
                "Cognito rejected the configured credentials (%s)", error_code
            )
            msg = "The configured Radoff credentials are no longer valid."
            raise AuthInvalidError(msg) from err
        if error_code in _THROTTLING_ERROR_CODES:
            _LOGGER.debug(
                "Cognito is throttling the authentication request (%s)", error_code
            )
            msg = "The Radoff authentication service is temporarily unavailable."
            raise AuthUnavailableError(msg) from err
        raise

    if auth_data is None:
        msg = "Cognito returned no authentication data."
        raise AuthInvalidError(msg)

    if "AuthenticationResult" not in auth_data:
        challenge_name = auth_data.get("ChallengeName", "UNKNOWN")
        _LOGGER.debug(
            "Cognito requires a challenge this integration cannot complete: %s",
            challenge_name,
        )
        raise AuthChallengeRequiredError(challenge_name)

    return auth_data


def compute_token_expiry(expires_in: int) -> float:
    """Return the absolute time (`time.time()`-based) a token expires at."""
    return time.time() + expires_in


class CognitoSession:
    """
    Own the Cognito token lifecycle for one Radoff account (card S-12).

    Moved out of `API` (`api/client.py`), which used to hold `tokens` and
    `_token_expires_at` directly and, on every expiry, replayed the stored
    password through a brand new SRP handshake - see this module's own
    docstring for the cost that used to have. `get_bearer()` is the single
    entry point every caller should use; nothing outside this class reads
    `self.tokens` directly.

    Not thread-safe, deliberately - same reasoning card S-12 itself calls
    out: with a single `DataUpdateCoordinator`, calls into one `API`/
    `CognitoSession` pair are already serialized by construction. The config
    flow instantiates its own separate `API` (and therefore its own separate
    `CognitoSession`) for setup/re-auth validation, so the two never share
    state. This invariant holds today because of how the integration is
    structured, not because this class enforces it - exactly the caveat the
    card's own "CONTESTO" section raises about `disconnect()` recreating the
    HTTP session while a request could theoretically be in flight.
    """

    def __init__(
        self,
        username: str,
        password: str,
        client_id: str,
        pool_id: str,
        pool_region: str,
    ) -> None:
        """Store the account's Cognito identity. No network call is made yet."""
        self.username = username
        self.password = password
        self.client_id = client_id
        self.pool_id = pool_id
        self.pool_region = pool_region
        self.tokens: dict[str, Any] = {}
        self._token_expires_at: float = 0
        self._refresh_token: str | None = None
        self._consecutive_refresh_failures: int = 0

    def _is_token_expired(self) -> bool:
        """Check if the current token is expired or will expire soon."""
        if not self.tokens:
            return True
        return time.time() >= (self._token_expires_at - _TOKEN_EXPIRY_MARGIN_SECONDS)

    def get_bearer(self) -> str:
        """
        Return a valid Cognito IdToken, refreshing or re-logging in as needed.

        Single entry point for every other module (card S-12): callers no
        longer check expiry or the `RefreshToken` themselves. Order of
        attempts:

        1. the current token, if not close to expiring;
        2. `_refresh()` via Cognito's `GetTokensFromRefreshToken` operation,
           if a `RefreshToken` is on hand - a lightweight call, not a full
           SRP handshake, and one that needs no domain re-discovery;
        3. `_srp_login()`, the full SRP handshake - either because there is
           no `RefreshToken` yet (first call ever on this session) or
           because the refresh attempt itself just failed.

        A single refresh failure does not give up on this call: it counts
        toward `_consecutive_refresh_failures` and falls back to
        `_srp_login()` so the caller still gets a usable token right now
        (card S-12 AC: "Un refresh rifiutato una volta viene ritentato" -
        the retry being that same-cycle SRP fallback, and the next expiry
        naturally re-attempting a refresh with whatever `RefreshToken` that
        fallback obtained). A second consecutive refresh failure only
        raises `AuthInvalidError` if the same-cycle SRP fallback that
        follows the *first* failure also fails to complete (e.g. raises
        `AuthUnavailableError`): both `_refresh()` and `_srp_login()` reset
        the counter to 0 on success (revised with Piero on 2026-09-07, see
        `_MAX_CONSECUTIVE_REFRESH_FAILURES`'s comment and the module
        docstring), so the common case of "refresh fails, SRP fallback
        succeeds" never trips it, however many cycles in a row it happens.
        `AuthInvalidError` still maps to `ConfigEntryAuthFailed` by
        `coordinator.py` when it is raised (card S-12 AC: "rifiutato due
        volte porta al flusso di re-auth", unchanged since S-08).

        A refresh call can fail for reasons that say nothing about the
        credentials themselves (a transient network blip, Cognito
        throttling) as much as for a truly rejected `RefreshToken` - this
        method does not distinguish between them for the failure counter,
        unlike `authenticate_user`'s careful classification of the SRP path:
        the card's own acceptance criteria only ask about "un refresh
        rifiutato", and the SRP fallback triggered here still goes through
        that full classification on its own, so a transient failure that
        happens to also break the SRP fallback surfaces correctly as
        `AuthUnavailableError`, never as `AuthInvalidError`.
        """
        if self.tokens and not self._is_token_expired():
            return self.tokens["IdToken"]

        if self._refresh_token:
            _LOGGER.debug("Cognito token expired; attempting a token refresh")
            try:
                self._refresh()
            except (ClientError, EndpointConnectionError) as err:
                self._consecutive_refresh_failures += 1
                _LOGGER.debug(
                    "Cognito refresh failed (%d consecutive failure(s)): %s",
                    self._consecutive_refresh_failures,
                    err,
                )
                if (
                    self._consecutive_refresh_failures
                    >= _MAX_CONSECUTIVE_REFRESH_FAILURES
                ):
                    self.invalidate()
                    self._refresh_token = None
                    msg = (
                        "Cognito rejected the refresh token twice in a row; "
                        "the configured Radoff credentials must be re-entered."
                    )
                    raise AuthInvalidError(msg) from err
                self._srp_login()
                return self.tokens["IdToken"]
            else:
                return self.tokens["IdToken"]

        self._srp_login()
        return self.tokens["IdToken"]

    def _refresh(self) -> None:
        """
        Attempt to renew the session via Cognito's GetTokensFromRefreshToken.

        Raises the underlying `ClientError`/`EndpointConnectionError` on any
        failure - `get_bearer()` is what decides whether that counts toward
        `_consecutive_refresh_failures` and whether to fall back to
        `_srp_login()`. On success, `self.tokens` is updated with the new
        `IdToken`/`AccessToken`/`ExpiresIn`.

        Uses `GetTokensFromRefreshToken`, not `InitiateAuth`/
        `AuthFlow=REFRESH_TOKEN_AUTH`: this card's own manual AC1
        verification against a real account (2026-09-04) found that the
        Radoff Cognito app client has refresh token rotation enabled, which
        makes `InitiateAuth` unconditionally reject that flow with
        `UnsupportedOperationException: This API does not support refresh
        token rotation` - see the module docstring for the full story.
        `GetTokensFromRefreshToken` is the API AWS documents as the
        rotation-compatible replacement, and needs only `ClientId` and the
        stored `RefreshToken` (no password, no SRP math, no `USERNAME`).

        Because this app client rotates, every successful call here returns
        a *new* `RefreshToken` and invalidates the one just used (after a
        short grace period) - unlike a non-rotating app client, where a
        refresh response never carries a new one. `self._refresh_token` is
        therefore updated from the response, falling back to keeping the
        current value only if a response is ever missing that field (e.g. a
        hypothetical future app client change back to non-rotating).

        Like the SRP calls this integration already makes, this is an
        unauthenticated Cognito Identity Provider API call - no AWS
        credentials are required or configured for it.
        """
        client = boto3.client("cognito-idp", region_name=self.pool_region)
        response = client.get_tokens_from_refresh_token(
            ClientId=self.client_id,
            RefreshToken=self._refresh_token,
        )

        auth_result = response.get("AuthenticationResult")
        if not auth_result or "IdToken" not in auth_result:
            msg = "Cognito refresh returned no usable AuthenticationResult."
            raise ClientError(
                {"Error": {"Code": "InvalidRefreshResponse", "Message": msg}},
                "GetTokensFromRefreshToken",
            )

        self.tokens = auth_result
        self._refresh_token = auth_result.get("RefreshToken", self._refresh_token)
        expires_in = auth_result.get("ExpiresIn", 3600)
        self._token_expires_at = compute_token_expiry(expires_in)
        self._consecutive_refresh_failures = 0
        _LOGGER.debug(
            "Refreshed Cognito session via GetTokensFromRefreshToken; "
            "expires in %d seconds",
            expires_in,
        )

    def _srp_login(self) -> None:
        """
        Perform a full Cognito SRP handshake and store the resulting tokens.

        Delegates the handshake itself, and all of its error classification
        (`AuthInvalidError`/`AuthChallengeRequiredError`/`AuthUnavailableError`),
        to `authenticate_user` above - unchanged since cards S-08/S-09.
        A successful SRP login also returns a `RefreshToken`, stored here for
        the next `get_bearer()` cycle to use instead of another full login.

        Resets `_consecutive_refresh_failures` to 0 on success, same as
        `_refresh()` (revised with Piero on 2026-09-07, superseding this
        card's original design - see `_MAX_CONSECUTIVE_REFRESH_FAILURES`'s
        comment and the module docstring's 2026-09-07 update for why): a
        real account was found to sometimes reject every refresh attempt
        for a whole login session for reasons outside this integration's
        control, and only resetting on a successful *refresh* would have
        let that turn into a false re-auth prompt roughly every other
        polling cycle despite a still-correct password. A login here can be
        either the very first one for this session or a fallback after a
        rejected refresh; either way, a token in hand right now is what the
        counter ultimately exists to guarantee, and this method just
        obtained one.
        """
        _LOGGER.debug("Performing full Cognito SRP handshake")
        auth_data = authenticate_user(
            username=self.username,
            password=self.password,
            client_id=self.client_id,
            pool_id=self.pool_id,
            pool_region=self.pool_region,
        )
        auth_result = auth_data["AuthenticationResult"]
        self.tokens = auth_result
        self._refresh_token = auth_result.get("RefreshToken")
        expires_in = auth_result.get("ExpiresIn", 3600)
        self._token_expires_at = compute_token_expiry(expires_in)
        self._consecutive_refresh_failures = 0
        _LOGGER.info("Token will expire in %d seconds", expires_in)

    def invalidate(self) -> None:
        """
        Clear the current token state, without touching the `RefreshToken`.

        Replaces the old `API.disconnect()` on the token-expiry/401 path
        (card S-12, "COSA FARE" step 5): a rejected or expired *access*
        token is a credentials-adjacent problem, not a networking one, so
        `API.disconnect()` recreating the `requests.Session` (and its
        connection pool) for it was unwarranted churn - it used to do that
        on every single reconnect, roughly once every 24h per user before
        this card, or on every live 401. This method leaves the HTTP
        transport session untouched entirely; `api/client.py` no longer
        calls anything resembling `disconnect()`.

        The `RefreshToken` is deliberately NOT cleared here: an HTTP 401 on
        a single request (see `api/client.py::_check_response_status`) means
        the current *access* token's session died, not that the
        `RefreshToken` itself is bad - the next `get_bearer()` call can and
        should try `_refresh()` with it before falling back to a full SRP
        login (card S-12 AC: "Un 401 su una singola richiesta non abbatte
        l'integrazione: il ciclo successivo riparte con un token nuovo").

        `_consecutive_refresh_failures` is also left untouched: invalidating
        the current tokens says nothing about whether the refresh mechanism
        itself is healthy, which is the only thing that counter tracks.
        """
        self.tokens = {}
        self._token_expires_at = 0
