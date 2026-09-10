"""
Transport and error-taxonomy tests for the arch 2.0 API (card M-02).

Three things are pinned here, all of them client-level and all of them
against the **real** payloads M-01 captured on dev
(`tests/fixtures/dev/error__*.json`, loaded via `load_dev_fixture`) rather
than against bodies we imagined:

1. how a request is built - one base URL, no domain header, `domain_prefix`
   always sent as a query parameter;
2. what each HTTP status raises, one test per status (401, 403, the two
   404s, 429, 5xx), plus the 429's own rule: no immediate retry, and a
   backoff that grows and then resets;
3. the two acceptance criteria that are properties of the codebase rather
   than of a call - no arch 1.x transport leftovers anywhere in the
   integration, and exactly one host in it.

Two bodies are not real fixtures, and cannot be: M-01 deliberately did not
provoke a 429 (the quota is shared with the production mobile app - T-02
D-21) nor a 5xx. Their shape comes from the backend's own answer in T-02
D-22 and is marked as such below.
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
# prefixes live only inside the M-01 fixtures these tests load, and there is
# no reason for a new one to spread further through the repo.
DOMAIN_PREFIX = "test-domain-1"

# The 429 body is not a captured fixture: provoking one would have meant
# saturating a quota shared with the Radoff mobile app, which M-01
# deliberately did not do. This is the shape the backend documented in T-02
# D-22 - produced by API Gateway itself, not by the application, which is
# also why it carries neither `error` nor `Retry-After`.
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
    """
    `domain_prefix` travels as a query param on every domain-scoped call.

    And the arch 1.x domain header is gone from all of them. Both halves
    matter: M-01 measured that a `GET /data/devices` without `domain_prefix`
    answers 200 with every device of every domain the account can reach
    (`error__devices_no_domain_prefix.json`), so omitting it silently widens
    the query instead of failing.
    """
    register_devices(requests_mock, load_dev_fixture("devices__full"))

    api = _api(monkeypatch)
    devices = api.get_devices()

    # Both devices of the captured page: card M-04 removed the type filter
    # that used to leave the `sense` out.
    assert len(devices) == 2
    assert requests_mock.request_history
    for request in requests_mock.request_history:
        assert request.qs["domain_prefix"] == [DOMAIN_PREFIX]
        assert "x-domain" not in request.headers


def test_a_client_without_a_domain_prefix_refuses_to_call(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    No domain means no request at all, not a request without the param.

    The defensive floor under the coordinator's own check (RT-2926): a
    domain-scoped endpoint called without `domain_prefix` is not a narrower
    query, it is a cross-domain read.
    """
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
    """
    A 401 invalidates the tokens and raises `AuthExpiredError` (S-08, kept).

    Card M-02 keeps this deliberately: routing an expired token straight to
    the re-auth prompt is the defect S-08 fixed. What changed in arch 2.0 is
    that this branch is now *only* about the session - the "wrong domain"
    case it used to share with the 403 is a 403 here.
    """
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
    """
    A 403 is its own class, logged as a reconfiguration, session untouched.

    Three separate ways the 403 is told apart from the 401 it used to be in
    arch 1.x: the exception class, the log line (ERROR, naming the domain and
    saying it needs a reconfiguration - the 401 logs at DEBUG and names none)
    and the fact that the Cognito tokens are left alone, because there is
    nothing wrong with them.
    """
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
    """
    Card M-03: nothing is isolated per device any more, because nothing can be.

    S-13 recorded a 404 or a 5xx on one device's own GET as a
    `DeviceFetchError` and let the rest of the poll continue. There is one
    request per cycle now (`GET /data/devices`), so any status it returns
    is the cycle's: the taxonomy class still tells the caller what happened,
    and `coordinator.py` decides what that costs.
    """
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
    """
    The other 404: an unknown `device_type`, with `available` kept on the error.

    Nothing calls `/analytics/measures-ranges` yet (the schema-driven client
    is a later card of this migration), but the semantic belongs to this
    taxonomy and the real body already exists to test it against - which is
    also why the two 404s are told apart by the body and not by the path.
    """
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
    """
    A 429 raises at once, carrying a ~5s backoff - and sends one request only.

    "No immediate retry" has two halves, and this is the behavioural one:
    the call does not re-send anything before raising. The other half - that
    urllib3 itself no longer retries a 429 - is
    `test_429_is_no_longer_in_the_urllib3_retry_list` below, because
    `requests_mock` replaces the adapter that `Retry` lives on and cannot
    observe it.
    """
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
    # The advice S-11 added is kept: name the interval actually configured.
    assert "polling interval" in str(raised.value)


