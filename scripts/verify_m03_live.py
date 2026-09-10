"""
Verifica dal vivo dei criteri di accettazione di M-03 (RT-2941), su dev.

Perche' esiste
--------------
La suite automatica di M-03 gira col trasporto mockato: prova che il client
concorda con le fixture reali di M-01, non che concordi con l'API di oggi.
M-03 e' anche il punto della serie in cui il pattern di chiamata cambia -
da `1 + N` (search + una GET per device) a una sola `GET /data/devices`
paginata - e un cambio del genere si guarda una volta contro il backend
vero prima di chiudere la card.

Questo script fa esattamente quello: costruisce il client vero
(`custom_components.radoff.api.API`), chiama `get_devices()` una volta
contro `DEFAULT_BASE_URL`, e verifica gli AC della card sulla response che
torna davvero. Non e' codice spedito e non e' un test: vive in `scripts/`,
si esegue a mano con le proprie credenziali, e stampa un verdetto.

Come si esegue
--------------
    # credenziali nel proprio .env (gitignored), come RADOFF_USERNAME /
    # RADOFF_PASSWORD - oppure RADOFF_DEV_USERNAME / RADOFF_DEV_PASSWORD
    python3 scripts/verify_m03_live.py

    # su un dominio specifico, invece del primo con telemetria
    python3 scripts/verify_m03_live.py --domain-prefix 875fe89b

    # se l'app client del pool dev non ha ancora ALLOW_USER_SRP_AUTH
    # (la richiesta aperta di M-01), l'unico modo di arrivare ai dati:
    python3 scripts/verify_m03_live.py --auth-flow password \
        --pool-id eu-west-1_XXXXXXXX --client-id XXXXXXXX

Cosa NON verifica
-----------------
- Il wiring lato Home Assistant (entita', registry, availability): serve un
  HA vivo, e quello lo coprono i test di `tests/test_sensor.py` e
  `tests/test_coordinator.py`, che girano dentro `hass`.
- Con `--auth-flow password` non verifica l'handshake di autenticazione
  dell'integrazione, che usa SRP: quel flusso viene scavalcato e il token
  iniettato nella sessione. Lo script lo dice a schermo, forte, perche' un
  PASS ottenuto cosi' non e' un PASS sull'autenticazione.

Politica di stampa
------------------
Stessa di `probe_arch2.py` e di `diagnostics.py`: i serial si stampano (in
arch 2.0 sono l'identita' del device e M-01 li tiene in chiaro nelle
fixture), le etichette scritte da persone no - `name`, `room_name`,
`building_name` non compaiono mai, ne' le coordinate, ne' l'email, ne'
alcun token.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.api import API  # noqa: E402
from custom_components.radoff.api.client import (  # noqa: E402
    DEVICES_PAGE_SIZE,
    SUPPORTED_DEVICE_TYPES,
)
from custom_components.radoff.const import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
    DOMAIN,
)

# Intervalli di plausibilita' per i due valori su cui M-03 ha tolto una
# conversione (il fattore 0.00835 su internal_temperature). Non sono
# soglie di prodotto - quelle arrivano con lo schema in M-04 - sono la
# domanda "questo numero e' gia' nell'unita' dichiarata?": una temperatura
# fuori da questo intervallo vorrebbe dire che una scala serve ancora.
PLAUSIBLE_CELSIUS = (-40.0, 85.0)
PLAUSIBLE_PASCAL = (80_000.0, 110_000.0)

# Campi non-misura del blocco telemetry: non devono mai diventare letture.
NON_MEASURE_KEYS = ("timestamp", "device_type")


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
            print("\n  Non osservabili su questo dominio (non e' un fallimento):")
            for _, name, detail in skipped:
                print(f"    - {name}: {detail}")
        return 1 if failed else 0


class RequestCounter:
    """Conta le richieste HTTP del client, per path."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def hook(self, response: Any, *_args: Any, **_kwargs: Any) -> Any:
        from urllib.parse import urlsplit

        self.paths.append(urlsplit(response.request.url).path)
        return response


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


