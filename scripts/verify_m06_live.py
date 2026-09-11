"""
Verifica dal vivo dei criteri di accettazione di M-06 (RT-2945), su dev.

Perche' esiste
--------------
M-06 sposta la disponibilita' delle entita' su un campo che non e' nostro:
`connection_status`. La suite mockata verifica cosa fa il codice per ogni
valore di quel campo - `connected`, `disconnected`, un valore mai visto, il
campo assente - e lo fa meglio di qualunque passata dal vivo, perche' puo'
provocare tutti e quattro i casi a comando.

Cio' che nessuna suite mockata puo' dire e' quali valori l'API serva
*davvero* oggi, e con che eta'. Sono due numeri da cui dipende tutto il
resto:

- se dev servisse un terzo valore, `KNOWN_CONNECTION_STATUSES`
  (api/models.py) sarebbe gia' incompleto e ogni device che lo porta
  finirebbe in `INDETERMINATE` - disponibile, ma per la ragione sbagliata.
  L'enumerazione completa e' la richiesta aperta T-08 D-17;
- se `connection_status_updated_at` fosse sistematicamente piu' vecchio di
  sei ore sui device connessi, la rete di sicurezza di questa card
  (`CONNECTION_STATUS_STALE_WINDOW`) sarebbe rumore invece che segnale - ed
  e' esattamente il residuo che D-17 (b) tiene aperto: chi aggiorna quel
  campo, e con quale cadenza.

Piu' un controllo che la card chiede per nome: il caso riproducibile su dev
(dominio `875fe89b`, device `57FA28`, un sense con `telemetry: null`) deve
essere davvero un device **connesso** senza telemetria. Se fosse
`disconnected`, il primo AC non avrebbe soggetto dal vivo e lo si saprebbe
da qui invece che da una segnalazione.

Come si esegue
--------------
    # credenziali nel proprio .env (gitignored), come RADOFF_USERNAME /
    # RADOFF_PASSWORD - oppure RADOFF_DEV_USERNAME / RADOFF_DEV_PASSWORD
    python3 scripts/verify_m06_live.py --domain-prefix 875fe89b

    # i due override di pool servono finche' const.py punta a un pool
    # diverso dall'ambiente di DEFAULT_BASE_URL (vedi docs/M-04-verifica-dev.md)
    python3 scripts/verify_m06_live.py \
        --pool-id eu-west-1_XXXXXXXX --client-id XXXXXXXX \
        --domain-prefix XXXXXXXX

Cosa NON verifica, e perche'
----------------------------
- **Il valore inatteso di `connection_status`.** Non e' provocabile: e' il
  backend a scriverlo. Se questa passata ne trovasse uno, sarebbe un FAIL
  che chiede di aggiornare l'enumerazione, non un test. Il comportamento
  del client davanti a un valore nuovo (entita' disponibili, un WARNING)
  e' verificato in `tests/test_sensor.py`.
- **Il ripristino dopo un riavvio.** Riguarda il registro di stato di Home
  Assistant, non l'API: si verifica in
  `tests/test_sensor.py::test_a_restart_restores_the_last_known_value`, e
  a mano con `./scripts/develop`.
- **Il 429.** Come per M-05: saturare una quota condivisa con l'app mobile
  per vedere un errore degraderebbe dev per chiunque altro. La
  coordinazione fra 429 e disponibilita' e' verificata in
  `tests/test_coordinator.py`.

Politica di stampa
------------------
Stessa di `probe_arch2.py` e degli altri `verify_*_live.py`: i serial si
stampano, le etichette scritte da persone no - `name`, `room_name`,
`building_name` non compaiono mai, ne' le coordinate, ne' l'email, ne'
alcun token.
"""

from __future__ import annotations

import argparse
import collections
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.api import (  # noqa: E402
    API,
    AuthExpiredError,
    AuthInvalidError,
)
from custom_components.radoff.api.models import (  # noqa: E402
    KNOWN_CONNECTION_STATUSES,
    ConnectionState,
    RadoffDevice,
)
from custom_components.radoff.const import (  # noqa: E402
    CONNECTION_STATUS_STALE_WINDOW,
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
)

# Il caso che la card indica per nome come riproducibile su dev: un sense
# che non trasmette, che dopo M-04 espone comunque le 10 entita' del suo
# tipo. E' il soggetto del primo AC.
SILENT_DEVICE_SERIAL = "57FA28"

