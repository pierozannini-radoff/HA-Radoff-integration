"""
Structural completeness test for card S-15.

pm1/pm25/pm10 shipped without a name in strings.json/translations because
the definition of one sensor lives on four separate files (properties.py +
strings.json + 2 translations) and nothing enforced that they stay in sync
(analysis §8.1). This test makes that enforcement structural instead of
relying on whoever touches `properties.py` to remember the other three
files: for every (bucket, property) pair `properties.py::MAPPING` defines -
including the aggregated entities S-10 introduced - it asserts a matching
`entity.sensor.<slug>.name` exists in strings.json and in every
translations/*.json file, and flags any leftover key that no longer maps to
a real MAPPING entry.

Deliberately out of scope (card S-15's own "OUT OF SCOPE", owned by S-16):
the `_index` sibling entities `sensor.py::INDEX_MAPPING` generates - their
names and `state` blocks are not required here, only tolerated as
non-orphaned.
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
    assert sensor_entities[slug].get("name"), (
        f"{path.name}: entity.sensor.{slug} has no non-empty 'name'"
    )


@pytest.mark.parametrize("path", TRANSLATION_PATHS, ids=lambda p: p.name)
def test_no_orphaned_entity_names(path: Path) -> None:
    """Flags entity.sensor keys left over from a removed/renamed property."""
    sensor_entities = set(DOCS[path].get("entity", {}).get("sensor", {}).keys())
    orphans = sensor_entities - ALLOWED_SLUGS
    assert not orphans, f"{path.name}: orphaned entity.sensor keys: {sorted(orphans)}"
