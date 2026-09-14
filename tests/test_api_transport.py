"""
How a request is built, and what each HTTP status raises.

Covers: `domain_prefix` and base URL, the taxonomy per status, the 429
backoff, and the codebase-wide checks on transport leftovers and hosts.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest

from custom_components.radoff.api import API
from custom_components.radoff.api.exceptions import (
    APIAuthError,
    APIDeviceNotFoundError,
    APIDomainAccessError,
    APIRateLimitError,
    APIServerError,
    APIUnknownDeviceTypeError,
    DomainNotFoundError,
)
from custom_components.radoff.api.auth import AuthExpiredError
from custom_components.radoff.const import (
    DEFAULT_BASE_URL,
    RATE_LIMIT_BACKOFF_JITTER,
    RATE_LIMIT_BACKOFF_START,
)

from .conftest import (
    BASE_URL,
    auth_result,
    load_dev_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
)

# Synthetic, like every other identifier this suite invents: the real
# prefixes live only inside the dev fixtures these tests load, and there is
# no reason for a new one to spread further through the repo.
DOMAIN_PREFIX = "test-domain-1"

# The 429 body is not a captured fixture: provoking one would have meant
# saturating a quota shared with the Radoff mobile app. This is the shape
# the backend documented - produced by API Gateway itself, not by the
# application, which is also why it carries neither `error` nor
# `Retry-After`.
RATE_LIMIT_BODY = {"message": "Too Many Requests"}

# Same reason: no captured 5xx. A body is included only so the client's
# body parsing runs on something; the classification is driven by status.
SERVER_ERROR_BODY = {"message": "Internal Server Error"}

INTEGRATION_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "radoff"
)


def _api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    domain_prefix: str = DOMAIN_PREFIX,
    **kwargs: Any,
) -> API:
    """Return an authenticated client whose Cognito handshake is mocked away."""
    patch_authenticate_user(
        monkeypatch, result=auth_result(make_id_token([DOMAIN_PREFIX]))
    )
    return API(
        username="user@example.com",
        password="hunter2",
        domain_prefix=domain_prefix,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# How a request is built
# ---------------------------------------------------------------------------


def test_every_request_carries_domain_prefix_and_no_domain_header(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """`domain_prefix` travels as a query param on every domain-scoped call."""
    register_devices(requests_mock, load_dev_fixture("devices__full"))

    api = _api(monkeypatch)
    devices = api.get_devices()

    # Both devices of the captured page: there is no type filter.
    assert len(devices) == 2
    assert requests_mock.request_history
    for request in requests_mock.request_history:
        assert request.qs["domain_prefix"] == [DOMAIN_PREFIX]
        assert "x-domain" not in request.headers


def test_a_client_without_a_domain_prefix_refuses_to_call(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """No domain means no request at all, not a request without the param."""
    api = _api(monkeypatch, domain_prefix="")

    with pytest.raises(DomainNotFoundError):
        api.get_devices()

    assert requests_mock.request_history == []


def test_the_base_url_option_moves_every_request(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """An overridden base URL is where the requests go, trailing slash and all."""
    other_host = "https://api.int.iot.radoff.life"
    register_devices(
        requests_mock, load_dev_fixture("devices__full"), base_url=other_host
    )

    api = _api(monkeypatch, base_url=f"{other_host}/")
    api.get_devices()

    assert api.base_url == other_host
    assert requests_mock.request_history[-1].netloc == "api.int.iot.radoff.life"


# ---------------------------------------------------------------------------
# The error taxonomy, one test per status
# ---------------------------------------------------------------------------


def test_401_expires_the_session_and_stays_recoverable(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A 401 invalidates the tokens and raises `AuthExpiredError`."""
    register_devices(
        requests_mock,
        load_dev_fixture("error__unauthenticated"),
        status_code=401,
    )

    api = _api(monkeypatch)
    with pytest.raises(AuthExpiredError):
        api.get_devices()

    assert not api._session.tokens  # noqa: SLF001