# I due pool Cognito in gioco, con l'ambiente a cui appartengono. Servono
# solo a spiegare un fallimento di autenticazione: vedi `auth_hint`.
POOL_LABELS = {
    "eu-west-1_5SsvW9t6S": "dev",
    "eu-west-1_zD4CSIZ6i": "prod (il default di const.py)",
}


def auth_hint(err: Exception, pool_id: str) -> str:
    """
    Spiegare un fallimento di autenticazione invece di riportarlo e basta.

    I due modi in cui questa passata puo' non partire si assomigliano nel
    log e portano a conclusioni opposte, e distinguerli a mano e' costato
    un'indagine il 2026-09-11:

    - `AuthInvalidError` = Cognito ha rifiutato le credenziali **per quel
      pool**. Dev e prod hanno utenze separate: un account che esiste su
      uno non esiste necessariamente sull'altro, quindi le stesse
      credenziali che funzionano altrove qui sono semplicemente sbagliate.
    - `AuthExpiredError` / 401 dopo un handshake riuscito = le credenziali
      erano buone, ma il token viene da un pool che quell'API non accetta
      (M-01: `accepted_pool: pool_dev` in
      `tests/fixtures/dev/_manifest.json`).

    Nessuna chiamata in piu' per scoprirlo: un secondo tentativo su un
    altro pool sarebbe un secondo login fallito a carico dell'account.
    """
    label = POOL_LABELS.get(pool_id, "sconosciuto")
    if isinstance(err, AuthInvalidError):
        return (
            f"Cognito ha rifiutato le credenziali sul pool {pool_id} "
            f"({label}).\n"
            "Le utenze dei due pool sono separate: credenziali valide su un "
            "ambiente non lo sono sull'altro.\n"
            f"Verificare che il .env contenga l'utenza di {label}, non quella "
            "di un altro ambiente."
        )
    if isinstance(err, AuthExpiredError):
        return (
            f"L'handshake sul pool {pool_id} ({label}) e' riuscito, ma l'API "
            "ha risposto 401.\n"
            "E' il token a essere del pool sbagliato per questo host: l'API "
            "di dev accetta solo il pool dev (M-01, accepted_pool).\n"
            "Passare --pool-id / --client-id del pool giusto per l'host in "
            "uso (vedi l'intestazione qui sopra)."
        )
    return ""


