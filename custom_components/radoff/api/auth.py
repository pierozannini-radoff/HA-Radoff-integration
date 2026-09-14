"""Cognito authentication primitives for the Radoff API."""

import logging
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from pycognito.aws_srp import AWSSRP

_LOGGER = logging.getLogger(__name__)

# Credentials that are no longer valid. A wrong password and a disabled user
# share NotAuthorizedException: Cognito has no separate code for the latter.
_DEFINITIVE_AUTH_ERROR_CODES = frozenset(
    {"NotAuthorizedException", "UserNotFoundException"}
)

# Cognito error codes that say nothing about the credentials: retrying later
# can succeed.
_THROTTLING_ERROR_CODES = frozenset({"TooManyRequestsException"})

# Margin (seconds) before a token's own expiry at which it is already treated
# as expired, so a request in flight never races the real Cognito deadline.
_TOKEN_EXPIRY_MARGIN_SECONDS = 300

# Consecutive refresh failures tolerated, falling back to a full SRP login
# each time. Any successful login resets it, refresh or SRP.
_MAX_CONSECUTIVE_REFRESH_FAILURES = 2


class AuthExpiredError(Exception):
    """The session behind an already-issued token is no longer valid."""


class AuthInvalidError(Exception):
    """The configured credentials are no longer valid: re-auth, not retry."""


class AuthChallengeRequiredError(Exception):
    """Cognito wants a challenge this integration cannot complete."""

    def __init__(self, challenge_name: str) -> None:
        """Store the Cognito challenge name so the caller can report it."""
        super().__init__(f"Cognito requires an unsupported challenge: {challenge_name}")
        self.challenge_name = challenge_name


class AuthUnavailableError(Exception):
    """Cognito could not be reached, or is throttling: never an auth failure."""


def authenticate_user(
    username: str,
    password: str,
    client_id: str,
    pool_id: str,
    pool_region: str,
) -> dict[str, Any]:
    """
    Perform the Cognito SRP authentication and return its raw result.

    The result always carries an `AuthenticationResult` key. Raises
    `AuthInvalidError` on rejected credentials or no authentication data,
    `AuthChallengeRequiredError` on a challenge, and `AuthUnavailableError` on
    throttling or an unreachable endpoint; any other `ClientError` propagates
    unchanged, since it says nothing about the password.
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
    """Own the Cognito token lifecycle for one Radoff account."""

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

        Tries the current token, then a refresh, then a full SRP handshake. A
        single refresh failure falls back to SRP so the caller still gets a
        token now; a second consecutive one raises `AuthInvalidError`.
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

        This app client has refresh token rotation enabled, which rules out
        `InitiateAuth`/`REFRESH_TOKEN_AUTH` entirely and means every successful
        call returns a new `RefreshToken` that supersedes the one just used.
        Rotation also makes Cognito reject a just-issued `RefreshToken` for
        about a minute after the SRP login that minted it. Raises the
        underlying `ClientError` or `EndpointConnectionError` on failure.
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
        """Perform a full Cognito SRP handshake and store the resulting tokens."""
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

        A 401 kills the access token's session, not the `RefreshToken`, so the
        next call can still refresh instead of replaying a full SRP login.
        """
        self.tokens = {}
        self._token_expires_at = 0
