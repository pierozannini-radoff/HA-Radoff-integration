"""
Structural completeness test for cards S-15, S-16 and M-04.

pm1/pm25/pm10 shipped without a name in strings.json/translations because
the definition of one sensor lived on four separate files (the field table +
strings.json + 2 translations) and nothing enforced that they stay in sync
(analysis §8.1). This test makes that enforcement structural instead of
relying on whoever touches that table to remember the other three files.

Card M-04 changes what it enforces them against. There is no table left:
the source of truth is `/analytics/measures-ranges`, so the fixture M-01
captured for the whole catalogue (`measures_ranges__all.json` - the merged
answer the endpoint gives with no `device_type`) is what these assertions
read. Two consequences worth stating, because they are the point of the
card:

- the measure names, and therefore the entity slugs, are the API's;
- the qualitative states are the API's too, in the API's own vocabulary -
  `excellent -> high -> good -> poor -> terrible`, with `high` second, and
  `low -> good -> high` for temperature and humidity. The states this
  integration used to invent (`medium`, and `excellent`/`terrible` on a
  three-band measure) are gone, and an orphaned translation of one of them
  fails the orphan test below.

What it can no longer check by construction is the *reachability* of a
state: the bands come from the response, so every status the response
declares is reachable by definition. `test_schema.py` walks the boundaries
instead, against the same fixtures.
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
# `GET /analytics/measures-ranges` returns it without a `device_type`.
# Driving off the merged answer rather than off one type is deliberate -
# the integration no longer filters devices by type (M-04), so a user with
# a `sismoff` must find `ch4` and `co` translated too.
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
    """S-15 AC, on M-04's source of truth: every served measure has a name."""
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    assert slug in sensor_entities, f"{path.name}: missing entity.sensor.{slug}"
    assert sensor_entities[slug].get(
        "name"
    ), f"{path.name}: entity.sensor.{slug} has no non-empty 'name'"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("slug", sorted(f"{name}_index" for name in INDEX_MEASURES))
def test_every_banded_measure_has_an_index_name(path: Path, slug: str) -> None:
    """A measure the API bands gets a qualitative sibling, and it needs a name."""
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    assert slug in sensor_entities, f"{path.name}: missing entity.sensor.{slug}"
    assert sensor_entities[slug].get(
        "name"
    ), f"{path.name}: entity.sensor.{slug} has no non-empty 'name'"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
def test_no_orphaned_entity_names(path: Path) -> None:
    """Flags entity.sensor keys left over from a removed/renamed measure."""
    sensor_entities = set(DOCS[path].get("entity", {}).get("sensor", {}).keys())
    orphans = sensor_entities - ALLOWED_SLUGS
    assert not orphans, f"{path.name}: orphaned entity.sensor keys: {sorted(orphans)}"


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
@pytest.mark.parametrize("name", sorted(INDEX_MEASURES))
def test_index_translation_states_match_the_served_bands(path: Path, name: str) -> None:
    """
    S-16 AC, restated for M-04: the states are exactly the ones served.

    Neither missing a band the API declares - which would leave a user
    looking at a raw `terrible` in their own language's dashboard - nor
    keeping one it does not, which is how `medium` would survive this
    migration unnoticed.
    """
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
    """
    M-04 AC: no state this integration invented survives anywhere.

    `medium` is the marker. It never existed in the API's vocabulary - it
    came from `sensor.py::FIVE_LEVELS`, deleted with the rest of the
    hardcoded tables - so a single occurrence left in a translation file
    means one `*_index` entity was migrated by hand and missed.
    """
    sensor_entities = DOCS[path].get("entity", {}).get("sensor", {})
    with_medium = [
        slug
        for slug, entry in sensor_entities.items()
        if "medium" in entry.get("state", {})
    ]
    assert not with_medium, f"{path.name}: `medium` still translated in {with_medium}"
