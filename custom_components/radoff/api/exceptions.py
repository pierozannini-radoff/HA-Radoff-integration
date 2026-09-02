"""
Exceptions for the Radoff API.

Pure move from the former `api.py` (see S-06b): same names, same base
class, same docstrings. Extracted first because nothing else in the `api`
package depends on anything but this module.
"""


class APIAuthError(Exception):
    """Exception class for auth error."""


class APIConnectionError(Exception):
    """Exception class for connection error."""


class DomainNotFoundError(Exception):
    """Exception class for domain not found error."""


class BearerTokenNotFoundError(Exception):
    """Exception class for bearer token not found/available."""
