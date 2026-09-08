"""
Structural completeness test for cards S-15 and S-16.

pm1/pm25/pm10 shipped without a name in strings.json/translations because
the definition of one sensor lives on four separate files (properties.py +
strings.json + 2 translations) and nothing enforced that they stay in sync
(analysis §8.1). This test makes that enforcement structural instead of
relying on whoever touches `properties.py` to remember the other three
files: for every (bucket, property) pair `properties.py::MAPPING` defines -
including the aggregated entities S-10 introduced - it asserts a matching
`entity.sensor.<slug>.name` exists in strings.json and in every
translations/*.json file, and flags any leftover key that no longer maps to
a real MAPPING entry (S-15).

It also covers the `_index` sibling entities `sensor.py::INDEX_MAPPING`
generates (S-16, C5/C13): for each one, the states an index_fn can actually
reach on boundary values must match `INDEX_MAPPING[...]["states"]` exactly,
and each translation file's `state` block for that entity must expose
exactly those states - neither missing a reachable one nor keeping an
orphaned key from a state the index_fn can no longer return.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.entity import reading_key_slug  # noqa: E402
from custom_components.radoff.properties import MAPPING  # noqa: E402
from custom_components.radoff.sensor import INDEX_MAPPING  # noqa: E402

RADOFF_DIR = REPO_ROOT / "custom_components" / "radoff"
STRINGS_PATH = RADOFF_DIR / "strings.json"
TRANSLATIONS_DIR = RADOFF_DIR / "translations"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _translation_paths() -> list[Path]:
    return [STRINGS_PATH, *sorted(TRANSLATIONS_DIR.glob("*.json"))]


def _required_slugs() -> set[str]:
    """Every entity slug a real (bucket, property) pair in MAPPING resolves to."""
    return {
        reading_key_slug((bucket, prop_name))
        for bucket, props in MAPPING.items()
        for prop_name in props
    }


def _index_slugs() -> set[str]:
    """The `_index` sibling slugs sensor.py generates for INDEX_MAPPING entries."""
    return {f"{name}_index" for name in INDEX_MAPPING}


REQUIRED_SLUGS = sorted(_required_slugs())
ALLOWED_SLUGS = _required_slugs() | _index_slugs()
TRANSLATION_PATHS = _translation_paths()
DOCS = {path: _load(path) for path in TRANSLATION_PATHS}


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("slug", REQUIRED_SLUGS)
def test_every_mapping_entry_has_a_name(path: Path, slug: str) -> None:
    """S-15 AC: every properties.py entry (incl. S-10's aggregated ones) has a name."""
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    assert slug in sensor_entities, f"{path.name}: missing entity.sensor.{slug}"
    assert sensor_entities[slug].get(
        "name"
    ), f"{path.name}: entity.sensor.{slug} has no non-empty 'name'"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
def test_no_orphaned_entity_names(path: Path) -> None:
    """Flags entity.sensor keys left over from a removed/renamed property."""
    sensor_entities = set(DOCS[path].get("entity", {}).get("sensor", {}).keys())
    orphans = sensor_entities - ALLOWED_SLUGS
    assert not orphans, f"{path.name}: orphaned entity.sensor keys: {sorted(orphans)}"


def _reachable_index_states(name: str) -> set[str]:
    """States `INDEX_MAPPING[name]["index"]` actually returns on boundary values."""
    index_obj = INDEX_MAPPING[name]
    index_fn = index_obj["index"]
    epsilon = 0.01
    reached: set[str] = set()
    for threshold in index_obj["thresholds"]:
        reached.add(index_fn(threshold))
        reached.add(index_fn(threshold + epsilon))
    return reached


@pytest.mark.parametrize("name", sorted(INDEX_MAPPING))
def test_index_fn_boundary_states_match_declared_states(name: str) -> None:
    """S-16 AC: states reachable from an index_fn are exactly its declared `states`."""
    declared = set(INDEX_MAPPING[name]["states"])
    reached = _reachable_index_states(name)
    assert reached == declared, (
        f"{name}: index_fn reachable states {sorted(reached)} != "
        f"declared states {sorted(declared)}"
    )


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("name", sorted(INDEX_MAPPING))
def test_index_translation_states_match_declared_states(path: Path, name: str) -> None:
    """S-16 AC: each *_index translation exposes exactly the declared states."""
    slug = f"{name}_index"
    declared = set(INDEX_MAPPING[name]["states"])
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    assert slug in sensor_entities, f"{path.name}: missing entity.sensor.{slug}"
    state_keys = set(sensor_entities[slug].get("state", {}).keys())
    missing = declared - state_keys
    orphaned = state_keys - declared
    assert (
        not missing
    ), f"{path.name}: entity.sensor.{slug}.state missing keys {sorted(missing)}"
    assert (
        not orphaned
    ), f"{path.name}: entity.sensor.{slug}.state has orphaned keys {sorted(orphaned)}"
