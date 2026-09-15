"""
Fotografa (e confronta) lo stato Radoff di un'istanza Home Assistant ferma.

Serve alla passata a mano della migrazione a VERSION 3: l'unica verifica che
nessuna suite puo' fare, perche' riguarda il registro e lo storico di
un'installazione vera invece che una loro ricostruzione.

Si usa in tre momenti:

    python3 scripts/ha_registry_snapshot.py <config-dir> --out prima.json
    # ... aggiornamento dell'integrazione, riavvio, riparazione ...
    python3 scripts/ha_registry_snapshot.py <config-dir> --out dopo.json
    python3 scripts/ha_registry_snapshot.py --diff prima.json dopo.json

**L'istanza dev'essere ferma.** Lo snapshot legge `.storage/*` e il DB del
recorder in sola lettura, ma un file JSON che Home Assistant sta riscrivendo
si legge a meta'.

Cosa guarda, e perche' ciascuna cosa

- **entity_id ↔ unique_id**: la migrazione riscrive il secondo e non deve
  toccare il primo. E' l'`entity_id` a tenere attaccati `states` e le serie
  statistiche, quindi e' li' che si vede se lo storico e' sopravvissuto.
- **righe di stato e serie long-term per entita'**: un `entity_id` rimasto
  uguale ma con lo storico staccato sarebbe una migrazione riuscita a meta'.
- **unita' della serie statistica**: e' il campo che Home Assistant confronta
  per decidere se una serie e' continua. E' li' che si vede il buco atteso
  sul solo tvoc, e che si vede se ne sono comparsi altri non previsti.
- **serial del device di ogni entita'**: e' l'ingresso della migrazione
  offline (entita' -> device -> serial), quindi l'unico dato da cui il nuovo
  identificatore puo' venire.

Politica di stampa: nessun segreto finisce nello snapshot. Delle config entry
si registrano versione, *nomi* delle chiavi di `data`/`options` e un'etichetta
posizionale (entry A, entry B); mai username, password o il dominio. I serial
dei device e gli `entity_id` si' - sono cio' che va confrontato.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

DOMAIN = "radoff"


def _load(config_dir: Path, name: str) -> dict[str, Any]:
    return json.loads((config_dir / ".storage" / name).read_text(encoding="utf-8"))


def _serial_by_device_id(config_dir: Path) -> dict[str, str]:
    devices = _load(config_dir, "core.device_registry")["data"]["devices"]
    serials: dict[str, str] = {}
    for device in devices:
        for identifier in device.get("identifiers") or []:
            if identifier and identifier[0] == DOMAIN:
                serials[device["id"]] = identifier[1]
    return serials


def _history(db: Path) -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """Righe di stato per entity_id, e metadati della serie statistica."""
    if not db.is_file():
        return {}, {}

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        states = {
            entity_id: count
            for entity_id, count in conn.execute(
                "SELECT m.entity_id, COUNT(*) FROM states s "
                "JOIN states_meta m ON s.metadata_id = m.metadata_id "
                "GROUP BY m.entity_id"
            )
        }
        stats = {
            statistic_id: {"rows": rows, "unit": unit}
            for statistic_id, unit, rows in conn.execute(
                "SELECT sm.statistic_id, sm.unit_of_measurement, COUNT(st.id) "
                "FROM statistics_meta sm "
                "LEFT JOIN statistics st ON st.metadata_id = sm.id "
                "GROUP BY sm.statistic_id, sm.unit_of_measurement"
            )
        }
    finally:
        conn.close()
    return states, stats


def snapshot(config_dir: Path) -> dict[str, Any]:
    """Lo stato Radoff dell'istanza, senza nulla di segreto dentro."""
    entries = [
        entry
        for entry in _load(config_dir, "core.config_entries")["data"]["entries"]
        if entry.get("domain") == DOMAIN
    ]
    labels = {
        entry["entry_id"]: f"entry {chr(ord('A') + index)}"
        for index, entry in enumerate(entries)
    }
    serials = _serial_by_device_id(config_dir)
    states, stats = _history(config_dir / "home-assistant_v2.db")

    entities = []
    for entity in _load(config_dir, "core.entity_registry")["data"]["entities"]:
        if entity.get("platform") != DOMAIN:
            continue
        entity_id = entity["entity_id"]
        statistic = stats.get(entity_id, {})
        entities.append(
            {
                "entity_id": entity_id,
                "unique_id": entity["unique_id"],
                "entry": labels.get(entity.get("config_entry_id"), "?"),
                "serial": serials.get(entity.get("device_id") or ""),
                "disabled_by": entity.get("disabled_by"),
                "unit_override": (entity.get("options") or {})
                .get("sensor", {})
                .get("unit_of_measurement"),
                "state_rows": states.get(entity_id, 0),
                "statistics_rows": statistic.get("rows", 0),
                "statistics_unit": statistic.get("unit"),
            }
        )

    return {
        "config_entries": [
            {
                "entry": labels[entry["entry_id"]],
                "version": entry.get("version"),
                "minor_version": entry.get("minor_version"),
                "data_keys": sorted(entry.get("data", {})),
                "options_keys": sorted(entry.get("options", {})),
            }
            for entry in entries
        ],
        "devices": sorted(set(serials.values())),
        "entities": sorted(entities, key=lambda item: item["entity_id"]),
    }


