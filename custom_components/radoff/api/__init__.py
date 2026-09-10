"""
Public re-exports for the `api` package.

This package replaces the former single `api.py` module (see S-06b - pure
move, no behaviour change). Everything importable from `api.py` before that
change is re-exported here; card S-07 renames the two domain dataclasses
(`Device` -> `RadoffDevice`, `RadoffSensor` -> `Reading`, see
`api/models.py`), so `from .api import API, RadoffDevice` and similar
imports elsewhere in the integration (coordinator.py, sensor.py) use the new
names from this point on. Card S-08 added `AuthExpiredError`/`AuthInvalidError`
from `api/auth.py`, so `coordinator.py` and `config_flow.py` can import the
re-auth exception types the same way. Card S-09 adds two more from the same
module - `AuthChallengeRequiredError` and `AuthUnavailableError` - so
`config_flow.py` can map every outcome of the Cognito handshake to a
distinct, translated user-facing message instead of falling through to
"unknown" (findings C8/C9). Card M-03 removes `DeviceFetchError`
(`api/models.py`) so `coordinator.py` can type the per-device errors
`API.get_devices()` now returns instead of raising. Card M-06 adds `ConnectionState`,
`KNOWN_CONNECTION_STATUSES` and `classify_connection_status` from the same
module, so `entity.py` can read a device's connection without importing from
the package's internals. Card M-02 adds the five
classes of the arch 2.0 error taxonomy (`api/exceptions.py`), so
`coordinator.py` can react differently to "domain access revoked", "rate
limited", "transient backend failure" and the two kinds of 404 instead of
treating every non-200 as the same `APIAuthError`.

Every name below is listed in `__all__`, which is what tells ruff's
pyflakes-derived unused-import check (F401) that these imports are the
re-export itself, not dead code - no `noqa` needed.
"""

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
