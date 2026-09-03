"""
Cognito authentication primitives for the Radoff API.

Pure move from the former `api.py::API.connect` (see S-06b): the two pieces
of that method which only depend on their own arguments, not on any state
of the `API` instance (tokens, connection status, session) - the SRP
handshake against Cognito, and the expiry computed from `ExpiresIn`. Token
state itself stays on `API` in client.py; delegating it to a session object
is S-12's work, which needs refresh logic, not a move.

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

Important timing caveat, documented here because it constrains what this
module can promise: `API.connect()` (see client.py) is used both for the
*initial* login and for the periodic reconnect this integration performs by
replaying the stored password via a full SRP handshake (no Cognito
RefreshToken is used today - see finding F7, card S-12). This means a
password changed on the Radoff side is only detected here the next time
that reconnect actually runs. Depending on whether the Radoff backend
invalidates already-issued sessions immediately (global sign-out) or only
lets the old token expire naturally, that can be as soon as the next couple
of poll cycles (a 401 from a live API call triggers a reconnect attempt on
the following poll - see client.py's `_check_response_status`), or as late
as the token's own natural expiry.

That natural-expiry ceiling was originally assumed to be ~1 hour (~55
minutes with `_is_token_expired`'s 5-minute margin) but is, verified
against a real account on 2026-09-03, `ExpiresIn` = 86400 seconds (24
hours) for this app client - i.e. up to ~23h55m without a live 401 or a
manual reconnect (integration reload or Home Assistant restart, both of
which start a fresh, disconnected `API` and force an immediate
re-authentication attempt). Shortening the practical detection window (e.g.
a shorter Cognito token TTL, or a dedicated re-validation interval
independent of the token's own expiry) is out of scope here and tracked
under S-12 alongside the RefreshToken work.
"""

import logging
import time
from typing import Any

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


class AuthExpiredError(Exception):
    """
    The session behind an already-issued token is no longer valid.

    The configured credentials may still be correct - the caller should
    simply retry, e.g. by reconnecting on the next poll. Raised by
    `api/client.py` on an HTTP 401 from an authenticated call; never raised
    from this module, which only performs the initial/periodic Cognito
    handshake itself.
    """


class AuthInvalidError(Exception):
    """
    The configured credentials themselves are no longer valid.

    The password changed, the Cognito user was disabled or deleted (both
    surfaced by Cognito as `NotAuthorizedException`/`UserNotFoundException`,
    see `_DEFINITIVE_AUTH_ERROR_CODES`), or pycognito returned no
    authentication data at all for a reason it does not itself document.
    Recovering requires the user to re-enter their password, i.e. Home
    Assistant's re-auth flow (`config_flow.py::async_step_reauth_confirm`),
    not a silent retry.
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
        super().__init__(
            f"Cognito requires an unsupported challenge: {challenge_name}"
        )
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
