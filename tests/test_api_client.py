"""
Client-level tests for the arch 2.0 device call and data model (card M-03).

Everything here runs against the **real** payloads M-01 captured on dev
(`tests/fixtures/dev/devices__*.json`, loaded with `load_dev_fixture`),
because the shape of `telemetry` is the whole point of this card and a
hand-written fixture would only prove that the client agrees with whoever
wrote it. Where a case the dev account could not produce is needed - a
`nowplus` with `telemetry: null`, a second page, a nested LIFE - it is
derived from those same real objects rather than invented, and the
derivation is stated in the test.

What is pinned here, in order: the flat telemetry block becomes readings
keyed by field name, the serial is the identity, every reading carries the
block's timestamp, `telemetry: null` is data-less and not an error, the
type filter, the pagination loop and its two guards, and the nested LIFE
that must be logged rather than dropped.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import pytest

from custom_components.radoff.api import API
from custom_components.radoff.api.client import MAX_DEVICE_PAGES

from .conftest import (
    auth_result,
    load_dev_fixture,
    make_id_token,
    patch_authenticate_user,
    register_devices,
)

DOMAIN_PREFIX = "test-domain-1"

# The one real device of the dev domain that actually reports (M-01,
# "Esito 5"): a `nowplus`, `connection_status: connected`, with a full
# telemetry block. Its companion in the same fixture is a `sense` with
# `telemetry: null`, which is the other half of what these tests need.
REPORTING_SERIAL = "3D90E0"
SILENT_SERIAL = "57FA28"

# The 9 measurements `telemetry` carries on that device - i.e. its keys
# minus the two that are not measurements (`timestamp`, `device_type`).
EXPECTED_FIELDS = {
    "aqi_value",
    "eco2",
    "internal_temperature",
    "pm1",
    "pm10",
    "pm25",
    "pressure",
    "relative_humidity",
    "tvoc",
}


def _api(monkeypatch: pytest.MonkeyPatch) -> API:
    """Return an authenticated client whose Cognito handshake is mocked away."""
    patch_authenticate_user(
        monkeypatch, result=auth_result(make_id_token([DOMAIN_PREFIX]))
    )
    return API(
        username="user@example.com",
        password="hunter2",
        domain_prefix=DOMAIN_PREFIX,
    )


def _real_page() -> dict[str, Any]:
    """A mutable copy of the real one-page device list captured on dev."""
    return copy.deepcopy(load_dev_fixture("devices__full"))


def _devices_of(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a page's raw devices by serial, for readable mutation in tests."""
    return {device["serial_number"]: device for device in page["devices"]}


# ---------------------------------------------------------------------------
# The flat telemetry block
# ---------------------------------------------------------------------------


