#!/usr/bin/env python3
"""
Standalone verification for card S-09 (Cognito challenge/exception mapping).

Exercises `custom_components/radoff/api/auth.py::authenticate_user` in
isolation, the same way `verify_s06.py`/`verify_s07.py`/`verify_s08.py` did
for their own cards: this module has no dependency on `homeassistant` (only
`botocore` and `pycognito`), so it is loaded directly via `importlib` from
its file path, bypassing `custom_components/radoff/__init__.py` and
`custom_components/radoff/api/__init__.py` (both of which pull in
`homeassistant`, not required to exercise this module's own logic).

This covers the part of card S-09 that can be checked without a running
Home Assistant instance: every classification `authenticate_user()` is now
responsible for (challenge detection, `None` handling, definitive-vs-
transient Cognito errors, unreachable endpoint, and "don't misclassify
anything else"). It does NOT exercise `config_flow.py` (needs
`homeassistant`) or the unique_id normalization in
`_async_create_entry` - see the manual/py_compile steps in
`s-09-implementazione.md` for those.

Not shipped code (see `hacs.json`/`.github/workflows/release.yml`: only
`custom_components/radoff` is packaged) - excluded from ruff via
`.ruff.toml`'s per-file-ignores.

Run with:
    python3 verify_s09.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).parent
AUTH_MODULE_PATH = REPO_ROOT / "custom_components" / "radoff" / "api" / "auth.py"

_FAILURES: list[str] = []


def _load_auth_module():
    """Import auth.py by file path, without going through the `api` package."""
    spec = importlib.util.spec_from_file_location(
        "radoff_auth_under_test", AUTH_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    suffix = f" - {detail}" if detail and not condition else ""
    print(f"[{status}] {label}{suffix}")
    if not condition:
        _FAILURES.append(label)


def _client_error(code: str, message: str = "boom"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "InitiateAuth")


def main() -> int:  # noqa: PLR0915
    auth = _load_auth_module()

    common_kwargs = {
        "username": "someone@example.com",
        "password": "hunter2",
        "client_id": "client-id",
        "pool_id": "pool-id",
        "pool_region": "eu-west-1",
    }

    # 1. Successful authentication returns the raw dict with AuthenticationResult.
    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        return_value={"AuthenticationResult": {"IdToken": "x", "ExpiresIn": 3600}},
    ):
        result = auth.authenticate_user(**common_kwargs)
        _check(
            "successful login returns AuthenticationResult",
            isinstance(result, dict) and "AuthenticationResult" in result,
        )

    # 2. NEW_PASSWORD_REQUIRED challenge: no AuthenticationResult key (C8).
    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        return_value={
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "ChallengeParameters": {},
        },
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthChallengeRequiredError as err:
            _check(
                "NEW_PASSWORD_REQUIRED raises AuthChallengeRequiredError",
                err.challenge_name == "NEW_PASSWORD_REQUIRED",
                f"challenge_name={err.challenge_name!r}",
            )
        except Exception as err:  # noqa: BLE001
            _check(
                "NEW_PASSWORD_REQUIRED raises AuthChallengeRequiredError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check(
                "NEW_PASSWORD_REQUIRED raises AuthChallengeRequiredError",
                False,
                "no exception raised",
            )

    # 3. An MFA challenge follows the same shape, different name.
    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        return_value={"ChallengeName": "SMS_MFA", "ChallengeParameters": {}},
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthChallengeRequiredError as err:
            _check(
                "SMS_MFA raises AuthChallengeRequiredError",
                err.challenge_name == "SMS_MFA",
            )
        except Exception as err:  # noqa: BLE001
            _check(
                "SMS_MFA raises AuthChallengeRequiredError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check("SMS_MFA raises AuthChallengeRequiredError", False, "no exception")

    # 4. auth_data is None.
    with mock.patch.object(auth.AWSSRP, "authenticate_user", return_value=None):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthInvalidError:
            _check("None auth_data raises AuthInvalidError", True)
        except Exception as err:  # noqa: BLE001
            _check(
                "None auth_data raises AuthInvalidError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check("None auth_data raises AuthInvalidError", False, "no exception")

    # 5. Wrong password / disabled user -> NotAuthorizedException.
    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        side_effect=_client_error("NotAuthorizedException"),
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthInvalidError:
            _check("NotAuthorizedException raises AuthInvalidError", True)
        except Exception as err:  # noqa: BLE001
            _check(
                "NotAuthorizedException raises AuthInvalidError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check(
                "NotAuthorizedException raises AuthInvalidError", False, "no exception"
            )

    # 6. Deleted account -> UserNotFoundException.
    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        side_effect=_client_error("UserNotFoundException"),
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthInvalidError:
            _check("UserNotFoundException raises AuthInvalidError", True)
        except Exception as err:  # noqa: BLE001
            _check(
                "UserNotFoundException raises AuthInvalidError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check(
                "UserNotFoundException raises AuthInvalidError", False, "no exception"
            )

    # 7. Throttling -> TooManyRequestsException must NOT be treated as invalid_auth.
    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        side_effect=_client_error("TooManyRequestsException"),
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthUnavailableError:
            _check("TooManyRequestsException raises AuthUnavailableError", True)
        except Exception as err:  # noqa: BLE001
            _check(
                "TooManyRequestsException raises AuthUnavailableError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check(
                "TooManyRequestsException raises AuthUnavailableError",
                False,
                "no exception",
            )

    # 8. Network unreachable -> EndpointConnectionError (not a ClientError at all).
    from botocore.exceptions import EndpointConnectionError

    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        side_effect=EndpointConnectionError(
            endpoint_url="https://cognito-idp.eu-west-1.amazonaws.com/"
        ),
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except auth.AuthUnavailableError:
            _check("EndpointConnectionError raises AuthUnavailableError", True)
        except Exception as err:  # noqa: BLE001
            _check(
                "EndpointConnectionError raises AuthUnavailableError",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check(
                "EndpointConnectionError raises AuthUnavailableError",
                False,
                "no exception",
            )

    # 9. An unrelated ClientError code must propagate unchanged, not be
    # misclassified as one of our new exceptions (over-broad mapping check).
    from botocore.exceptions import ClientError

    with mock.patch.object(
        auth.AWSSRP,
        "authenticate_user",
        side_effect=_client_error("InvalidParameterException"),
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except (
            auth.AuthInvalidError,
            auth.AuthUnavailableError,
            auth.AuthChallengeRequiredError,
        ) as err:
            _check(
                "unrelated ClientError propagates unchanged",
                False,
                f"misclassified as {type(err)}",
            )
        except ClientError:
            _check("unrelated ClientError propagates unchanged", True)
        else:
            _check(
                "unrelated ClientError propagates unchanged", False, "no exception"
            )

    # 10. A wholly unrelated exception type must also propagate unchanged.
    with mock.patch.object(
        auth.AWSSRP, "authenticate_user", side_effect=ValueError("boom")
    ):
        try:
            auth.authenticate_user(**common_kwargs)
        except ValueError:
            _check("unrelated exception propagates unchanged", True)
        except Exception as err:  # noqa: BLE001
            _check(
                "unrelated exception propagates unchanged",
                False,
                f"raised {type(err)} instead",
            )
        else:
            _check("unrelated exception propagates unchanged", False, "no exception")

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} check(s) FAILED: {', '.join(_FAILURES)}")
        return 1
    print("All checks PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
