"""
Guardia sulle fixture reali catturate su dev (card M-01).

Non testa l'integrazione: testa le *fixture*. `scripts/probe_arch2.py` reda
ogni response prima di scriverla, ma quella redazione gira una volta, sulla
macchina di chi esegue la ricognizione, e il risultato finisce nel repo per
sempre. Questi test sono il secondo paio d'occhi: girano in CI a ogni
commit e falliscono se in `tests/fixtures/dev/` compare qualcosa che non
doveva entrarci - un JWT, una mail, un UUID non sostituito, una coordinata
a piena precisione.

Se la cartella e' vuota (la ricognizione non e' ancora stata eseguita) i
test si saltano invece di fallire: la card che la esegue e' M-01, non
questa suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from .conftest import DEV_FIXTURES_DIR, load_dev_fixture

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_JWT_RE = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b")

# Chiavi di coordinate: lo script le arrotonda a 1 decimale (~11 km). Piu'
# cifre significative di cosi' vuol dire che una redazione e' saltata.
_COORD_KEYS = {"lat", "latitude", "lng", "lon", "long", "longitude"}
_COORD_MAX_DECIMALS = 1

_SECRET_KEY_HINTS = ("password", "token", "secret", "apikey", "authorization")


def _fixture_files() -> list[Path]:
    if not DEV_FIXTURES_DIR.is_dir():
        return []
    return sorted(
        path
        for path in DEV_FIXTURES_DIR.glob("*.json")
        if not path.name.startswith("_")
    )


def _iter_nodes(node: Any, path: str = "$") -> list[tuple[str, str, Any]]:
    """Appiattisce il payload in (percorso, chiave, valore) per i controlli."""
    found: list[tuple[str, str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((f"{path}.{key}", str(key), value))
            found.extend(_iter_nodes(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_iter_nodes(value, f"{path}[{index}]"))
    return found


_FIXTURE_FILES = _fixture_files()

pytestmark = pytest.mark.skipif(
    not _FIXTURE_FILES,
    reason=(
        "Nessuna fixture reale in tests/fixtures/dev/: la ricognizione M-01 "
        "non e' ancora stata eseguita su dev."
    ),
)


@pytest.mark.parametrize("path", _FIXTURE_FILES, ids=lambda p: getattr(p, "stem", "nessuna-fixture"))
def test_dev_fixture_is_valid_json(path: Path) -> None:
    """Ogni fixture reale e' JSON valido e caricabile dall'helper."""
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert load_dev_fixture(path.stem) == parsed


@pytest.mark.parametrize("path", _FIXTURE_FILES, ids=lambda p: getattr(p, "stem", "nessuna-fixture"))
def test_dev_fixture_carries_no_identifiers(path: Path) -> None:
    """Nessun JWT, mail o UUID e' sopravvissuto alla redazione."""
    text = path.read_text(encoding="utf-8")
    assert not _JWT_RE.search(text), f"{path.name}: JWT non redatto"
    assert not _EMAIL_RE.search(text), f"{path.name}: indirizzo mail non redatto"
    assert not _UUID_RE.search(text), (
        f"{path.name}: UUID non sostituito - lo script lo rimpiazza con "
        "<uuid-N>, quindi questo e' arrivato per un'altra strada"
    )


@pytest.mark.parametrize("path", _FIXTURE_FILES, ids=lambda p: getattr(p, "stem", "nessuna-fixture"))
def test_dev_fixture_carries_no_precise_coordinates(path: Path) -> None:
    """Le coordinate sono arrotondate, non a piena precisione."""
    parsed = json.loads(path.read_text(encoding="utf-8"))
    for node_path, key, value in _iter_nodes(parsed):
        if key.lower() not in _COORD_KEYS or not isinstance(value, (int, float)):
            continue
        decimals = len(str(value).partition(".")[2])
        assert decimals <= _COORD_MAX_DECIMALS, (
            f"{path.name}: {node_path} = {value} ha {decimals} decimali, "
            f"il massimo consentito e' {_COORD_MAX_DECIMALS}"
        )


@pytest.mark.parametrize("path", _FIXTURE_FILES, ids=lambda p: getattr(p, "stem", "nessuna-fixture"))
def test_dev_fixture_secret_keys_are_redacted(path: Path) -> None:
    """Nessuna chiave che suona come un segreto porta ancora un valore."""
    parsed = json.loads(path.read_text(encoding="utf-8"))
    for node_path, key, value in _iter_nodes(parsed):
        if not any(hint in key.lower() for hint in _SECRET_KEY_HINTS):
            continue
        assert value in (None, "**REDACTED**"), (
            f"{path.name}: {node_path} sembra un segreto ma non e' redatto"
        )


def test_dev_fixtures_have_a_manifest() -> None:
    """Le fixture sono accompagnate dal manifest e dagli esiti."""
    for name in ("_manifest.json", "_findings.json"):
        assert (DEV_FIXTURES_DIR / name).is_file(), (
            f"{name} manca: rigenera con `python3 scripts/probe_arch2.py`"
        )


def test_dev_manifest_records_response_headers() -> None:
    """
    Il manifest porta gli header di risposta, non solo i body.

    E' un requisito esplicito della card: senza gli header non si ricava il
    nome dell'header di request id (T-08 D-30).
    """
    manifest = json.loads(
        (DEV_FIXTURES_DIR / "_manifest.json").read_text(encoding="utf-8")
    )
    calls = manifest["calls"]
    assert calls, "il manifest non registra nessuna chiamata"
    assert any(call["response_headers"] for call in calls), (
        "nessuna chiamata nel manifest porta header di risposta"
    )
