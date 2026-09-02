"""
Cognito authentication primitives for the Radoff API.

Pure move from the former `api.py::API.connect` (see S-06b): the two pieces
of that method which only depend on their own arguments, not on any state
of the `API` instance (tokens, connection status, session) - the SRP
handshake against Cognito, and the expiry computed from `ExpiresIn`. Token
state itself stays on `API` in client.py; delegating it to a session object
is S-12's work, which needs refresh logic, not a move.
"""

import time
from typing import Any

from pycognito.aws_srp import AWSSRP


def authenticate_user(
    username: str,
    password: str,
    client_id: str,
    pool_id: str,
    pool_region: str,
) -> dict[str, Any] | None:
    """Perform the Cognito SRP authentication and return its raw result."""
    connection = AWSSRP(
        username=username,
        password=password,
        pool_id=pool_id,
        client_id=client_id,
        pool_region=pool_region,
    )
    return connection.authenticate_user()


def compute_token_expiry(expires_in: int) -> float:
    """Return the absolute time (`time.time()`-based) a token expires at."""
    return time.time() + expires_in
