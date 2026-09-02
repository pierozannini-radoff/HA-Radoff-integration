"""
Public re-exports for the `api` package.

This package replaces the former single `api.py` module (see S-06b - pure
move, no behaviour change). Everything importable from `api.py` before this
change is re-exported here unchanged, so `from .api import API, Device` and
similar imports elsewhere in the integration (coordinator.py, config_flow.py)
keep working without a single line of logic touched outside this package.

Every name below is listed in `__all__`, which is what tells ruff's
pyflakes-derived unused-import check (F401) that these imports are the
re-export itself, not dead code - no `noqa` needed.
"""

from .client import API
from .exceptions import (
    APIAuthError,
    APIConnectionError,
    BearerTokenNotFoundError,
    DomainNotFoundError,
)
from .models import Device, RadoffSensor

__all__ = [
    "API",
    "APIAuthError",
    "APIConnectionError",
    "BearerTokenNotFoundError",
    "Device",
    "DomainNotFoundError",
    "RadoffSensor",
]
