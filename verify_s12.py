#!/usr/bin/env python3
"""
Standalone verification for card S-12 (`api/auth.py::CognitoSession`).

Same convention as `verify_s08.py`/`verify_s09.py`: loads `api/auth.py`
directly via `importlib` from its file path, bypassing
`custom_components/radoff/__init__.py` and `api/__init__.py` (which import
`api/client.py` -> `..properties` -> `homeassistant`, unavailable in this
sandbox). `auth.py` itself has no `homeassistant` dependency at all (only
`boto3`, `botocore` and `pycognito`), which is exactly what makes this
possible.

`boto3.client(...)` and `pycognito.aws_srp.AWSSRP` are monkeypatched on the
loaded module object with small fakes so every scenario below runs with no
network access and no real Cognito account, exercising exactly the
get_bearer()/_refresh()/_srp_login()/invalidate() logic this card adds -
not `api/client.py` or `coordinator.py`, which need a real Home Assistant
install to exercise meaningfully (see verify_s08.py's own note on this) and
are instead checked with `py_compile` + `ruff` here, plus manual
verification against the dev harness for the timing-dependent acceptance
criteria (see `s-12-implementazione.md`).

Usage: `python3 verify_s12.py` from the repository root.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

AUTH_PY = Path(__file__).parent / "custom_components" / "radoff" / "api" / "auth.py"

_spec = importlib.util.spec_from_file_location("radoff_auth", AUTH_PY)
if _spec is None or _spec.loader is None:
    print(f"FAIL: could not load spec for {AUTH_PY}")
    sys.exit(1)
auth = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = auth
_spec.loader.exec_module(auth)

from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

FAILURES: list[str] = []


def check(label: str, *, condition: bool) -> None:
    """Record a single PASS/FAIL line for `label`."""
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        FAILURES.append(label)


def _make_session(
    *,
    tokens: dict | None = None,
    expires_at: float = 0,
    refresh_token: str | None = None,
    consecutive_failures: int = 0,
) -> "auth.CognitoSession":  # type: ignore[name-defined]
    session = auth.CognitoSession(
        username="user@example.com",
        password="hunter2",
        client_id="client-id",
        pool_id="pool-id",
        pool_region="eu-west-1",
    )
    session.tokens = tokens or {}
    session._token_expires_at = expires_at  # noqa: SLF001
    session._refresh_token = refresh_token  # noqa: SLF001
    session._consecutive_refresh_failures = consecutive_failures  # noqa: SLF001
    return session


def _srp_result(id_token: str, refresh_token: str | None = "srp-refresh") -> dict:
    result = {
        "AuthenticationResult": {
            "IdToken": id_token,
            "AccessToken": f"access-{id_token}",
            "ExpiresIn": 3600,
        }
    }
    if refresh_token is not None:
        result["AuthenticationResult"]["RefreshToken"] = refresh_token
    return result


def _client_error(code: str = "NotAuthorizedException") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "Refresh Token has expired"}},
        "InitiateAuth",
    )


def _forbid_srp() -> None:
    """Patch AWSSRP so calling it fails the test loudly."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "AWSSRP should not have been called in this scenario"
        raise AssertionError(msg)

    auth.AWSSRP = _boom  # noqa: SLF001


