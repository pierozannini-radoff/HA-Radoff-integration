"""
Schema tests (card M-04): `measures-ranges` -> `MeasureSpec`.

Everything this integration used to hardcode - units, labels, thresholds,
the vocabulary of the qualitative bands - now comes from the API, so this
file pins the reading of that response rather than the values themselves.
Where a value does appear below it is quoted from a real fixture captured
on dev (`tests/fixtures/dev/measures_ranges__*.json`), never retyped from
the tables M-04 deleted: a test that restated them would recreate the very
thing the card removes, one layer down.

The `pos` cases are the heart of it. The backend warned that `pos` is
sparse and must be used to sort, never to index, so both the real sparse
fixtures and a deliberately broken one (gaps, duplicates, a missing `pos`)
are exercised here.
"""

from __future__ import annotations

import logging

import pytest
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.radoff.schema import (
    DEVICE_CLASSES,
    HA_UNITS,
    Band,
    MeasureSpec,
    build_specs,
    resolve_ha_unit,
)

from .conftest import load_dev_fixture

# Every type the catalogue holds, as M-01 captured it. `life` is excluded:
# the fixture of that name is the 404 body dev answered with, not a schema.
#
# `sismoff` *is* here, and only here. Its schema is read like every other
# type's - that costs nothing and keeps `co`/`ch4` covered - but the card
# excludes it from the entity-level checks, because M-01 captured no
# sismoff device to run them against (decision of 2026-09-10).
SCHEMA_TYPES = ("nowplus", "sense", "city", "now", "sismoff")


def _specs(device_type: str) -> dict[str, MeasureSpec]:
    return build_specs(
        load_dev_fixture(f"measures_ranges__{device_type}"), device_type=device_type
    )


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_measures_are_ordered_by_pos(device_type: str) -> None:
    """M-04 AC: the measures come out sorted by `pos`, whatever the JSON order."""
    positions = [spec.pos for spec in _specs(device_type).values()]

    assert positions == sorted(positions)
    assert None not in positions


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_pos_is_sparse_and_is_never_an_index(device_type: str) -> None:
    """
    M-04 AC: a non-contiguous `pos` does not break the ordering.

    This is the trap the backend flagged explicitly, and the real fixtures
    spring it on their own: a `now` declares six measures whose `pos` values
    are 1, 3, 6, 10, 11, 12 - the gaps being the measures that type does not
    have. Anything that treated `pos` as an index into a list would raise or
    silently mis-assign here.
    """
    specs = _specs(device_type)
    positions = [spec.pos for spec in specs.values()]

    # Sparse for real, not just in theory: the values skip numbers.
    assert max(positions) > len(positions)
    # And the order still holds end to end.
    assert list(specs) == [
        name for name, _ in sorted(specs.items(), key=lambda kv: kv[1].pos)
    ]


def test_a_broken_pos_still_produces_a_total_order() -> None:
    """A duplicated, negative or missing `pos` degrades gracefully."""
    specs = build_specs(
        {
            "gamma": {"pos": 7, "unit": "ppm", "ranges": []},
            "alpha": {"pos": 7, "unit": "ppm", "ranges": []},
            "delta": {"unit": "ppm", "ranges": []},
            "beta": {"pos": -3, "unit": "ppm", "ranges": []},
        },
        device_type="invented",
    )

    # Sorted by pos, ties broken by name, and the measure with no `pos` at
    # the end - never dropped, never crashing the build.
    assert list(specs) == ["beta", "alpha", "gamma", "delta"]
    assert specs["delta"].pos is None


def test_bands_keep_the_order_the_api_served() -> None:
    """
    The five-band vocabulary is `excellent -> high -> good -> poor -> terrible`.

    `high` sits second, between `excellent` and `good`. That is intentional
    on the backend's side and it is not the order this integration used to
    hardcode (`excellent -> good -> medium -> poor -> terrible`), so it is
    pinned from the fixture rather than assumed.
    """
    specs = _specs("nowplus")

    assert specs["eco2"].statuses == (
        "excellent",
        "high",
        "good",
        "poor",
        "terrible",
    )
    assert specs["internal_temperature"].statuses == ("low", "good", "high")


def test_the_last_band_is_open_ended() -> None:
    """The band the API sends without `upperBound` catches everything above."""
    eco2 = _specs("nowplus")["eco2"]

    assert eco2.ranges[-1] == Band(status="terrible", upper_bound=None)
    assert all(band.upper_bound is not None for band in eco2.ranges[:-1])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "excellent"),
        (500.0, "excellent"),  # on the bound: the band it bounds
        (500.01, "high"),
        (1000.0, "high"),
        (1500.0, "good"),
        (2000.0, "poor"),
        (2000.01, "terrible"),
        (99999.0, "terrible"),
    ],
)
def test_status_for_walks_the_bands_inclusively(value: float, expected: str) -> None:
    """`upperBound` is inclusive, matching the thresholds T-02 verified."""
    assert _specs("nowplus")["eco2"].status_for(value) == expected