def test_429_backoff_grows_per_streak_and_resets_on_success(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    Consecutive 429s double the delay; the first success starts over.

    Exponential from ~5s, as T-02 D-22 asked, with jitter subtracted - so
    each delay is asserted as a range, not as a number. The reset is what
    keeps an isolated rate limit from inheriting an exponent from an
    incident already over.
    """
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
    """
    429 is gone from `status_forcelist`; the 5xx entries stay.

    Asserted on the adapter rather than on a call: `requests_mock` mounts its
    own adapter, so no mocked request can ever exercise `Retry`. Retrying a
    429 immediately - which is what this list used to do, after a 0.5s
    backoff - is precisely what the backend asked us not to do (T-02 D-22),
    since the quota is shared and an immediate retry spends more of it.
    """
    api = API(username="user@example.com", password="hunter2")

    retries = api.session.adapters["https://"].max_retries
    assert 429 not in retries.status_forcelist
    assert {500, 502, 503, 504} <= set(retries.status_forcelist)


def test_5xx_is_a_transient_server_error(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A 5xx is its own class; since card M-03 it costs the whole cycle."""
    register_devices(requests_mock, SERVER_ERROR_BODY, status_code=503)

    api = _api(monkeypatch)
    with pytest.raises(APIServerError):
        api.get_devices()


def test_a_request_id_is_read_from_the_header_arch2_actually_sends(
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    `x-amzn-requestid` reaches the log line of a failed request.

    M-01 found it on every single response captured on dev (T-02 D-30),
    which is why it is now looked up first; the older names stay as
    fallbacks.
    """
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
    """
    A body that is not JSON at all does not derail the classification.

    Not a theoretical case: API Gateway answers some failures with plain
    text or HTML before the application is ever reached, and T-02 D-29 is
    still open on the complete shape of the error body. The status is what
    the taxonomy keys on; the body only ever adds detail.
    """
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
    """
    An unclassified non-200 keeps the pre-existing catch-all behaviour.

    `APIAuthError` itself, i.e. the base class of the whole taxonomy. Card
    M-03: S-13 used to isolate this one per device; with a single call per
    cycle it costs the cycle, like every other status.
    """
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
        # M-02: the arch 1.x domain header and base path.
        "x-domain",
        "api/v1/core",
        # M-03: the arch 1.x data model. The response buckets and the
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
    """
    M-02/M-03 AC: no arch 1.x transport or data-model leftover survives.

    Deliberately a plain text scan, comments and docstrings included: a
    reviewer checking these criteria will grep for these strings, and the
    test should agree with what the grep shows. Why each of them
    disappeared is recorded in the cards and in the migration document, not
    in a string that keeps matching that grep forever - which is also why
    the code explains the bucket model in prose rather than by naming the
    classes it deleted.
    """
    offenders = [
        path.relative_to(INTEGRATION_DIR).as_posix()
        for path in _integration_sources()
        if leftover in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_the_api_host_is_written_exactly_once() -> None:
    """
    M-02 AC: one base URL, in const.py, repeated nowhere else.

    In arch 2.0 the environment *is* the hostname, so a second host literal
    anywhere is a second environment the integration could silently talk to.
    """
    host_url = re.compile(r"https?://[\w.-]*\biot\.radoff\.life")

    occurrences = [
        (path.relative_to(INTEGRATION_DIR).as_posix(), match.group())
        for path in _integration_sources()
        for match in host_url.finditer(path.read_text(encoding="utf-8"))
    ]

    assert occurrences == [("const.py", DEFAULT_BASE_URL)]
