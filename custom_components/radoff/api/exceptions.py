"""
Exceptions for the Radoff API.

Every response error subclasses `APIAuthError`: it is the catch-all callers
already handle, and subclassing lets newer code tell the causes apart.
"""


class APIAuthError(Exception):
    """Exception class for auth error."""


class APIConnectionError(Exception):
    """Exception class for connection error."""


class DomainNotFoundError(Exception):
    """Exception class for domain not found error."""


class BearerTokenNotFoundError(Exception):
    """Exception class for bearer token not found/available."""


class APIDomainAccessError(APIAuthError):
    """HTTP 403: the account does not belong to the queried domain."""


class APIRateLimitError(APIAuthError):
    """
    HTTP 429: the shared API Gateway quota for this stage was exceeded.

    The quota is shared with Radoff's own apps and the response carries no
    `Retry-After`, so `retry_after` holds the delay this client computed.
    """

    def __init__(self, message: str, retry_after: float) -> None:
        """Store the backoff delay, in seconds, this 429 should be honoured with."""
        super().__init__(message)
        self.retry_after = retry_after


class APIServerError(APIAuthError):
    """HTTP 5xx: a transient failure on the Radoff side. Retryable as-is."""


class APIDeviceNotFoundError(APIAuthError):
    """HTTP 404 on a single device: unknown serial, or a device with no data."""


class APIUnknownDeviceTypeError(APIAuthError):
    """HTTP 404 from `measures-ranges`: `available` lists the valid types."""

    def __init__(self, message: str, available: list[str]) -> None:
        """Store the `available` device types the backend listed in the 404 body."""
        super().__init__(message)
        self.available = available