def inject_password_flow_token(api: API, username: str, password: str) -> None:
    """
    Autentica con USER_PASSWORD_AUTH e infila il token nella sessione.

    Scorciatoia deliberata, e l'unica strada finche' l'app client del pool
    dev non ha `ALLOW_USER_SRP_AUTH` (la richiesta aperta di M-01):
    l'integrazione autentica solo via SRP, che su quel client oggi non e'
    abilitato. Serve a poter guardare la chiamata dati dal vivo; non dice
    nulla sull'autenticazione dell'integrazione, e lo script lo dichiara.
    """
    import boto3

    client = boto3.client("cognito-idp", region_name=api._session.pool_region)  # noqa: SLF001
    result = client.initiate_auth(
        ClientId=api._session.client_id,  # noqa: SLF001
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )["AuthenticationResult"]

    api._session.tokens = result  # noqa: SLF001
    api._session._token_expires_at = time.time() + result["ExpiresIn"]  # noqa: SLF001


def census_domain(api: API, prefix: str) -> tuple[int, int]:
    """Ritorna (device supportati, di cui senza telemetria) per un dominio."""
    api.domain_prefix = prefix
    devices = api.get_devices()
    return len(devices), sum(1 for device in devices if device.stale)


def pick_domain(api: API, forced: str | None, max_domains: int) -> str:
    """
    Sceglie il dominio da interrogare: quello passato, o il migliore che c'e'.

    M-01 ha imparato a sue spese che "ha device" non implica "ha dati": la
    sua prima passata scelse un dominio i cui device erano tutti muti, e i
    due criteri di accettazione che riguardano i valori restarono non
    osservati. Qui il dominio si censisce invece di indovinarlo, e si
    preferisce quello che esercita entrambe le condizioni della card - un
    device che trasmette E uno con `telemetry: null` - perche' solo li'
    nessun controllo degrada a `skip`.

    Costa una richiesta per dominio, sulla stessa quota condivisa di T-02
    D-21 (50 req/s): con i 15 domini dell'account di dev e' rumore, ma il
    tetto e' esplicito in `--max-domains` per non trasformare un account
    grande in una passata lunga.
    """
    if forced:
        return forced

    domains = api.list_domains()
    prefixes = [
        entry.get("domain", {}).get("prefix")
        for entry in domains
        if entry.get("domain", {}).get("prefix")
    ]
    if not prefixes:
        msg = f"Nessun `domain.prefix` nella discovery ({len(domains)} voci)."
        raise SystemExit(msg)

    print(
        f"  Domini accessibili: {len(prefixes)}; ne censisco al massimo {max_domains}"
    )
    best: tuple[tuple[int, int], str] | None = None
    reporting_elsewhere: list[str] = []
    for prefix in prefixes[:max_domains]:
        try:
            supported, silent = census_domain(api, prefix)
        except Exception as err:  # noqa: BLE001
            print(f"    {prefix}: non interrogabile ({type(err).__name__})")
            continue
        print(f"    {prefix}: {supported} supportati, {silent} senza telemetria")
        if not supported:
            continue
        if supported > silent:
            reporting_elsewhere.append(prefix)
        # Un dominio con device che trasmettono E device muti esercita ogni
        # controllo; poi viene chi almeno trasmette, perche' da li' dipendono
        # i controlli su valori e timestamp; per ultimo chi ha solo device
        # muti. A parita' di rango vince chi ha piu' device.
        if silent and supported > silent:
            rank = 3
        elif supported > silent:
            rank = 2
        else:
            rank = 1
        score = (rank, supported)
        if best is None or score > best[0]:
            best = (score, prefix)
        if rank == 3:  # noqa: PLR2004
            break

    if best is None:
        msg = (
            "Nessuno dei domini censiti ha device di tipo supportato "
            f"({', '.join(sorted(SUPPORTED_DEVICE_TYPES))}): niente da verificare."
        )
        raise SystemExit(msg)

    chosen = best[1]
    print(f"  Scelto '{chosen}'")
    others = [prefix for prefix in reporting_elsewhere if prefix != chosen]
    if best[0][0] < 3 and others:  # noqa: PLR2004
        # Su dev nessun dominio ha insieme un device che trasmette e uno
        # muto, quindi una passata sola lascia scoperto un controllo o
        # l'altro. Dirlo qui e' meglio che lasciarlo dedurre dagli `skip`.
        print(
            "  Nessun dominio esercita insieme telemetria e telemetry:null. "
            f"Per l'altra meta': --domain-prefix {others[0]}"
        )
    return chosen


