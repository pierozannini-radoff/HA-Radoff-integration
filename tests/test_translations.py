"""
Translation completeness against the schema the API serves.

Covers: measure names, qualitative sibling names, states, orphaned keys.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from .conftest import load_dev_fixture  # noqa: E402

RADOFF_DIR = REPO_ROOT / "custom_components" / "radoff"
STRINGS_PATH = RADOFF_DIR / "strings.json"
TRANSLATIONS_DIR = RADOFF_DIR / "translations"

# The catalogue's whole vocabulary: every measure of every device type, as
# `GET /analytics/measures-ranges` returns it without a `device_type`. The
# integration does not filter devices by type, so a user with a `sismoff`
# must find `ch4` and `co` translated too.
ALL_MEASURES: dict[str, dict] = load_dev_fixture("measures_ranges__all")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _translation_paths() -> list[Path]:
    return [STRINGS_PATH, *sorted(TRANSLATIONS_DIR.glob("*.json"))]


def _index_measures() -> dict[str, list[str]]:
    """`{measure: [status, ...]}` for every measure the API bands."""
    return {
        name: [band["status"] for band in measure["ranges"]]
        for name, measure in ALL_MEASURES.items()
        if measure["ranges"]
    }


REQUIRED_SLUGS = sorted(ALL_MEASURES)
INDEX_MEASURES = _index_measures()
ALLOWED_SLUGS = set(REQUIRED_SLUGS) | {f"{name}_index" for name in INDEX_MEASURES}
TRANSLATION_PATHS = _translation_paths()
DOCS = {path: _load(path) for path in TRANSLATION_PATHS}


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("slug", REQUIRED_SLUGS)
def test_every_measure_has_a_name(path: Path, slug: str) -> None:
    """Every measure the API serves has a translated name."""
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    assert slug in sensor_entities, f"{path.name}: missing entity.sensor.{slug}"
    assert sensor_entities[slug].get(
        "name"
    ), f"{path.name}: entity.sensor.{slug} has no non-empty 'name'"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("slug", sorted(f"{name}_index" for name in INDEX_MEASURES))
def test_every_banded_measure_has_an_index_name(path: Path, slug: str) -> None:
    """A measure the API bands has a translated name for its sibling."""
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    assert slug in sensor_entities, f"{path.name}: missing entity.sensor.{slug}"
    assert sensor_entities[slug].get(
        "name"
    ), f"{path.name}: entity.sensor.{slug} has no non-empty 'name'"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
def test_no_orphaned_entity_names(path: Path) -> None:
    """No entity.sensor key is left over from a removed or renamed measure."""
    sensor_entities = set(DOCS[path].get("entity", {}).get("sensor", {}).keys())
    orphans = sensor_entities - ALLOWED_SLUGS
    assert not orphans, f"{path.name}: orphaned entity.sensor keys: {sorted(orphans)}"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("name", sorted(INDEX_MEASURES))
def test_index_translation_states_match_the_served_bands(path: Path, name: str) -> None:
    """Translated states are exactly the bands the API serves."""
    slug = f"{name}_index"
    declared = set(INDEX_MEASURES[name])
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


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
def test_the_deleted_vocabulary_is_gone(path: Path) -> None:
    """No `medium` state survives in any translation file."""
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    with_medium = [
        slug
        for slug, entry in sensor_entities.items()
        if "medium" in entry.get("state", {})
    ]
    assert not with_medium, f"{path.name}: `medium` still translated in {with_medium}"
