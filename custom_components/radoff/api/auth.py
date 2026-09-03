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
token if still valid, tries a lightweight `REFRESH_TOKEN_AUTH` call if the
token is stale and a `RefreshToken` is on hand, and only falls back to a full
SRP handshake (`_srp_login()`, built on `authenticate_user()` below,
unchanged) if there is no `RefreshToken` yet or the refresh attempt itself
failed. `API` (client.py) no longer holds any token state at all - it asks
this session for a bearer token every time and never inspects `tokens`
directly; see that module for the composition.

Historical context on the timing this card changes: the natural-expiry
ceiling for a full SRP re-login used to be assumed at ~1 hour (~55 minutes
with the 300-second expiry margin `CognitoSession._is_token_expired` still
uses) but was found, verified against a real account on 2026-09-03, to
actually be `ExpiresIn` = 86400 seconds (24 hours) for this app client. That
number bounded how quickly a changed Radoff password used to get detected
via a live 401 or the periodic SRP reconnect (see `s-08-implementazione.md`).
After this card, the periodic path is normally the refresh call, not a new
SRP login, so that specific ceiling no longer applies the same way: the
practical detection window is now bounded by how long Cognito's
`RefreshToken` itself stays valid for this app client (Cognito's own
default is 30 days, but the actual configured value for the Radoff pool has
not been verified against a real account) or, sooner, by a live 401 during
polling (see `api/client.py::_check_response_status`) or two consecutive
rejected refresh attempts (see `CognitoSession.get_bearer`). This is flagged
here, not resolved: verifying the app client's actual refresh-token TTL
needs the same kind of empirical check T-02/S-08 already did for `ExpiresIn`,
and is tracked as a follow-up rather than blocking this card.
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

# How many consecutive REFRESH_TOKEN_AUTH failures `CognitoSession.get_bearer`
# tolerates - by falling back to a full SRP login each time - before giving
# up on the refresh path and raising `AuthInvalidError` instead of trying
# again (card S-12 AC: "rifiutato due volte porta al flusso di re-auth").
# Deliberately NOT reset by a successful SRP fallback (decided with Piero):
# only a *successful refresh* resets the counter (see
# `CognitoSession._refresh`) - otherwise a structurally broken refresh
# mechanism (e.g. a Cognito app client misconfiguration) would hide forever
# behind the SRP fallback, defeating the point of this card.
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
    `CognitoSession.get_bearer()` had two consecutive `REFRESH_TOKEN_AUTH`
    attempts rejected in a row. Recovering requires the user to re-enter
    their password, i.e. Home Assistant's re-auth flow
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
        2. `_refresh()` via Cognito's `REFRESH_TOKEN_AUTH`, if a
           `RefreshToken` is on hand - a lightweight call, not a full SRP
           handshake, and one that needs no domain re-discovery;
        3. `_srp_login()`, the full SRP handshake - either because there is
           no `RefreshToken` yet (first call ever on this session) or
           because the refresh attempt itself just failed.

        A single refresh failure does not give up on this call: it counts
        toward `_consecutive_refresh_failures` and falls back to
        `_srp_login()` so the caller still gets a usable token right now
        (card S-12 AC: "Un refresh rifiutato una volta viene ritentato" -
        the retry being that same-cycle SRP fallback, and the next expiry
        naturally re-attempting a refresh with whatever `RefreshToken` that
        fallback obtained). Only a *second* consecutive refresh failure
        raises `AuthInvalidError` instead of falling back again (card S-12
        AC: "rifiutato due volte porta al flusso di re-auth", mapped to
        `ConfigEntryAuthFailed` by `coordinator.py`, unchanged since S-08).

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
            _LOGGER.debug("Cognito token expired; attempting REFRESH_TOKEN_AUTH")
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
        Attempt to renew the current session via Cognito's REFRESH_TOKEN_AUTH flow.

        Raises the underlying `ClientError`/`EndpointConnectionError` on any
        failure - `get_bearer()` is what decides whether that counts toward
        `_consecutive_refresh_failures` and whether to fall back to
        `_srp_login()`. On success, `self.tokens` is updated with the new
        `IdToken`/`AccessToken`/`ExpiresIn`. Cognito's refresh response never
        contains a new `RefreshToken` (card S-12 "COSA FARE", step 3), so
        `self._refresh_token` is deliberately left untouched here - the
        original one, obtained at the last full SRP login, keeps being used
        until it is itself rejected.

        Uses a plain `boto3` `cognito-idp` client rather than `pycognito`'s
        `AWSSRP` (which only implements the SRP flow): `InitiateAuth` with
        `REFRESH_TOKEN_AUTH` needs no password and no SRP math, only the
        stored `RefreshToken`. Like the SRP calls this integration already
        makes, this is an unauthenticated Cognito Identity Provider API
        call - no AWS credentials are required or configured for it.
        """
        client = boto3.client("cognito-idp", region_name=self.pool_region)
        response = client.initiate_auth(
            ClientId=self.client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={
                "REFRESH_TOKEN": self._refresh_token,
                "USERNAME": self.username,
            },
        )

        auth_result = response.get("AuthenticationResult")
        if not auth_result or "IdToken" not in auth_result:
            msg = "Cognito refresh returned no usable AuthenticationResult."
            raise ClientError(
                {"Error": {"Code": "InvalidRefreshResponse", "Message": msg}},
                "InitiateAuth",
            )

        self.tokens = auth_result
        expires_in = auth_result.get("ExpiresIn", 3600)
        self._token_expires_at = compute_token_expiry(expires_in)
        self._consecutive_refresh_failures = 0
        _LOGGER.debug(
            "Refreshed Cognito session via REFRESH_TOKEN_AUTH; expires in %d seconds",
            expires_in,
        )

    def _srp_login(self) -> None:
        """
        Perform a full Cognito SRP handshake and store the resulting tokens.

        Delegates the handshake itself, and all of its error classification
        (`AuthInvalidError`/`AuthChallengeRequiredError`/`AuthUnavailableError`),
        to `authenticate_user` above - unchanged since cards S-08/S-09.
        Unlike `_refresh()`, a successful SRP login *does* return a new
        `RefreshToken`, stored here for the next `get_bearer()` cycle to use
        instead of another full login. Deliberately does not reset
        `_consecutive_refresh_failures` (decided with Piero, see that
        counter's own comment) - a login here can be either the very first
        one for this session, or a fallback after a rejected refresh, and
        only a genuinely successful *refresh* proves the mechanism healthy
        again.
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
