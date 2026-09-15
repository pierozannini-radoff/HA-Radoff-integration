"""Public re-exports for the `api` package."""

from .auth import (
    AuthChallengeRequiredError,
    AuthExpiredError,
    AuthInvalidError,
    AuthUnavailableError,
)
from .client import API
from .exceptions import (
    APIAuthError,
    APIConnectionError,
    APIDeviceNotFoundError,
    APIDomainAccessError,
    APIRateLimitError,
    APIServerError,
    APIUnknownDeviceTypeError,
    BearerTokenNotFoundError,
    DomainNotFoundError,
)
from .models import (
    KNOWN_CONNECTION_STATUSES,
    ConnectionState,
    RadoffDevice,
    Reading,
    classify_connection_status,
)

__all__ = [
    "API",
    "APIAuthError",
    "APIConnectionError",
    "APIDeviceNotFoundError",
    "APIDomainAccessError",
    "APIRateLimitError",
    "APIServerError",
    "APIUnknownDeviceTypeError",
    "AuthChallengeRequiredError",
    "AuthExpiredError",
    "AuthInvalidError",
    "AuthUnavailableError",
    "KNOWN_CONNECTION_STATUSES",
    "BearerTokenNotFoundError",
    "ConnectionState",
    "DomainNotFoundError",
    "RadoffDevice",
    "Reading",
    "classify_connection_status",
]
