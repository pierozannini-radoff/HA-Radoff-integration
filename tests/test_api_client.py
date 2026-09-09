"""
Client-level tests for the composite reading key (S-10 finding C4, T-06/F2).

`RadoffDevice.readings` is keyed by `(bucket, property_name)` precisely so
that the same property arriving in two buckets produces two independent
entries instead of one silently overwriting the other. Until RT-2927 the
only property mapped in two buckets was `airqualityindex` - which the
backend never actually sends in `data`, so the fixtures had to invent one,
and inventing it is what hid T-06/F2 for a whole milestone. With the
`Bucket.DATA` AQI entry gone, `MAPPING` no longer describes any property in
two buckets, and nothing else in the suite would notice a regression to
bare-property-name keying. This file covers that directly, on the client,
without asking a fixture to lie about the payload.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.radoff.api import API
from custom_components.radoff.api import client as client_module
from custom_components.radoff.api.models import Bucket
from custom_components.radoff.properties import MAPPING

from .conftest import (
    auth_result,
    load_device_fixture,
    make_id_token,
    patch_authenticate_user,
    register_device,
)

DOMAIN_ID = "aaaaaaaa-0000-0000-0000-000000000001"
DEVICE_ID = "device-0000-0001"


def test_one_property_in_two_buckets_yields_two_readings(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """`data.tvoc` and `aggregatedData.tvoc` are two entries, not one."""
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))

    two_bucket_mapping = {
        Bucket.DATA: MAPPING[Bucket.DATA],
        Bucket.AGGREGATED: {
            **MAPPING[Bucket.AGGREGATED],
            "tvoc": {
                "deviceClass": SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS,
                "friendlyName": "VOC",
                "unit": "µg/m³",
            },
        },
    }
    monkeypatch.setattr(client_module, "MAPPING", two_bucket_mapping)

    payload = load_device_fixture("device_0001_nominal.json")
    payload["data"]["aggregatedData"].append(
        {"propertyName": "tvoc", "aggregationValue": 77}
    )
    register_device(requests_mock, DEVICE_ID, payload)

    api = API(username="user@example.com", password="hunter2", domain_id=DOMAIN_ID)
    readings, _ = api._get_data(DEVICE_ID)  # noqa: SLF001

    assert readings[(Bucket.DATA, "tvoc")].value == 50
    assert readings[(Bucket.AGGREGATED, "tvoc")].value == 77
    assert readings[(Bucket.DATA, "tvoc")].bucket is Bucket.DATA
    assert readings[(Bucket.AGGREGATED, "tvoc")].bucket is Bucket.AGGREGATED


def test_aqi_comes_only_from_the_aggregated_bucket(
    monkeypatch: pytest.MonkeyPatch, requests_mock: Any
) -> None:
    """
    A `data.airqualityindex`, if the backend ever sent one, is not read.

    The real payload has never carried it (T-06/F2): `MAPPING` describes the
    AQI in `Bucket.AGGREGATED` only, and this pins that a stray one in `data`
    produces no second reading for the same property.
    """
    patch_authenticate_user(monkeypatch, result=auth_result(make_id_token([DOMAIN_ID])))

    payload = load_device_fixture("device_0001_nominal.json")
    payload["data"]["data"].append({"propertyName": "airqualityindex", "value": 20})
    register_device(requests_mock, DEVICE_ID, payload)

    api = API(username="user@example.com", password="hunter2", domain_id=DOMAIN_ID)
    readings, _ = api._get_data(DEVICE_ID)  # noqa: SLF001

    assert (Bucket.DATA, "airqualityindex") not in readings
    assert readings[(Bucket.AGGREGATED, "airqualityindex")].value == 25