def test_telemetry_becomes_readings_keyed_by_field_name(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    Every measurement of `telemetry` is one reading, keyed by its field name.

    The composite `(bucket, property)` key of S-10 is gone with the buckets
    that made it necessary: arch 2.0 sends one flat object holding the last
    value of each field, so the key is the field name (card M-03).
    """
    register_devices(requests_mock, _real_page())

    devices = _api(monkeypatch).get_devices()

    # Both devices of the page, since card M-04 removed the type filter.
    assert len(devices) == 2
    device = next(d for d in devices if d.serial_number == REPORTING_SERIAL)
    assert set(device.readings) == EXPECTED_FIELDS
    assert device.readings["internal_temperature"].value == 26.0583
    assert device.readings["pressure"].value == 100488.0
    # Every reading knows its own name, and it is the key it is filed under.
    assert all(field == reading.name for field, reading in device.readings.items())


def test_the_serial_is_the_identity_and_the_new_fields_are_carried(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    `serial_number` identifies the device, and the 2.0-only fields reach the model.

    `connection_status` is read but not consumed here - availability based
    on it is card M-06 - which is exactly why it is worth pinning now: the
    card that uses it should find it already in the model.
    """
    register_devices(requests_mock, _real_page())

    device = _api(monkeypatch).get_devices()[0]

    assert device.serial_number == REPORTING_SERIAL
    assert device.device_type == "nowplus"
    assert device.connection_status == "connected"
    assert device.connection_status_updated_at is not None
    assert device.firmware_version == "0.2.8"
    assert device.domain_prefix == "875fe89b"
    assert device.room_name and device.building_name
    # Both halves of each pair: the label a person wrote and the stable
    # slug that survives a rename.
    assert device.room_slug == "default-room"
    assert device.building_slug == "default-building"
    assert device.stale is False


def test_every_reading_carries_the_blocks_own_timestamp(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    `measured_at` is the telemetry block's timestamp, the same for every field.

    Not a detail: T-08 D-15 says that timestamp is the *maximum* of the
    fields' own timestamps, so a slow field (radon) carries a `measured_at`
    refreshed by its faster siblings. Pinning the identity here is what
    keeps that property visible instead of letting a future change make
    `measured_at` look per-field without being it.
    """
    register_devices(requests_mock, _real_page())

    device = _api(monkeypatch).get_devices()[0]

    measured_at = {reading.measured_at for reading in device.readings.values()}
    assert len(measured_at) == 1
    assert measured_at.pop() == device.telemetry_timestamp
    assert device.telemetry_timestamp is not None
    assert device.telemetry_timestamp.tzinfo is not None


def test_telemetry_null_is_no_data_and_not_an_error(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    M-03 AC: a device with `telemetry: null` is `stale=True` and raises nothing.

    Asserted on the real silent device of the capture, a `sense` - which
    card M-04 stopped filtering out, so the null no longer has to be moved
    onto another device to be testable. The shape is the captured one (M-01,
    D-16: the majority of the 120 devices censused are in this state).
    """
    register_devices(requests_mock, _real_page())

    devices = _api(monkeypatch).get_devices()
    silent = next(d for d in devices if d.serial_number == SILENT_SERIAL)

    assert silent.stale is True
    assert silent.readings == {}
    assert silent.telemetry_timestamp is None
    # The identity fields still arrive: a silent device is still a device.
    assert silent.device_type == "sense"


def test_a_device_of_another_type_is_kept(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    M-04 AC: `type` is a schema cache key, not an eligibility test.

    The `sense` of the real fixture used to be dropped here, because
    nothing in the code could describe another type's telemetry. The schema
    is served per type now (`get_measures_ranges`), so discarding a device
    the API returned would be throwing away data we can render - and the
    card's acceptance criterion forbids it in as many words. What stays
    narrow is the promise: Now+ is the supported type (README), not a
    filter.
    """
    register_devices(requests_mock, _real_page())

    devices = _api(monkeypatch).get_devices()
    serials = {device.serial_number for device in devices}

    assert serials == {REPORTING_SERIAL, SILENT_SERIAL}
    assert {device.device_type for device in devices} == {"nowplus", "sense"}


def test_a_device_with_no_type_keeps_an_empty_type_and_survives(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A missing `type` leaves `device_type` an empty string, not `None`."""
    page = _real_page()
    del _devices_of(page)[REPORTING_SERIAL]["type"]
    register_devices(requests_mock, page)

    device = next(
        d
        for d in _api(monkeypatch).get_devices()
        if d.serial_number == REPORTING_SERIAL
    )

    assert device.device_type == ""
    assert set(device.readings) == EXPECTED_FIELDS


def test_non_measurement_telemetry_keys_never_become_readings(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    `timestamp` and `device_type` are in the block but are not measurements.

    A non-numeric field the backend might add later is skipped for the same
    reason, rather than becoming a reading whose value no sensor can show.
    """
    page = _real_page()
    _devices_of(page)[REPORTING_SERIAL]["telemetry"]["radon_status"] = "ok"
    register_devices(requests_mock, page)

    readings = _api(monkeypatch).get_devices()[0].readings

    assert "timestamp" not in readings
    assert "device_type" not in readings
    assert "radon_status" not in readings
    assert set(readings) == EXPECTED_FIELDS


def test_fields_are_read_by_name_and_never_by_position(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    M-03 AC: reversing the order of the payload's keys changes nothing.

    JSON objects have no meaningful order, and the client must not acquire
    a dependency on the one the backend happens to serialize today.
    """
    ordered = _real_page()
    reversed_page = _real_page()
    telemetry = _devices_of(reversed_page)[REPORTING_SERIAL]["telemetry"]
    _devices_of(reversed_page)[REPORTING_SERIAL]["telemetry"] = dict(
        reversed(list(telemetry.items()))
    )

    register_devices(requests_mock, ordered)
    from_ordered = _api(monkeypatch).get_devices()[0].readings

    register_devices(requests_mock, reversed_page)
    from_reversed = _api(monkeypatch).get_devices()[0].readings

    assert {field: reading.value for field, reading in from_ordered.items()} == {
        field: reading.value for field, reading in from_reversed.items()
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def _two_pages() -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Two pages built from the real device object, with a real `pagination` shape.

    The dev domain holds 2 devices, so its captured pages cannot show a
    walk across more than one (M-01 says so itself, "Una riserva
    sull'evidenza di V1"). The second device here is the same real object
    under another serial, which is enough to prove the loop follows
    `total_pages` rather than stopping at the first response.
    """
    first = _real_page()
    first["pagination"] = {"page": 1, "page_size": 2, "total": 3, "total_pages": 2}

    second = _real_page()
    second_devices = _devices_of(second)
    second_devices[REPORTING_SERIAL]["serial_number"] = "3D90E1"
    second["devices"] = [second_devices[REPORTING_SERIAL]]
    second["pagination"] = {"page": 2, "page_size": 2, "total": 3, "total_pages": 2}

    return first, second


def test_the_loop_follows_total_pages(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A device on page 2 is collected: the client does not stop at page 1."""
    register_devices(requests_mock, *_two_pages())

    devices = _api(monkeypatch).get_devices()

    assert {device.serial_number for device in devices} == {
        REPORTING_SERIAL,
        SILENT_SERIAL,
        "3D90E1",
    }
    assert [request.qs["page"] for request in requests_mock.request_history] == [
        ["1"],
        ["2"],
    ]


def test_one_page_costs_exactly_one_request(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    A single-page response ends the loop, and the whole cycle is one call.

    The premise this card removes `DeviceFetchError` on: with one request
    per cycle there is no per-device failure left to isolate.
    """
    register_devices(requests_mock, _real_page())

    _api(monkeypatch).get_devices()

    assert len(requests_mock.request_history) == 1
    assert requests_mock.request_history[0].qs["page_size"] == ["200"]


def test_a_serial_repeated_across_pages_is_collected_once(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    A device seen twice while walking the pages produces one device, not two.

    M-01 measured the ordering as stable (V1), so a repeat means the list
    shifted between two requests - and two `RadoffDevice` with the same
    serial would mean two entity sets for one device.
    """
    first, second = _two_pages()
    _devices_of(second)["3D90E1"]["serial_number"] = REPORTING_SERIAL

    register_devices(requests_mock, first, second)
    devices = _api(monkeypatch).get_devices()

    assert [device.serial_number for device in devices] == [
        REPORTING_SERIAL,
        SILENT_SERIAL,
    ]


def test_a_never_ending_pagination_stops_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A `total_pages` that never terminates costs a bounded cycle, not a hang.

    The loop runs inside an executor thread, where nothing else would stop
    it: `asyncio.timeout` in the coordinator bounds how long HA *waits*, not
    how long the thread runs.
    """
    endless = _real_page()
    endless["pagination"] = {
        "page": 1,
        "page_size": 200,
        "total": 10_000,
        "total_pages": 9_999,
    }
    register_devices(requests_mock, endless)

    with caplog.at_level(logging.WARNING, logger="custom_components.radoff.api.client"):
        devices = _api(monkeypatch).get_devices()

    assert len(requests_mock.request_history) == MAX_DEVICE_PAGES
    # The same two devices over and over, deduplicated by serial.
    assert len(devices) == 2
    assert "ceiling" in caplog.text


# ---------------------------------------------------------------------------
# The nested LIFE
# ---------------------------------------------------------------------------


def test_a_nested_life_is_logged_and_produces_no_device(
    monkeypatch: pytest.MonkeyPatch,
    requests_mock: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    M-03 AC: a nested LIFE yields one log line, no entity and no exception.

    The nesting is real (M-01, D-33): a `city` carries the whole object of
    the `life` it controls inline, and that nested device can belong to a
    different domain than its parent. The dev fixtures in the repo have no
    example - the pass that found one was discarded for redaction reasons -
    so the shape is rebuilt here from the real device object, as D-33
    describes it. Modelling the pair is T-08 D-33; this card only refuses
    to lose it silently.
    """
    page = _real_page()
    controller = copy.deepcopy(_devices_of(page)[REPORTING_SERIAL])
    slave = copy.deepcopy(_devices_of(page)[SILENT_SERIAL])

    slave["serial_number"] = "855894"
    slave["type"] = "life"
    slave["domain_prefix"] = "9a8e7728"
    slave["managed_by_device_serial"] = "E754F0"

    controller["serial_number"] = "E754F0"
    controller["type"] = "city"
    controller["controller_of_device_serial"] = "855894"
    controller["controller_of_device"] = slave

    page["devices"] = [*page["devices"], controller]
    register_devices(requests_mock, page)

    with caplog.at_level(logging.INFO, logger="custom_components.radoff.api.client"):
        devices = _api(monkeypatch).get_devices()

    # The controller is a device like any other since M-04 dropped the type
    # filter - what stays out is the *nested* one, which is only reported.
    assert {device.serial_number for device in devices} == {
        REPORTING_SERIAL,
        SILENT_SERIAL,
        "E754F0",
    }
    assert "855894" in caplog.text
    assert "does not model" in caplog.text