def _forbid_refresh() -> None:
    """Patch boto3.client so a refresh attempt fails the test loudly."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "boto3.client (Cognito refresh) should not have been called"
        raise AssertionError(msg)

    auth.boto3 = MagicMock(client=_boom)  # noqa: SLF001


def _allow_srp(id_token: str, refresh_token: str | None = "srp-refresh") -> None:
    fake_srp = MagicMock()
    fake_srp.authenticate_user.return_value = _srp_result(id_token, refresh_token)
    auth.AWSSRP = MagicMock(return_value=fake_srp)  # noqa: SLF001


def _allow_refresh(
    *, id_token: str | None = None, error: Exception | None = None
) -> None:
    fake_client = MagicMock()
    if error is not None:
        fake_client.initiate_auth.side_effect = error
    else:
        fake_client.initiate_auth.return_value = {
            "AuthenticationResult": {
                "IdToken": id_token,
                "AccessToken": f"access-{id_token}",
                "ExpiresIn": 3600,
            }
        }
    auth.boto3 = MagicMock(client=MagicMock(return_value=fake_client))  # noqa: SLF001


def test_valid_token_short_circuits() -> None:
    session = _make_session(
        tokens={"IdToken": "still-good"}, expires_at=time.time() + 10_000
    )
    _forbid_refresh()
    _forbid_srp()
    bearer = session.get_bearer()
    check(
        "valid token: returned without any Cognito call",
        condition=bearer == "still-good",
    )


def test_refresh_success_no_srp() -> None:
    session = _make_session(
        tokens={"IdToken": "expiring"},
        expires_at=time.time() - 100,
        refresh_token="rt-original",
    )
    _forbid_srp()
    _allow_refresh(id_token="refreshed-id")
    bearer = session.get_bearer()
    check(
        "expired token + refresh token: refresh used, SRP not called",
        condition=bearer == "refreshed-id",
    )
    check(
        "successful refresh preserves the original RefreshToken (Cognito never returns a new one)",
        condition=session._refresh_token == "rt-original",  # noqa: SLF001
    )
    check(
        "successful refresh resets the consecutive-failure counter",
        condition=session._consecutive_refresh_failures == 0,  # noqa: SLF001
    )


def test_refresh_rejected_once_falls_back_to_srp() -> None:
    session = _make_session(tokens={}, refresh_token="rt-1", consecutive_failures=0)
    _allow_refresh(error=_client_error())
    _allow_srp("srp-fallback-id", refresh_token="rt-2")
    bearer = session.get_bearer()
    check(
        "refresh rejected once: falls back to SRP and still returns a bearer token",
        condition=bearer == "srp-fallback-id",
    )
    check(
        "one rejected refresh increments the failure counter to 1",
        condition=session._consecutive_refresh_failures == 1,  # noqa: SLF001
    )
    check(
        "SRP fallback's new RefreshToken replaces the rejected one",
        condition=session._refresh_token == "rt-2",  # noqa: SLF001
    )


def test_refresh_rejected_twice_raises_auth_invalid_without_srp() -> None:
    session = _make_session(tokens={}, refresh_token="rt-2", consecutive_failures=1)
    _allow_refresh(error=_client_error())
    _forbid_srp()
    try:
        session.get_bearer()
    except auth.AuthInvalidError:
        raised = True
    else:
        raised = False
    check(
        "second consecutive refresh rejection raises AuthInvalidError, no SRP attempted",
        condition=raised,
    )
    check("AuthInvalidError clears the current tokens", condition=session.tokens == {})
    check(
        "AuthInvalidError also clears the now-untrusted RefreshToken",
        condition=session._refresh_token is None,  # noqa: SLF001
    )


def test_no_refresh_token_goes_straight_to_srp() -> None:
    session = _make_session(tokens={}, refresh_token=None)
    _forbid_refresh()
    _allow_srp("first-login-id", refresh_token="first-rt")
    bearer = session.get_bearer()
    check(
        "no RefreshToken yet: SRP used directly, refresh not attempted",
        condition=bearer == "first-login-id",
    )
    check(
        "first SRP login stores the RefreshToken for next time",
        condition=session._refresh_token == "first-rt",  # noqa: SLF001
    )


def test_invalidate_keeps_refresh_token_and_prefers_refresh_next_time() -> None:
    session = _make_session(
        tokens={"IdToken": "will-be-invalidated"},
        expires_at=time.time() + 10_000,
        refresh_token="keep-me",
    )
    session.invalidate()
    check("invalidate() clears the current tokens", condition=session.tokens == {})
    check(
        "invalidate() does NOT clear the RefreshToken (a 401 isn't a refresh-token problem)",
        condition=session._refresh_token == "keep-me",  # noqa: SLF001
    )

    _forbid_srp()
    _allow_refresh(id_token="post-401-id")
    bearer = session.get_bearer()
    check(
        "after invalidate(), get_bearer() reconnects via refresh, not a full SRP login",
        condition=bearer == "post-401-id",
    )


def test_network_failure_during_refresh_counts_and_falls_back() -> None:
    session = _make_session(tokens={}, refresh_token="rt-net", consecutive_failures=0)
    _allow_refresh(error=EndpointConnectionError(endpoint_url="https://cognito-idp"))
    _allow_srp("srp-after-network-blip", refresh_token="rt-after")
    bearer = session.get_bearer()
    check(
        "EndpointConnectionError during refresh also falls back to SRP",
        condition=bearer == "srp-after-network-blip",
    )
    check(
        "EndpointConnectionError during refresh counts toward the failure counter",
        condition=session._consecutive_refresh_failures == 1,  # noqa: SLF001
    )


if __name__ == "__main__":
    test_valid_token_short_circuits()
    test_refresh_success_no_srp()
    test_refresh_rejected_once_falls_back_to_srp()
    test_refresh_rejected_twice_raises_auth_invalid_without_srp()
    test_no_refresh_token_goes_straight_to_srp()
    test_invalidate_keeps_refresh_token_and_prefers_refresh_next_time()
    test_network_failure_during_refresh_counts_and_falls_back()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for label in FAILURES:
            print(f"  - {label}")
        sys.exit(1)
    print("All checks passed.")