class Verdict:
    """Raccoglie gli esiti dei controlli e stampa il verdetto finale."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((status, name, detail))
        icon = {"PASS": "  ok  ", "FAIL": " FAIL ", "SKIP": " skip "}[status]
        print(f"[{icon}] {name}")
        if detail:
            for line in detail.splitlines():
                print(f"          {line}")

    def ok(self, name: str, detail: str = "") -> None:
        self.record("PASS", name, detail)

    def fail(self, name: str, detail: str = "") -> None:
        self.record("FAIL", name, detail)

    def skip(self, name: str, detail: str = "") -> None:
        self.record("SKIP", name, detail)

    def check(self, condition: bool, name: str, detail: str = "") -> bool:  # noqa: FBT001
        (self.ok if condition else self.fail)(name, detail)
        return condition

    def exit_code(self) -> int:
        failed = [row for row in self.rows if row[0] == "FAIL"]
        skipped = [row for row in self.rows if row[0] == "SKIP"]
        passed = [row for row in self.rows if row[0] == "PASS"]
        print()
        print(f"  {len(passed)} PASS, {len(failed)} FAIL, {len(skipped)} skip")
        if failed:
            print("\n  Falliti:")
            for _, name, _ in failed:
                print(f"    - {name}")
        if skipped:
            print("\n  Non osservabili in questa passata (non e' un fallimento):")
            for _, name, detail in skipped:
                print(f"    - {name}: {detail}")
        return 1 if failed else 0


def read_env_file(path: Path) -> None:
    """Carica `KEY=value` da un file nell'ambiente, senza sovrascrivere."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Username e password dall'ambiente, con i due nomi in uso nel repo."""
    read_env_file(Path(args.env_file))
    username = (
        args.username
        or os.environ.get("RADOFF_USERNAME")
        or os.environ.get("RADOFF_DEV_USERNAME")
        or ""
    )
    password = os.environ.get("RADOFF_PASSWORD") or os.environ.get(
        "RADOFF_DEV_PASSWORD", ""
    )
    if not username or not password:
        msg = (
            "Credenziali mancanti. Servono RADOFF_USERNAME e RADOFF_PASSWORD "
            "(o RADOFF_DEV_*) nell'ambiente o nel file passato a --env-file."
        )
        raise SystemExit(msg)
    return username, password


def pick_domain(api: API, forced: str | None) -> str | None:
    """Il dominio passato, o il primo che la discovery restituisce."""
    if forced:
        return forced
    try:
        domains = api.list_domains()
    except Exception as err:  # noqa: BLE001
        print(f"  Discovery non raggiungibile ({type(err).__name__}): {err}")
        return None
    prefixes = [
        entry.get("domain", {}).get("prefix")
        for entry in domains
        if entry.get("domain", {}).get("prefix")
    ]
    return prefixes[0] if prefixes else None


def check_enumeration_is_still_complete(
    verdict: Verdict, raw_devices: list[dict[str, Any]]
) -> None:
    """
    L'enumerazione che il codice conosce copre tutto cio' che dev serve.

    Il controllo che giustifica il terzo stato. Se qui comparisse un valore
    fuori da `KNOWN_CONNECTION_STATUSES`, il client lo tratterebbe come
    "non determinabile" e terrebbe le entita' disponibili - la reazione
    voluta - ma la classificazione sarebbe da aggiornare, ed e' questo il
    posto dove accorgersene invece della segnalazione di un utente.
    """
    values = collections.Counter(
        str(device.get("connection_status")) for device in raw_devices
    )
    unknown = sorted(
        value
        for value in values
        if value != "None" and value.strip().lower() not in KNOWN_CONNECTION_STATUSES
    )
    distribution = ", ".join(
        f"{value}: {count}" for value, count in values.most_common()
    )

    verdict.check(
        not unknown,
        "Ogni connection_status servito da dev e' nell'enumerazione del client",
        f"{sum(values.values())} device - {distribution}\n"
        f"il client conosce: {', '.join(sorted(KNOWN_CONNECTION_STATUSES))}"
        + (f"\nfuori enumerazione: {', '.join(unknown)}" if unknown else ""),
    )

    verdict.check(
        "None" not in values or values["None"] < sum(values.values()),
        "connection_status e' presente nel payload",
        f"assente su {values['None']} device su {sum(values.values())}",
    )


def check_status_and_connection_status_coexist(
    verdict: Verdict, raw_devices: list[dict[str, Any]]
) -> None:
    """
    I due campi sono distinti e coesistono, come T-02 D-17 (c) dice.

    La card lo pone come vincolo di lettura ("non confonderli: uno e'
    amministrativo, l'altro operativo") e questo lo misura: quanti device
    portano entrambi i campi, e con quali valori. Il caso interessante e'
    `status: active` con `connection_status: disconnected` - un device
    regolarmente in servizio che in questo momento non parla: se non
    esistesse, la distinzione sarebbe teorica.
    """
    both = [
        device
        for device in raw_devices
        if device.get("status") is not None
        and device.get("connection_status") is not None
    ]
    statuses = collections.Counter(str(device.get("status")) for device in raw_devices)
    divergent = [
        device
        for device in both
        if str(device.get("status")).lower() == "active"
        and str(device.get("connection_status")).lower() != "connected"
    ]

    verdict.check(
        len(both) == len(raw_devices),
        "status e connection_status sono entrambi presenti su ogni device",
        f"{len(both)} su {len(raw_devices)}; valori di status: "
        f"{', '.join(f'{k}: {v}' for k, v in statuses.most_common())}",
    )

    if divergent:
        verdict.ok(
            "I due campi divergono davvero (status active, non connesso)",
            f"{len(divergent)} device su {len(raw_devices)}: "
            f"{', '.join(sorted(str(d.get('serial_number')) for d in divergent)[:5])}"
            + (" ..." if len(divergent) > 5 else ""),
        )
    else:
        verdict.skip(
            "I due campi divergono davvero (status active, non connesso)",
            "nessun device in questo stato adesso: la distinzione resta "
            "documentata ma non osservata in questa passata",
        )


def check_the_silent_device_is_connected(
    verdict: Verdict, devices: list[RadoffDevice]
) -> None:
    """
    Il soggetto del primo AC esiste su dev, ed e' connesso e muto.

    La card lo indica per nome: dopo M-04 questo device espone le 10
    entita' del tipo `sense` e prima ne esponeva zero, quindi e' il primo
    posto dove il criterio "connected con telemetry: null non e' non
    disponibile" diventa osservabile su un'installazione vera.
    """
    silent = next(
        (device for device in devices if device.serial_number == SILENT_DEVICE_SERIAL),
        None,
    )
    if silent is None:
        verdict.skip(
            f"Il caso riproducibile della card ({SILENT_DEVICE_SERIAL})",
            "device assente da questo dominio in questa passata",
        )
        return

    verdict.check(
        silent.stale and silent.connection_state is ConnectionState.CONNECTED,
        f"{SILENT_DEVICE_SERIAL} e' connesso e senza telemetria: le sue "
        f"entita' restano disponibili",
        f"tipo '{silent.device_type}', connection_status "
        f"'{silent.connection_status}', stale={silent.stale}, "
        f"letture={len(silent.readings)}, status '{silent.status}'",
    )


def check_availability_verdicts(verdict: Verdict, devices: list[RadoffDevice]) -> None:
    """
    Quante entita' questa card rende disponibili, e quante ne toglie.

    Il conto che dice se la card ha l'effetto che dichiara sul dominio
    vero: i device connessi senza telemetria guadagnano entita'
    disponibili (con `unknown` o l'ultimo valore noto), i device non
    connessi le perdono anche se la loro ultima telemetria e' ancora nel
    payload - cioe' l'inverso esatto di come si comportavano fino a S-07.
    """
    connected_silent = [
        device
        for device in devices
        if device.connection_state is ConnectionState.CONNECTED and device.stale
    ]
    disconnected_with_data = [
        device
        for device in devices
        if device.connection_state is ConnectionState.DISCONNECTED and not device.stale
    ]
    indeterminate = [
        device
        for device in devices
        if device.connection_state is ConnectionState.INDETERMINATE
    ]

    verdict.ok(
        "Effetto della card sul dominio, in device",
        f"{len(devices)} device in tutto\n"
        f"{len(connected_silent)} connessi e muti: prima tutte le entita' "
        f"non disponibili, ora disponibili\n"
        f"{len(disconnected_with_data)} non connessi con telemetria in "
        f"payload: prima disponibili, ora no\n"
        f"{len(indeterminate)} non determinabili: disponibili, con WARNING",
    )


def check_status_timestamp_freshness(
    verdict: Verdict, devices: list[RadoffDevice]
) -> None:
    """
    La rete di sicurezza a 6 ore e' segnale o rumore, sui device veri.

    Il numero che questa passata esiste per misurare. Se i device connessi
    portassero sistematicamente un `connection_status_updated_at` piu'
    vecchio della finestra, il WARNING scatterebbe su tutto e la costante
    andrebbe ripensata - e sapremmo anche qualcosa sul residuo di D-17 (b),
    cioe' con quale cadenza quel campo viene aggiornato.

    Non e' un PASS/FAIL sulla salute di dev: e' un PASS se la rete resta
    silenziosa sulla maggioranza dei device connessi, con i numeri stampati
    in ogni caso.
    """
    connected = [
        device
        for device in devices
        if device.connection_state is ConnectionState.CONNECTED
    ]
    if not connected:
        verdict.skip(
            "La finestra di 6 ore non scatta sui device connessi",
            "nessun device connesso in questo dominio in questa passata",
        )
        return

    now = datetime.now(UTC)
    ages = [
        (device.serial_number, now - device.connection_status_updated_at)
        for device in connected
        if device.connection_status_updated_at is not None
    ]
    missing = len(connected) - len(ages)
    stale = [
        (serial, age) for serial, age in ages if age >= CONNECTION_STATUS_STALE_WINDOW
    ]

    if not ages:
        verdict.fail(
            "La finestra di 6 ore non scatta sui device connessi",
            f"nessuno dei {len(connected)} device connessi porta "
            f"connection_status_updated_at: la rete di sicurezza non ha "
            f"nulla da confrontare",
        )
        return

    youngest = min(age for _, age in ages)
    oldest = max(age for _, age in ages)
    verdict.check(
        len(stale) * 2 <= len(ages),
        "La finestra di 6 ore resta silenziosa sulla maggioranza dei "
        "device connessi",
        f"{len(ages)} device connessi con timestamp ({missing} senza)\n"
        f"eta' del connection_status: da {youngest} a {oldest}\n"
        f"oltre la finestra di {CONNECTION_STATUS_STALE_WINDOW}: "
        f"{len(stale)} device"
        + (
            "\n"
            + "\n".join(
                f"  {serial}: {age}"
                for serial, age in sorted(stale, key=lambda r: -r[1].total_seconds())[
                    :5
                ]
            )
            if stale
            else ""
        ),
    )


def check_telemetry_timestamp_is_still_one_per_block(
    verdict: Verdict, devices: list[RadoffDevice]
) -> None:
    """
    Il limite noto e' ancora un limite: un timestamp per blocco, non per campo.

    Se il backend avesse iniziato a servire un timestamp per campo - la
    richiesta T-08 D-15 - il ripiego documentato per il radon diventerebbe
    inutile e `last_measured_at` potrebbe dire la verita' per ciascuna
    grandezza. Vale controllarlo a ogni passata: e' cio' che sblocca il
    residuo di questa card.
    """
    with_data = [device for device in devices if device.readings]
    if not with_data:
        verdict.skip(
            "Un solo timestamp per blocco di telemetria (D-15 ancora aperta)",
            "nessun device con telemetria in questo dominio",
        )
        return

    per_field = [
        device
        for device in with_data
        if len({reading.measured_at for reading in device.readings.values()}) > 1
    ]
    verdict.check(
        not per_field,
        "Un solo timestamp per blocco di telemetria (D-15 ancora aperta)",
        f"{len(with_data)} device con letture, tutti con un timestamp unico "
        f"per l'intero blocco"
        if not per_field
        else f"{len(per_field)} device portano timestamp diversi per campo: "
        f"D-15 potrebbe essere stata implementata, rileggere il ripiego "
        f"documentato in Reading.measured_at",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--pool-id", default=DEFAULT_POOL_ID)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--pool-region", default="")
    parser.add_argument("--domain-prefix", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="  %(levelname)-8s %(name)s: %(message)s",
    )

    username, password = credentials(args)
    region = args.pool_region or (
        args.pool_id.split("_")[0] if "_" in args.pool_id else DEFAULT_POOL_REGION
    )

    print()
    print("  Verifica dal vivo degli AC di M-06 (RT-2945)")
    print(f"  host      : {args.base_url}")
    print(f"  pool      : {args.pool_id} / client {args.client_id} / {region}")
    print(f"  finestra  : {CONNECTION_STATUS_STALE_WINDOW} (rete di sicurezza)")
    print()

    verdict = Verdict()

    api = API(
        username=username,
        password=password,
        client_id=args.client_id,
        pool_id=args.pool_id,
        pool_region=region or DEFAULT_POOL_REGION,
        base_url=args.base_url,
    )

    domain = pick_domain(api, args.domain_prefix or None)
    if not domain:
        verdict.skip(
            "Gli AC di M-06 sull'API vera",
            "nessun dominio disponibile: passare --domain-prefix",
        )
        return verdict.exit_code()

    api.domain_prefix = domain
    print(f"          dominio: {domain}")
    print()

    try:
        # Il payload grezzo serve per i campi che il modello non porta
        # (`config_status`) e per contare le assenze; il modello serve per
        # la classificazione, cioe' per cio' che il codice decide davvero.
        raw_page = api._get_devices_page(1)  # noqa: SLF001
        raw_devices = [
            device for device in raw_page.get("devices", []) if isinstance(device, dict)
        ]
        devices = api.get_devices()
    except Exception as err:  # noqa: BLE001
        hint = auth_hint(err, args.pool_id)
        verdict.fail(
            "Gli AC di M-06 sull'API vera",
            f"{type(err).__name__}: {err}" + (f"\n{hint}" if hint else ""),
        )
        return verdict.exit_code()

    check_enumeration_is_still_complete(verdict, raw_devices)
    check_status_and_connection_status_coexist(verdict, raw_devices)
    check_the_silent_device_is_connected(verdict, devices)
    check_availability_verdicts(verdict, devices)
    check_status_timestamp_freshness(verdict, devices)
    check_telemetry_timestamp_is_still_one_per_block(verdict, devices)

    print()
    print("  Nota: questa passata guarda una pagina di device (page_size 200).")
    print("  Su un dominio piu' grande i conti valgono per quella pagina, non")
    print("  per il dominio intero - la paginazione e' verificata da M-03/M-05.")

    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
