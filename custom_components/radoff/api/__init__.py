"""
Public re-exports for the `api` package.

This package replaces the former single `api.py` module (see S-06b - pure
move, no behaviour change). Everything importable from `api.py` before that
change is re-exported here; card S-07 renames the two domain dataclasses
(`Device` -> `RadoffDevice`, `RadoffSensor` -> `Reading`, see
`api/models.py`), so `from .api import API, RadoffDevice` and similar
imports elsewhere in the integration (coordinator.py, sensor.py) use the new
names from this point on. Card S-08 adds `AuthExpiredError`/`AuthInvalidError` from
`api/auth.py`, so `coordinator.py` and `config_flow.py` can import the
re-auth exception types the same way.

Every name below is listed in `__all__`, which is what tells ruff's
pyflakes-derived unused-import check (F401) that these imports are the
re-export itself, not dead code - no `noqa` needed.
"""

from .auth import AuthExpiredError, AuthInvalidError
from .client import API
from .exceptions import (
    APIAuthError,
    APIConnectionError,
    BearerTokenNotFoundError,
    DomainNotFoundError,
)
from .models import RadoffDevice, Reading

__all__ = [
    "API",
    "APIAuthError",
    "APIConnectionError",
    "AuthExpiredError",
    "AuthInvalidError",
    "BearerTokenNotFoundError",
    "DomainNotFoundError",
    "RadoffDevice",
    "Reading",
]