def test_403_is_a_domain_access_error_distinguishable_from_a_401(
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 403 is its own class, logged as a reconfiguration, session untouched."""
    register_devices(
        requests_mock,
        load_dev_fixture("error__devices_foreign_domain"),
        status_code=403,
    )

    api = _api(monkeypatch)
    api.connect()

    with caplog.at_level(logging.ERROR, logger="custom_components.radoff.api.client"):
        with pytest.raises(APIDomainAccessError) as raised:
            api.get_devices()

    assert not isinstance(raised.value, AuthExpiredError)
    assert api._session.tokens is not None  # noqa: SLF001

    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )
    assert DOMAIN_PREFIX in logged
    assert "reconfiguration" in logged
    assert "you do not belong to domain" in logged  # the backend's own detail


def test_404_on_the_device_list_is_a_not_found_and_costs_the_cycle(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A 404 on the device list is a not-found and costs the whole cycle."""
    register_devices(
        requests_mock,
        load_dev_fixture("error__device_detail_unknown_serial"),
        status_code=404,
    )

    api = _api(monkeypatch)
    with pytest.raises(APIDeviceNotFoundError):
        api.get_devices()

    assert len(requests_mock.request_history) == 1


def test_404_on_measures_ranges_carries_the_valid_device_types(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """The 404 for an unknown `device_type` keeps `available` on the error."""
    url = f"{BASE_URL}/analytics/measures-ranges"
    requests_mock.get(
        url,
        json=load_dev_fixture("error__measures_ranges_unknown_type"),
        status_code=404,
    )

    api = _api(monkeypatch)
    response = api.session.get(url, params={"device_type": "not-a-real-device-type"})

    with pytest.raises(APIUnknownDeviceTypeError) as raised:
        api._check_response_status(response=response, url=url)  # noqa: SLF001

    assert raised.value.available == ["city", "now", "nowplus", "sense", "sismoff"]


def test_429_backs_off_from_five_seconds_without_retrying(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A 429 raises at once carrying a ~5s backoff, and sends one request only."""
    register_devices(requests_mock, RATE_LIMIT_BODY, status_code=429)

    api = _api(monkeypatch)
    with pytest.raises(APIRateLimitError) as raised:
        api.get_devices()

    assert len(requests_mock.request_history) == 1
    assert (
        RATE_LIMIT_BACKOFF_START * (1 - RATE_LIMIT_BACKOFF_JITTER)
        <= raised.value.retry_after
        <= RATE_LIMIT_BACKOFF_START
    )
    # The message names the polling interval actually configured.
    assert "polling interval" in str(raised.value)


def test_429_backoff_grows_per_streak_and_resets_on_success(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """Consecutive 429s double the delay; the first success starts over."""
    register_devices(requests_mock, RATE_LIMIT_BODY, status_code=429)
    api = _api(monkeypatch)

    delays = []
    for _ in range(3):
        with pytest.raises(APIRateLimitError) as raised:
            api.get_devices()
        delays.append(raised.value.retry_after)

    for attempt, delay in enumerate(delays):
        nominal = RATE_LIMIT_BACKOFF_START * 2**attempt
        assert nominal * (1 - RATE_LIMIT_BACKOFF_JITTER) <= delay <= nominal

    register_devices(requests_mock, load_dev_fixture("devices__full"))
    api.get_devices()

    register_devices(requests_mock, RATE_LIMIT_BODY, status_code=429)
    with pytest.raises(APIRateLimitError) as raised:
        api.get_devices()
    assert raised.value.retry_after <= RATE_LIMIT_BACKOFF_START


def test_429_is_no_longer_in_the_urllib3_retry_list() -> None:
    """429 is gone from urllib3's `status_forcelist`; the 5xx entries stay."""
    api = API(username="user@example.com", password="hunter2")

    retries = api.session.adapters["https://"].max_retries
    assert 429 not in retries.status_forcelist
    assert {500, 502, 503, 504} <= set(retries.status_forcelist)


def test_5xx_is_a_transient_server_error(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A 5xx raises `APIServerError`, its own class in the taxonomy."""
    register_devices(requests_mock, SERVER_ERROR_BODY, status_code=503)

    api = _api(monkeypatch)
    with pytest.raises(APIServerError):
        api.get_devices()


def test_a_request_id_is_read_from_the_header_arch2_actually_sends(
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`x-amzn-requestid` reaches the log line of a failed request."""
    requests_mock.get(
        f"{BASE_URL}/data/devices",
        json=load_dev_fixture("error__unauthenticated"),
        status_code=401,
        headers={"x-amzn-requestid": "a4cf89d5-request-id"},
    )

    api = _api(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="custom_components.radoff.api.client"):
        with pytest.raises(AuthExpiredError):
            api.get_devices()

    assert "a4cf89d5-request-id" in caplog.text


def test_an_unparsable_error_body_is_still_classified_by_status(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A body that is not JSON is still classified by its status."""
    requests_mock.get(
        f"{BASE_URL}/data/devices",
        text="<html><body>502 Bad Gateway</body></html>",
        status_code=502,
    )

    api = _api(monkeypatch)
    with pytest.raises(APIServerError):
        api.get_devices()


def test_a_status_outside_the_taxonomy_stays_a_generic_error(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A status outside the taxonomy raises the base class, `APIAuthError`."""
    register_devices(requests_mock, {"error": "teapot"}, status_code=418)

    api = _api(monkeypatch)
    with pytest.raises(APIAuthError) as raised:
        api.get_devices()

    assert type(raised.value) is APIAuthError


# ---------------------------------------------------------------------------
# Acceptance criteria that are properties of the codebase
# ---------------------------------------------------------------------------


def _integration_sources() -> list[Path]:
    return sorted(
        path
        for path in INTEGRATION_DIR.rglob("*")
        if path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
    )


@pytest.mark.parametrize(
    "leftover",
    [
        # The arch 1.x domain header and base path.
        "x-domain",
        "api/v1/core",
        # The arch 1.x data model: the response buckets and the
        # composite reading key built on them, the two bucket names the
        # payload used to carry, and the scaling factor `internal_
        # temperature` was multiplied by - arch 2.0 sends every value in
        # the unit it declares, so a survivor here is a conversion being
        # invented again.
        "Bucket",
        "ReadingKey",
        "aggregatedData",
        "recalculatedData",
        "0.00835",
    ],
)
def test_no_arch1_transport_leftovers_in_the_integration(leftover: str) -> None:
    """No arch 1.x transport or data-model name survives anywhere, comments included."""
    offenders = [
        path.relative_to(INTEGRATION_DIR).as_posix()
        for path in _integration_sources()
        if leftover in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_api_host_is_written_exactly_once() -> None:
    """The API host is written once, in const.py, and nowhere else."""
    host_url = re.compile(r"https?://[\w.-]*\biot\.radoff\.life")

    occurrences = [
        (path.relative_to(INTEGRATION_DIR).as_posix(), match.group())
        for path in _integration_sources()
        for match in host_url.finditer(path.read_text(encoding="utf-8"))
    ]

    assert occurrences == [("const.py", DEFAULT_BASE_URL)]
