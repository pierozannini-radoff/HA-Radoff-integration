"""
Reading `measures-ranges` into `MeasureSpec`, against real dev fixtures.

Covers: `pos` ordering, band vocabulary and boundaries, units and device
classes, and what a malformed response costs.
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

# Every type the catalogue holds. `life` is excluded: the fixture of that
# name is the 404 body dev answered with, not a schema. `sismoff` is here,
# and only here - its schema is read like every other type's, keeping
# `co`/`ch4` covered, but no captured device of that type exists to run the
# entity-level checks against.
SCHEMA_TYPES = ("nowplus", "sense", "city", "now", "sismoff")


def _specs(device_type: str) -> dict[str, MeasureSpec]:
    return build_specs(
        load_dev_fixture(f"measures_ranges__{device_type}"), device_type=device_type
    )


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_measures_are_ordered_by_pos(device_type: str) -> None:
    """The measures come out sorted by `pos`, with no `pos` missing."""
    positions = [spec.pos for spec in _specs(device_type).values()]

    assert positions == sorted(positions)
    assert None not in positions


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_pos_is_sparse_and_is_never_an_index(device_type: str) -> None:
    """A `pos` with real gaps in it still sorts the measures end to end."""
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
    """The bands keep the served order, `high` second between excellent and good."""
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
    """`upperBound` is inclusive: a value on the boundary belongs to that band."""
    assert _specs("nowplus")["eco2"].status_for(value) == expected


def test_a_measure_with_no_bands_has_no_status() -> None:
    """`pressure` is served with `ranges: []` - a number, with no verdict on it."""
    pressure = _specs("nowplus")["pressure"]

    assert pressure.ranges == ()
    assert pressure.statuses == ()
    assert pressure.status_for(101325.0) is None


@pytest.mark.parametrize("device_type", SCHEMA_TYPES)
def test_no_scale_factor_is_served_or_applied(device_type: str) -> None:
    """No `scaleFactor` is served, and no `MeasureSpec` carries one."""
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
    """The AQI is published with no unit; tvoc keeps the `V - Ix` the API sends."""
    specs = _specs("nowplus")

    assert specs["aqi_value"].unit == ""
    assert specs["aqi_value"].ha_unit is None
    assert specs["tvoc"].unit == "V - Ix"
    assert specs["tvoc"].ha_unit == "V - Ix"


def test_pressure_is_declared_in_pascal() -> None:
    """Pressure keeps the `Pa` the API sends, with the pressure device class."""
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
    """`aqi_value` and `tvoc` carry no device class, and are in no class map."""
    assert _specs("nowplus")[measure].device_class is None
    assert measure not in DEVICE_CLASSES


def test_an_unmappable_unit_warns_and_yields_no_unit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A unit outside the map resolves to `None` and names itself in a WARNING."""
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
