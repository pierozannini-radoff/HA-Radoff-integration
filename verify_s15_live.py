#!/usr/bin/env python3
"""
Live AC verification launcher for card S-15 (pm1/pm25/pm10 missing names),
against a real Home Assistant dev harness - not `tests/test_translations.py`'s
offline JSON-vs-`properties.py` check, which cannot see what Home Assistant's
own translation-resolution machinery (`entity_platform.py`, driven by
`_attr_has_entity_name`/`translation_key`) actually does with those JSON
files at runtime.

Same launcher trick as `verify_s13_live.py` (see that script's own docstring
for why `custom_components.radoff...` - not bare `radoff...` - is the module
path that must be patched/imported, and why the *repository root*, not
`<repo>/custom_components`, goes on `sys.path`): this monkeypatches
`custom_components.radoff.async_setup_entry` to grab a `hass` reference and
schedule a check once a Radoff config entry (and its sensor platform) has
finished loading, then calls `homeassistant.__main__.main()` directly in
this same process.

Usage - run instead of `./scripts/develop`, with the exact same arguments:

    python3 verify_s15_live.py --config "$PWD/.devcontainer/config" --debug

What it checks, against the real entity registry, mapped to the card's ACs:

- "Le entità PM1, PM2.5 e PM10 hanno un nome proprio nella UI, in italiano e
  in inglese": for every pm1/pm25/pm10 entity, the registry's
  `original_name` - the name Home Assistant's own has_entity_name/
  translation_key resolution wrote at entity-add time (`entity_platform.py`,
  `_async_add_entity`) - is non-empty and matches the value shipped in
  strings.json/translations for `hass.config.language`.
- "Nessuna entità dell'integrazione mostra il nome del dispositivo come
  unico nome": every Radoff entity, not just the 3 PM ones, has a non-empty
  `original_name` - the same symptom (an unresolved translation_key makes a
  has_entity_name entity fall back to the bare device name) generalizes to
  any future property whose MAPPING entry ships without a name, which is
  exactly the structural gap this card closes.
- Per device, the pm1/pm25/pm10 names are pairwise distinct - "collidono
  nella UI" in the card's own description of the bug.

With more than one config entry (e.g. two real accounts, same as S-13's live
round), each entry's check re-scans the whole registry as it stands when
that entry finishes loading - the LAST one to run has the fullest picture,
so give every entry a moment to finish loading before reading the result.

Results are logged at WARNING (visible in the harness's own log output) and
also written to `s15_live_check_result.txt` (repo root, git-ignored) so they
can be `cat`-ed without scrolling back through the HA log.

Not shipped code - same convention as verify_s13_live.py and the other
root-level dev/verify scripts (see `.ruff.toml`'s per-file-ignores).
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
RESULT_FILE = REPO_ROOT / "s15_live_check_result.txt"
RADOFF_DIR = REPO_ROOT / "custom_components" / "radoff"

# `custom_components` must resolve as the same namespace package Home
# Assistant's own component loader will import `radoff` from - i.e. rooted
# at the *repository root* (see the module docstring's "Same launcher
# trick" paragraph).
sys.path.insert(0, str(REPO_ROOT))

import custom_components.radoff as radoff_pkg  # noqa: E402
from custom_components.radoff.const import DOMAIN  # noqa: E402

_LOGGER = logging.getLogger("verify_s15_live")

_PM_KEYS = ("pm1", "pm25", "pm10")

_ORIGINAL_ASYNC_SETUP_ENTRY = radoff_pkg.async_setup_entry


def _expected_name(language: str, key: str) -> str | None:
    """
    Return the name shipped for `key` in the given language's translation
    file, falling back to strings.json - read fresh from disk on every call
    (not hardcoded) so this script also catches a copy/paste mismatch
    between the shipped fix and this checker.
    """
    path = RADOFF_DIR / "translations" / f"{language}.json"
    if not path.exists():
        path = RADOFF_DIR / "strings.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc.get("entity", {}).get("sensor", {}).get(key, {}).get("name")


async def _check_entity_names(hass: Any) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entries = [entry for entry in registry.entities.values() if entry.platform == DOMAIN]

    lines: list[str] = []
    failures: list[str] = []

    def record(label: str, ok: bool) -> None:  # noqa: FBT001
        line = f"[{'OK' if ok else 'FAIL'}] {label}"
        lines.append(line)
        _LOGGER.warning("verify_s15_live: %s", line)
        if not ok:
            failures.append(label)

    if not entries:
        record("at least one Radoff entity is registered", False)

    # AC: no Radoff entity falls back to the bare device name.
    for entry in entries:
        record(
            f"{entry.entity_id} ({entry.unique_id}) has its own resolved name",
            bool(entry.original_name),
        )

    # AC: pm1/pm25/pm10 have a proper, correctly translated, non-colliding name.
    by_device: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for key in _PM_KEYS:
            if entry.unique_id.endswith(f"-{key}"):
                device_id = entry.unique_id.removesuffix(f"-{key}")
                by_device.setdefault(device_id, {})[key] = entry

    record("at least one device exposes pm1/pm25/pm10 entities", bool(by_device))

    expected = {key: _expected_name(hass.config.language, key) for key in _PM_KEYS}
    for device_id, per_key in by_device.items():
        names_seen = []
        for key in _PM_KEYS:
            entry = per_key.get(key)
            record(f"device {device_id}: {key} entity exists", entry is not None)
            if entry is None:
                continue
            record(
                f"device {device_id}: {key} name matches translations "
                f"({expected[key]!r})",
                entry.original_name == expected[key],
            )
            names_seen.append(entry.original_name)
        record(
            f"device {device_id}: pm1/pm25/pm10 names do not collide",
            len(set(names_seen)) == len(names_seen),
        )

    summary = (
        f"{len(failures)} check(s) FAILED" if failures else "All S-15 live checks passed"
    )
    lines.append("")
    lines.append(summary)
    RESULT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _LOGGER.warning("verify_s15_live: %s (see %s)", summary, RESULT_FILE)


async def _patched_async_setup_entry(hass: Any, config_entry: Any) -> bool:
    result = await _ORIGINAL_ASYNC_SETUP_ENTRY(hass, config_entry)
    hass.async_create_task(_check_entity_names(hass))
    return result


radoff_pkg.async_setup_entry = _patched_async_setup_entry

_LOGGER.warning(
    "verify_s15_live active - will check entity names right after each Radoff "
    "config entry finishes loading"
)

from homeassistant.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