def check_one_call(verdict: Verdict, counter: RequestCounter) -> None:
    """AC: un ciclo = una richiesta (piu' una per pagina oltre la prima)."""
    device_calls = [path for path in counter.paths if path == "/data/devices"]
    detail = (
        f"chiamate a /data/devices: {len(device_calls)}; tutti i path: {counter.paths}"
    )
    verdict.check(
        len(device_calls) >= 1 and all(p == "/data/devices" for p in device_calls),
        f"Un ciclo fa {len(device_calls)} richiesta/e a /data/devices, nessuna per device",
        detail,
    )
    legacy = [p for p in counter.paths if "search" in p or p.count("/") > 2]  # noqa: PLR2004
    verdict.check(
        not legacy,
        "Nessuna chiamata agli endpoint di arch 1.x (search, dettaglio per device)",
        f"path sospetti: {legacy}" if legacy else "",
    )


def check_model(verdict: Verdict, devices: list[Any]) -> None:
    """AC: serial come identita', campi 2.0 portati, letture per nome."""
    if not devices:
        verdict.skip(
            "Modello: serial, campi 2.0, letture per nome",
            "il dominio non ha device di tipo supportato "
            f"({', '.join(sorted(SUPPORTED_DEVICE_TYPES))})",
        )
        return

    verdict.check(
        all(device.serial_number for device in devices),
        "Ogni device ha un serial_number come identita'",
        ", ".join(device.serial_number for device in devices),
    )
    verdict.check(
        all(device.device_type in SUPPORTED_DEVICE_TYPES for device in devices),
        "Ogni device modellato e' di un tipo supportato",
        ", ".join(sorted({device.device_type for device in devices})),
    )

    carried = {
        "connection_status": sum(1 for d in devices if d.connection_status is not None),
        "firmware_version": sum(1 for d in devices if d.firmware_version is not None),
        "room_name": sum(1 for d in devices if d.room_name is not None),
        "room_slug": sum(1 for d in devices if d.room_slug is not None),
        "building_name": sum(1 for d in devices if d.building_name is not None),
        "building_slug": sum(1 for d in devices if d.building_slug is not None),
        "domain_prefix": sum(1 for d in devices if d.domain_prefix is not None),
    }
    verdict.check(
        all(count == len(devices) for count in carried.values()),
        "I campi che il payload 2.0 ha aggiunto arrivano nel modello",
        # Solo i CONTEGGI: room_name/building_name sono etichette scritte
        # da persone e non vanno stampate.
        ", ".join(f"{k}={v}/{len(devices)}" for k, v in carried.items()),
    )

    misnamed = [
        f"{device.serial_number}:{field}"
        for device in devices
        for field, reading in device.readings.items()
        if field != reading.name
    ]
    verdict.check(
        not misnamed,
        "Ogni lettura e' archiviata sotto il proprio nome di campo",
        f"disallineate: {misnamed}" if misnamed else "",
    )

    leaked = [
        f"{device.serial_number}:{key}"
        for device in devices
        for key in NON_MEASURE_KEYS
        if key in device.readings
    ]
    verdict.check(
        not leaked,
        "`timestamp` e `device_type` non diventano letture",
        f"trovati: {leaked}" if leaked else "",
    )


