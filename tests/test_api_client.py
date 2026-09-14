"""
The device call and the data model it builds, against real dev payloads.

Covers: telemetry to readings, the serial as identity, `telemetry: null`,
device types, the pagination loop and its guards, the nested LIFE.
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

# The one real device of the dev domain that actually reports: a `nowplus`,
# `connection_status: connected`, with a full telemetry block. Its companion
# in the same fixture is a `sense` with `telemetry: null`, which is the
# other half of what these tests need.
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
    """Every measurement of `telemetry` is one reading, keyed by its field name."""
    register_devices(requests_mock, _real_page())

    devices = _api(monkeypatch).get_devices()

    # Both devices of the page: there is no type filter.
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
    """`serial_number` identifies the device, and the 2.0-only fields reach the model."""
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
    """`measured_at` is the block's timestamp, identical on every reading."""
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
    """A device with `telemetry: null` is `stale=True` and raises nothing."""
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
    """`type` is a schema cache key: no device the API returned is discarded."""
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
    """`timestamp`, `device_type` and any non-numeric field become no reading."""
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
    """Reversing the order of the payload's keys changes nothing."""
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
    """Two pages built from the real device object, with a real `pagination` shape."""
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
    """A single-page response ends the loop, and the whole cycle is one call."""
    register_devices(requests_mock, _real_page())

    _api(monkeypatch).get_devices()

    assert len(requests_mock.request_history) == 1
    assert requests_mock.request_history[0].qs["page_size"] == ["200"]


def test_a_serial_repeated_across_pages_is_collected_once(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """A serial seen twice while walking the pages produces one device, not two."""
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
    """A `total_pages` that never terminates costs a bounded cycle, not a hang."""
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
    """A nested LIFE yields one log line, no device and no exception."""
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

    # The controller is a device like any other; what stays out is the
    # *nested* one, which is only reported.
    assert {device.serial_number for device in devices} == {
        REPORTING_SERIAL,
        SILENT_SERIAL,
        "E754F0",
    }
    assert "855894" in caplog.text
    assert "does not model" in caplog.text
