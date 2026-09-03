"""
Standalone verification for card S-08 (ConfigEntryAuthFailed and re-auth).

Same convention as `verify_s06.py`/`verify_s07.py`: run by hand with
`python3 verify_s08.py`, not shipped code (see `.ruff.toml`'s
per-file-ignores).

Unlike those two, this script does NOT stub `homeassistant`: the part of
this card with real, non-trivial logic - the Cognito error-code
classification in `api/auth.py::authenticate_user` - has zero dependency on
`homeassistant` at all. `api/auth.py` is loaded directly from its file path
via `importlib` (see `_load_auth_module` below) specifically to avoid
importing it as `custom_components.radoff.api.auth`, which would execute
`custom_components/radoff/__init__.py` and `api/__init__.py` along the way -
both of which DO import `homeassistant`, not installed for this sandbox's
Python version. Loaded this way, `authenticate_user` is exercised against
the real `pycognito`/`botocore` from `requirements.txt`, using a synthetic
`botocore.exceptions.ClientError` (the same exception shape `pycognito`'s
`AWSSRP.authenticate_user()` lets propagate from `boto3`).

What this script does NOT cover, and why: `coordinator.py`'s except-clause
ordering and `config_flow.py`'s `async_step_reauth`/`async_step_reauth_confirm`
both need a real `homeassistant.helpers.update_coordinator.DataUpdateCoordinator`
/ `ConfigFlow` to exercise meaningfully - either a full `homeassistant`
install (this sandbox has none importable for this Python version) or a
hand-rolled stub thorough enough to risk diverging from real HA behaviour on
exactly the mechanics this card cares about (`context["entry_id"]`,
`async_update_entry` triggering the existing `update_listener`,
`ConfigEntryAuthFailed` being caught by the coordinator's own
`_async_refresh`). Those two files are instead checked with `py_compile` +
`ruff` (syntax and lint only) here, and need the manual on-device
verification listed in `claude/s-08-implementazione.md` before the card is
considered closed - same practice already established for S-07's AC1-AC4.
"""

import importlib.util
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

_CHECKS_RUN = 0
_CHECKS_FAILED = 0


def check(label: str, *, condition: bool) -> None:
    """Record one assertion's outcome without stopping the run on failure."""
    global _CHECKS_RUN, _CHECKS_FAILED  # noqa: PLW0603
    _CHECKS_RUN += 1
    if condition:
        print(f"[OK] {label}")
    else:
        _CHECKS_FAILED += 1
        print(f"[FAIL] {label}")


def _load_auth_module():  # noqa: ANN202
    """Load `api/auth.py` in isolation, bypassing the `custom_components` package."""
    auth_path = (
        Path(__file__).parent / "custom_components" / "radoff" / "api" / "auth.py"
    )
    spec = importlib.util.spec_from_file_location("_radoff_auth_under_test", auth_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _client_error(code: str) -> ClientError:
    """Build a synthetic ClientError shaped like a real Cognito rejection."""
    return ClientError(
        error_response={"Error": {"Code": code, "Message": f"{code} (synthetic)"}},
        operation_name="InitiateAuth",
    )


def main() -> None:
    """Run every S-08 check."""
    auth = _load_auth_module()

    def run(*, side_effect: object = None, return_value: object = None):  # noqa: ANN202
        """Call authenticate_user with AWSSRP.authenticate_user mocked out."""
        with patch.object(
            auth.AWSSRP,
            "authenticate_user",
            side_effect=side_effect,
            return_value=return_value,
        ):
            try:
                result = auth.authenticate_user(
                    username="someone@example.com",
                    password="wrong-password",
                    client_id="client",
                    pool_id="eu-west-1_example",
                    pool_region="eu-west-1",
                )
            except Exception as err:  # noqa: BLE001 - capturing for the assertion below
                return None, err
            return result, None

    # 1. A real successful login: passes the AuthenticationResult through
    #    unchanged, no exception raised.
    result, err = run(
        return_value={"AuthenticationResult": {"IdToken": "x", "ExpiresIn": 3600}}
    )
    check(
        "successful login is passed through unchanged",
        condition=err is None
        and result is not None
        and "AuthenticationResult" in result,
    )

    # 2. Wrong password -> NotAuthorizedException -> AuthInvalidError.
    _, err = run(side_effect=_client_error("NotAuthorizedException"))
    check(
        "NotAuthorizedException (wrong password) raises AuthInvalidError",
        condition=isinstance(err, auth.AuthInvalidError),
    )

    # 3. Cognito returns that same NotAuthorizedException code for a user
    #    disabled via AdminDisableUser - card S-08 explicitly names both
    #    "credenziali non più valide" and "utente disabilitato" as the
    #    definitive case, and there is no separate code to distinguish them,
    #    so this restates check #2 for the record rather than exercising a
    #    separate code path.
    check(
        "disabled-user case is covered (same NotAuthorizedException code)",
        condition=isinstance(err, auth.AuthInvalidError),
    )

    # 4. Deleted account -> UserNotFoundException -> AuthInvalidError.
    _, err = run(side_effect=_client_error("UserNotFoundException"))
    check(
        "UserNotFoundException (deleted account) raises AuthInvalidError",
        condition=isinstance(err, auth.AuthInvalidError),
    )

    # 5. A transient/unrelated ClientError (e.g. throttling) must NOT be
    #    classified as AuthInvalidError - card S-08 AC: "Un errore di rete NON
    #    apre il flusso di re-auth."
    _, err = run(side_effect=_client_error("TooManyRequestsException"))
    check(
        "throttling ClientError is NOT classified as AuthInvalidError",
        condition=isinstance(err, ClientError)
        and not isinstance(err, auth.AuthInvalidError),
    )

    # 6. A completely unrelated exception (e.g. a network-level error
    #    surfacing from boto3 that isn't even a ClientError) is not
    #    swallowed or reclassified either.
    _, err = run(side_effect=ConnectionResetError("boom"))
    check(
        "a non-ClientError exception propagates unchanged",
        condition=isinstance(err, ConnectionResetError),
    )

    print(f"\n{_CHECKS_RUN - _CHECKS_FAILED}/{_CHECKS_RUN} checks passed.")
    if _CHECKS_FAILED:
        msg = f"{_CHECKS_FAILED} check(s) failed"
        raise SystemExit(msg)


if __name__ == "__main__":
    main()