def check_timestamps(verdict: Verdict, devices: list[Any]) -> None:
    """AC/T-08 D-15: measured_at e' il timestamp del blocco, uguale per tutti."""
    reporting = [device for device in devices if device.readings]
    if not reporting:
        verdict.skip(
            "measured_at e' il timestamp del blocco telemetry (T-08 D-15)",
            "nessun device del dominio ha telemetria in questo ciclo",
        )
        return

    problems: list[str] = []
    for device in reporting:
        stamps = {reading.measured_at for reading in device.readings.values()}
        if len(stamps) != 1:
            problems.append(f"{device.serial_number}: {len(stamps)} timestamp distinti")
            continue
        stamp = stamps.pop()
        if stamp != device.telemetry_timestamp:
            problems.append(
                f"{device.serial_number}: measured_at != telemetry_timestamp"
            )
        elif stamp is None or stamp.tzinfo is None:
            problems.append(
                f"{device.serial_number}: timestamp assente o senza timezone"
            )

    ages = [
        (datetime.now(UTC) - device.telemetry_timestamp).total_seconds() / 60
        for device in reporting
        if device.telemetry_timestamp is not None
    ]
    detail = "eta' della telemetria: " + ", ".join(
        f"{device.serial_number} {age:.0f} min"
        for device, age in zip(reporting, ages, strict=False)
    )
    verdict.check(
        not problems,
        "measured_at e' il timestamp del blocco telemetry, uguale per ogni campo",
        "\n".join(problems) if problems else detail,
    )


def check_no_scaling(verdict: Verdict, devices: list[Any]) -> None:
    """AC: i valori arrivano gia' nell'unita' dichiarata (niente 0.00835)."""
    checks = (
        ("internal_temperature", PLAUSIBLE_CELSIUS, "degC"),
        ("pressure", PLAUSIBLE_PASCAL, "Pa"),
    )
    observed: list[str] = []
    out_of_range: list[str] = []
    for device in devices:
        for field, (low, high), unit in checks:
            reading = device.readings.get(field)
            if reading is None:
                continue
            observed.append(f"{device.serial_number} {field}={reading.value} {unit}")
            if not low <= float(reading.value) <= high:
                out_of_range.append(
                    f"{device.serial_number} {field}={reading.value} "
                    f"fuori da [{low}, {high}] {unit}"
                )

    if not observed:
        verdict.skip(
            "Valori gia' nell'unita' dichiarata (nessuna scala applicata)",
            "nessun device riporta internal_temperature o pressure",
        )
        return

    verdict.check(
        not out_of_range,
        "Valori gia' nell'unita' dichiarata (nessuna scala applicata)",
        "\n".join(out_of_range) if out_of_range else "\n".join(observed),
    )


def check_silent_devices(verdict: Verdict, devices: list[Any]) -> None:
    """AC: telemetry: null -> stale=True, letture vuote, nessuna eccezione."""
    silent = [device for device in devices if device.stale]
    if not silent:
        verdict.skip(
            "Un device con telemetry: null produce stale=True",
            "nessun device supportato di questo dominio e' senza telemetria",
        )
        return
    verdict.check(
        all(not device.readings for device in silent),
        f"I {len(silent)} device senza telemetria sono stale, con letture vuote",
        ", ".join(device.serial_number for device in silent),
    )