def test_a_measure_with_no_bands_has_no_status() -> None:
    """`pressure` is served with `ranges: []` - a number, with no verdict on it."""
    pressure = _specs("nowplus")["pressure"]

    assert pressure.ranges == ()
    assert pressure.statuses == ()
    assert pressure.status_for(101325.0) is None


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_no_scale_factor_is_served_or_applied(device_type: str) -> None:
    """
    `scaleFactor` is deliberately absent from the response (T-08).

    Values arrive already scaled and the client must apply nothing. Pinned
    on the fixtures because the day it *does* appear, this test is the one
    that should notice - silently ignoring a scale factor would misreport
    every value of that measure.
    """
    payload = load_dev_fixture(f"measures_ranges__{device_type}")

    assert all("scaleFactor" not in measure for measure in payload.values())
    assert not any(
        hasattr(spec, "scale_factor") for spec in _specs(device_type).values()
    )


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_every_served_unit_is_mapped(device_type: str) -> None:
    """The unit enumeration the API uses is covered end to end, with no warning."""
    payload = load_dev_fixture(f"measures_ranges__{device_type}")

    assert {measure["unit"] for measure in payload.values()} <= set(HA_UNITS)


def test_the_aqi_has_no_unit_and_tvoc_keeps_the_one_the_api_declares() -> None:
    """
    The AQI's empty unit maps to nothing; tvoc's `V - Ix` passes through (M-07).

    The two are not the same case, which is why they stopped sharing a test.
    An index has no unit and Home Assistant renders that correctly, so
    inventing one would be worse than none. `V - Ix` is not a standard unit
    either (T-02 D-07) but it is what the API says the number is in, and
    publishing it is what stops the reading from looking like a
    concentration whose unit went missing - the shape it had in the
    released version, where tvoc was µg/m³.
    """
    specs = _specs("nowplus")

    assert specs["aqi_value"].unit == ""
    assert specs["aqi_value"].ha_unit is None
    assert specs["tvoc"].unit == "V - Ix"
    assert specs["tvoc"].ha_unit == "V - Ix"


def test_pressure_is_declared_in_pascal() -> None:
    """M-04 AC: pressure keeps the unit the API sends; the UI converts, not us."""
    pressure = _specs("nowplus")["pressure"]

    assert pressure.unit == "Pa"
    assert str(pressure.ha_unit) == "Pa"
    assert pressure.device_class is SensorDeviceClass.PRESSURE


def test_radon_is_served_in_becquerel_and_passes_through() -> None:
    """Home Assistant has no radon unit or device class: the string is the unit."""
    radon = _specs("sense")["radon_bqm3"]

    assert str(radon.ha_unit) == "Bq/m³"
    assert radon.device_class is None


@pytest.mark.parametrize("measure", ["aqi_value", "tvoc"])
def test_measures_with_no_honest_device_class_have_none(measure: str) -> None:
    """
    `aqi_value` and `tvoc` are published without a device class, on purpose.

    `SensorDeviceClass.AQI` presupposes the EPA 0-500 scale while this index
    runs 1-5 (T-02 D-08), and the VOC classes presuppose a concentration
    while tvoc is served in `V - Ix` (T-02 D-07). A device class is a
    promise about what a number means; neither promise is true here.
    """
    assert _specs("nowplus")[measure].device_class is None
    assert measure not in DEVICE_CLASSES


def test_an_unmappable_unit_warns_and_yields_no_unit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M-04 AC: a unit outside the map costs the unit, not the setup."""
    with caplog.at_level(logging.WARNING):
        resolved = resolve_ha_unit(
            "parsecs per fortnight", measure="eco2", device_type="nowplus"
        )

    assert resolved is None
    assert "parsecs per fortnight" in caplog.text
    assert "eco2" in caplog.text


def test_a_malformed_response_costs_the_malformed_part_only() -> None:
    """One broken entry must not cost a device type every entity it has."""
    specs = build_specs(
        {
            "eco2": {"unit": "ppm", "pos": 3, "ranges": [{"status": "good"}]},
            "broken": "not an object",
            "half_broken": {"unit": "ppm", "pos": 4, "ranges": "not a list"},
        },
        device_type="invented",
    )

    assert set(specs) == {"eco2", "half_broken"}
    assert specs["half_broken"].ranges == ()


def test_a_response_that_is_not_an_object_yields_an_empty_schema() -> None:
    """No schema, no crash: the device keeps its telemetry-only entities."""
    assert build_specs(["not", "an", "object"], device_type="invented") == {}