def print_snapshot(data: dict[str, Any]) -> None:
    """Il riassunto leggibile, quello che si incolla in una nota di QA."""
    print()
    for entry in data["config_entries"]:
        print(
            f"  {entry['entry']}: VERSION {entry['version']}.{entry['minor_version']}"
            f"  data={entry['data_keys']}  options={entry['options_keys']}"
        )
    print(f"  device: {', '.join(data['devices']) or '(nessuno)'}")

    entities = data["entities"]
    legacy = [e for e in entities if len(e["unique_id"].split("-")) > 4]
    with_stats = [e for e in entities if e["statistics_rows"]]
    print(
        f"  entita': {len(entities)}"
        f"  ({len(legacy)} su identificatori arch 1.x)"
        f"  con statistiche long-term: {len(with_stats)}"
    )
    print()
    print(
        f"  {'entity_id':<44} {'unique_id':<50} {'stati':>6} {'stat':>5}  unita' serie"
    )
    print(f"  {'-' * 44} {'-' * 50} {'-' * 6} {'-' * 5}  {'-' * 12}")
    for item in entities:
        unit = item["statistics_unit"]
        print(
            f"  {item['entity_id']:<44} {item['unique_id']:<50} "
            f"{item['state_rows']:>6} {item['statistics_rows']:>5}  "
            f"{unit if unit is not None else ''}"
        )


def print_diff(before: dict[str, Any], after: dict[str, Any]) -> int:
    """
    Il confronto, con un verdetto. Ritorna 1 se qualcosa che doveva reggere non regge.

    I criteri sono quelli della card, in quest'ordine: nessun `entity_id`
    sparito o comparso, nessun `unique_id` rimasto sulla forma vecchia,
    nessuna entita' che perde il proprio storico, e le sole unita' di serie
    che cambiano sono quelle annunciate.
    """
    before_by_id = {item["entity_id"]: item for item in before["entities"]}
    after_by_id = {item["entity_id"]: item for item in after["entities"]}

    lost = sorted(set(before_by_id) - set(after_by_id))
    born = sorted(set(after_by_id) - set(before_by_id))
    survivors = sorted(set(before_by_id) & set(after_by_id))

    print()
    for entry_before, entry_after in zip(
        before["config_entries"], after["config_entries"], strict=False
    ):
        print(
            f"  {entry_before['entry']}: "
            f"VERSION {entry_before['version']}.{entry_before['minor_version']}"
            f" -> {entry_after['version']}.{entry_after['minor_version']}"
            f"   data {entry_before['data_keys']} -> {entry_after['data_keys']}"
        )

    print()
    print(f"  entita' prima: {len(before_by_id)}   dopo: {len(after_by_id)}")
    print(f"  sopravvissute con lo stesso entity_id: {len(survivors)}")
    print(f"  sparite: {len(lost)}   nuove: {len(born)}")

    if lost:
        print("\n  SPARITE (l'entity_id non c'e' piu': dashboard e automazioni rotte)")
        for entity_id in lost:
            print(f"    - {entity_id}   era {before_by_id[entity_id]['unique_id']}")
    if born:
        print("\n  NUOVE (attese solo per misure che prima non esistevano)")
        for entity_id in born:
            print(f"    + {entity_id}   {after_by_id[entity_id]['unique_id']}")

    rekeyed, untouched, still_legacy, lost_history, unit_changed = [], [], [], [], []
    for entity_id in survivors:
        b, a = before_by_id[entity_id], after_by_id[entity_id]
        if b["unique_id"] != a["unique_id"]:
            rekeyed.append((entity_id, b["unique_id"], a["unique_id"]))
        else:
            untouched.append(entity_id)
        if len(a["unique_id"].split("-")) > 4:
            still_legacy.append((entity_id, a["unique_id"]))
        if b["state_rows"] and a["state_rows"] < b["state_rows"]:
            lost_history.append((entity_id, b["state_rows"], a["state_rows"]))
        if b["statistics_unit"] != a["statistics_unit"]:
            unit_changed.append((entity_id, b["statistics_unit"], a["statistics_unit"]))

    print(f"\n  RI-CHIAVATE: {len(rekeyed)}")
    for entity_id, old, new in rekeyed:
        print(f"    {entity_id}\n        {old}\n     -> {new}")
    print(f"\n  identificatore invariato: {len(untouched)}")

    problems = 0
    if still_legacy:
        problems += 1
        print(f"\n  ANCORA SU IDENTIFICATORI ARCH 1.x: {len(still_legacy)}")
        for entity_id, unique_id in still_legacy:
            print(f"    - {entity_id}   {unique_id}")
    if lost_history:
        problems += 1
        print(f"\n  STORICO PERSO: {len(lost_history)}")
        for entity_id, was, now in lost_history:
            print(f"    - {entity_id}: {was} righe di stato -> {now}")
    if lost:
        problems += 1

    if unit_changed:
        print(f"\n  UNITA' DELLA SERIE STATISTICA CAMBIATE: {len(unit_changed)}")
        for entity_id, was, now in unit_changed:
            expected = "tvoc" in entity_id or "vocs" in entity_id
            mark = "atteso" if expected else "NON ATTESO"
            print(f"    - {entity_id}: {was!r} -> {now!r}   [{mark}]")
            if not expected:
                problems += 1

    print()
    print(
        "  " + ("TUTTO REGGE." if not problems else f"DA GUARDARE: {problems} punti.")
    )
    return 1 if problems else 0


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", nargs="?", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--diff", nargs=2, type=Path, metavar=("PRIMA", "DOPO"))
    args = parser.parse_args()

    if args.diff:
        before = json.loads(args.diff[0].read_text(encoding="utf-8"))
        after = json.loads(args.diff[1].read_text(encoding="utf-8"))
        return print_diff(before, after)

    if args.config_dir is None:
        parser.error("serve un config-dir, oppure --diff PRIMA DOPO")

    data = snapshot(args.config_dir)
    print_snapshot(data)
    if args.out:
        args.out.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\n  scritto: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