def check_unique_ids(verdict: Verdict, devices: list[Any]) -> None:
    """AC: unique_id e' radoff-{serial_number}-{campo}."""
    reporting = [device for device in devices if device.readings]
    if not reporting:
        verdict.skip(
            "unique_id e' radoff-{serial_number}-{campo}",
            "nessuna lettura da cui costruirne uno",
        )
        return

    try:
        from types import SimpleNamespace

        from custom_components.radoff.entity import RadoffEntity
    except ImportError as err:  # pragma: no cover - dipende dall'ambiente
        verdict.skip("unique_id e' radoff-{serial_number}-{campo}", str(err))
        return

    device = reporting[0]
    field = sorted(device.readings)[0]
    stub = SimpleNamespace(_serial_number=device.serial_number, field=field)
    built = RadoffEntity.unique_id.fget(stub)
    expected = f"{DOMAIN}-{device.serial_number}-{field}"
    verdict.check(
        built == expected, "unique_id e' radoff-{serial_number}-{campo}", built
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica dal vivo degli AC di M-03 (RT-2941) contro dev."
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--username", default=None)
    parser.add_argument("--domain-prefix", default=None)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--pool-id", default=DEFAULT_POOL_ID)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--pool-region", default=None)
    parser.add_argument(
        "--auth-flow",
        choices=("srp", "password"),
        default="srp",
        help=(
            "srp: come fa l'integrazione. password: scavalca l'handshake e "
            "inietta un token USER_PASSWORD_AUTH, unica strada finche' il "
            "pool dev non abilita ALLOW_USER_SRP_AUTH (richiesta di M-01)."
        ),
    )
    parser.add_argument(
        "--max-domains",
        type=int,
        default=15,
        help=(
            "Quanti domini censire al massimo per sceglierne uno, quando "
            "--domain-prefix non e' passato. Una richiesta per dominio."
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="          log | %(levelname)s %(name)s: %(message)s",
    )

    username, password = credentials(args)
    region = args.pool_region or args.pool_id.partition("_")[0]

    print()
    print("  Verifica dal vivo degli AC di M-03 (RT-2941)")
    print(f"  host      : {args.base_url}")
    print(f"  pool      : {args.pool_id} / client {args.client_id} / {region}")
    print(f"  auth flow : {args.auth_flow}")
    if args.auth_flow == "password":
        print()
        print("  ATTENZIONE: l'handshake SRP dell'integrazione e' SCAVALCATO.")
        print("  Questa passata NON verifica l'autenticazione, solo la")
        print("  chiamata dati e il modello. Vedi la richiesta aperta di M-01")
        print("  su ALLOW_USER_SRP_AUTH per il pool dev.")
    print()

    api = API(
        username=username,
        password=password,
        client_id=args.client_id,
        pool_id=args.pool_id,
        pool_region=region or DEFAULT_POOL_REGION,
        base_url=args.base_url,
    )

    if args.auth_flow == "password":
        inject_password_flow_token(api, username, password)

    verdict = Verdict()

    try:
        domain_prefix = pick_domain(api, args.domain_prefix, args.max_domains)
    except Exception as err:  # noqa: BLE001
        print(f"\n  Impossibile arrivare alla discovery: {type(err).__name__}: {err}")
        if args.auth_flow == "srp":
            print(
                "  Se e' un errore di autenticazione, e' probabilmente la "
                "richiesta aperta di M-01:\n  l'app client del pool dev non ha "
                "ALLOW_USER_SRP_AUTH. Riprova con --auth-flow password."
            )
        return 1

    # Il contatore si attacca DOPO il censimento: quello che va misurato e'
    # il ciclo di poll, non le richieste che questo script fa per scegliere
    # dove guardare.
    api.domain_prefix = domain_prefix
    counter = RequestCounter()
    api.session.hooks["response"].append(counter.hook)

    print(f"  Dominio interrogato: {domain_prefix}, page_size={DEVICES_PAGE_SIZE}")
    print()

    started = time.monotonic()
    try:
        devices = api.get_devices()
    except Exception as err:  # noqa: BLE001
        verdict.fail(
            "get_devices() completa senza sollevare",
            f"{type(err).__name__}: {err}",
        )
        return verdict.exit_code()
    elapsed_ms = (time.monotonic() - started) * 1000

    verdict.ok(
        "get_devices() completa senza sollevare",
        f"{len(devices)} device supportati in {elapsed_ms:.0f} ms",
    )

    check_one_call(verdict, counter)
    check_model(verdict, devices)
    check_timestamps(verdict, devices)
    check_no_scaling(verdict, devices)
    check_silent_devices(verdict, devices)
    check_unique_ids(verdict, devices)

    print()
    print("  Nota: uno slave LIFE annidato in controller_of_device, se il")
    print("  dominio ne avesse uno, comparirebbe come riga INFO qui sopra")
    print("  ('controls a nested ... device'). Con `city` e `life` fuori")
    print("  scopo la maggior parte dei domini non ne ha.")

    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
