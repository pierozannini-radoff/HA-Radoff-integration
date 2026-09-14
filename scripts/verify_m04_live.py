"""
Verifica dal vivo dei criteri di accettazione di M-04 (RT-2942), su dev.

Perche' esiste
--------------
La suite automatica di M-04 gira contro le fixture di M-01: prova che il
codice concorda con la response che l'API dava il 10 settembre 2026, non
con quella che da' oggi. Ed e' esattamente il punto della card: le unita',
le etichette e le soglie non sono piu' nostre, sono del backend. Se il
backend le cambia, la suite resta verde e l'integrazione cambia
comportamento - e' il pregio di questa architettura, non un difetto, ma
significa che una passata contro l'API vera prima di chiudere la card
verifica qualcosa che nessun test mockato puo' verificare.

Cosa guarda, in una frase: che lo schema servito oggi sia ancora quello su
cui questa card e' stata scritta, e che ogni AC che dipende dalla response
regga sulla response vera.

Come si esegue
--------------
    # credenziali nel proprio .env (gitignored), come RADOFF_USERNAME /
    # RADOFF_PASSWORD - oppure RADOFF_DEV_USERNAME / RADOFF_DEV_PASSWORD
    python3 scripts/verify_m04_live.py

    # i due override di pool servono finche' const.py punta a un pool
    # diverso dall'ambiente di DEFAULT_BASE_URL
    python3 scripts/verify_m04_live.py \
        --pool-id eu-west-1_XXXXXXXX --client-id XXXXXXXX \
        --domain-prefix XXXXXXXX

    # --auth-flow password NON serve piu': ALLOW_USER_SRP_AUTH e' stato
    # abilitato sul pool dev (RT-2952, verificato il 2026-09-10). Resta
    # come ripiego se quel flag dovesse tornare indietro.

    # su un dominio specifico, invece del primo con device
    python3 scripts/verify_m04_live.py --domain-prefix 875fe89b

Lo schema non e' scoped per dominio: i controlli sul catalogo (ordinamento,
fasce, unita', 404, deriva rispetto a M-01) girano anche su un account
senza device. Il dominio serve solo ai due controlli che confrontano lo
schema con dei device veri.

Cosa NON verifica
-----------------
- Il wiring lato Home Assistant: quali entita' nascono, come si chiamano,
  quali sono disabilitate. Serve un HA vivo, e lo coprono `test_sensor.py`
  e `test_init.py`, che girano dentro `hass`. Qui si verifica cio' che
  l'integrazione *deduce* dalla response, che e' l'input di quel wiring.
- `sismoff`: escluso da questa card per decisione presa il 2026-09-10.
  Il suo schema viene comunque letto e
  confrontato con la fixture, perche' costa una richiesta e la deriva del
  catalogo si misura meglio tutta insieme; nessun controllo sulle entita'
  di un sismoff.
- Con `--auth-flow password` non verifica l'handshake SRP: viene
  scavalcato e il token iniettato. Lo script lo dice a schermo, forte -
  ed e' il motivo per cui quel ripiego non va usato senza bisogno, ora
  che SRP su dev funziona.

Politica di stampa
------------------
Stessa di `probe_arch2.py` e di `verify_m03_live.py`: i serial si stampano,
le etichette scritte da persone no - `name`, `room_name`, `building_name`
non compaiono mai, ne' le coordinate, ne' l'email, ne' alcun token. Le
etichette che stampa sono quelle di `measures-ranges` ("Volatile organic
compounds"), che descrivono un prodotto e non una persona: M-01 aveva gia'
deciso di tenerle in chiaro nelle fixture per la stessa ragione.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.api import API, APIUnknownDeviceTypeError  # noqa: E402
from custom_components.radoff.api.client import (  # noqa: E402
    MEASURES_RANGES_PATH,
    SUPPORTED_DEVICE_TYPES,
)
from custom_components.radoff.const import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
)
from custom_components.radoff.schema import (  # noqa: E402
    DEVICE_CLASSES,
    HA_UNITS,
    build_specs,
)
from custom_components.radoff.sensor import DISABLED_BY_DEFAULT  # noqa: E402

# I tipi del catalogo, come M-01 li ha censiti. `life` non ha uno schema su
# dev (404), ed e' incluso apposta: e' il caso reale di "tipo che il
# catalogo non riconosce" e serve al controllo sul 404.
CATALOGUE_TYPES = ("nowplus", "sense", "city", "now", "sismoff")
NO_SCHEMA_TYPE = "life"
UNKNOWN_TYPE = "not-a-real-device-type"

# Le fixture di M-01 contro cui si misura la deriva del catalogo.
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "dev"

# Fuori scope in questa card, per decisione del 2026-09-10.
EXCLUDED_FROM_CARD = ("sismoff",)


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

    Identica a quella di `verify_m03_live.py`. Era l'unico modo di arrivare
    ai dati finche' l'app client del pool dev non aveva `ALLOW_USER_SRP_AUTH`;
    dal 2026-09-10 quel flag c'e' (RT-2952) e questa scorciatoia serve solo
    se dovesse tornare indietro. Non dice nulla sull'autenticazione
    dell'integrazione, e lo script lo dichiara a schermo.
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


def fetch_catalogue(api: API, verdict: Verdict) -> dict[str, dict[str, Any]]:
    """Una `measures-ranges` per tipo del catalogo. Ritorna i payload grezzi."""
    payloads: dict[str, dict[str, Any]] = {}
    for device_type in CATALOGUE_TYPES:
        try:
            payloads[device_type] = api.get_measures_ranges(device_type)
        except APIUnknownDeviceTypeError as err:
            verdict.fail(
                f"Schema del tipo '{device_type}' disponibile",
                f"il catalogo non lo riconosce piu': available={err.available}",
            )
        except Exception as err:  # noqa: BLE001
            verdict.fail(
                f"Schema del tipo '{device_type}' disponibile",
                f"{type(err).__name__}: {err}",
            )

    verdict.check(
        len(payloads) == len(CATALOGUE_TYPES),
        f"Lo schema risponde per tutti e {len(CATALOGUE_TYPES)} i tipi del catalogo",
        ", ".join(f"{t}={len(p)} misure" for t, p in payloads.items()),
    )
    return payloads


def check_one_call_per_type(verdict: Verdict, counter: RequestCounter) -> None:
    """AC: una chiamata per tipo, non per device."""
    schema_calls = [p for p in counter.paths if p == MEASURES_RANGES_PATH]
    verdict.check(
        len(schema_calls) == len(counter.paths),
        f"Le {len(schema_calls)} richieste della passata sono tutte allo schema",
        f"path visti: {sorted(set(counter.paths))}",
    )


def check_pos_ordering(verdict: Verdict, payloads: dict[str, dict[str, Any]]) -> None:
    """
    AC: le misure escono ordinate per `pos`, e un `pos` non contiguo non rompe.

    La trappola segnalata dal backend. Si verifica su ogni tipo, e si
    controlla anche che i buchi ci siano davvero: se un giorno il backend
    rendesse `pos` contiguo, il controllo passerebbe senza piu' dimostrare
    nulla, e vale la pena saperlo.
    """
    problems: list[str] = []
    observed: list[str] = []
    sparse_types: list[str] = []

    for device_type, payload in payloads.items():
        specs = build_specs(payload, device_type=device_type)
        positions = [spec.pos for spec in specs.values()]

        if None in positions:
            problems.append(f"{device_type}: una misura senza `pos`")
            continue
        if positions != sorted(positions):
            problems.append(f"{device_type}: ordine non crescente {positions}")
        if len(set(positions)) != len(positions):
            problems.append(f"{device_type}: `pos` duplicato in {positions}")
        if positions and max(positions) > len(positions):
            sparse_types.append(device_type)
        observed.append(f"{device_type}: pos={positions}")

    verdict.check(
        not problems,
        "Le misure escono ordinate per `pos`",
        "\n".join(problems) if problems else "\n".join(observed),
    )
    verdict.check(
        bool(sparse_types),
        "`pos` e' ancora sparso su almeno un tipo (la trappola e' viva)",
        f"tipi con buchi: {', '.join(sparse_types)}"
        if sparse_types
        else "nessun tipo ha buchi: `pos` e' diventato contiguo, "
        "il controllo non dimostra piu' nulla",
    )


def check_band_order(verdict: Verdict, payloads: dict[str, dict[str, Any]]) -> None:
    """AC: l'ordine delle fasce e' quello servito, con `high` in seconda."""
    five_band = ("excellent", "high", "good", "poor", "terrible")
    three_band = ("low", "good", "high")

    unexpected: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for device_type, payload in payloads.items():
        for name, spec in build_specs(payload, device_type=device_type).items():
            if not spec.ranges:
                continue
            seen.add(spec.statuses)
            if spec.statuses not in (five_band, three_band):
                unexpected.append(f"{device_type}.{name}: {spec.statuses}")

    verdict.check(
        not unexpected,
        "Le fasce arrivano nel vocabolario e nell'ordine attesi",
        "\n".join(unexpected)
        if unexpected
        else "\n".join(" -> ".join(order) for order in sorted(seen)),
    )


def check_no_scale_factor(
    verdict: Verdict, payloads: dict[str, dict[str, Any]]
) -> None:
    """AC/T-08: `scaleFactor` non c'e', e il client non applica nulla."""
    found = [
        f"{device_type}.{name}"
        for device_type, payload in payloads.items()
        for name, measure in payload.items()
        if isinstance(measure, dict) and "scaleFactor" in measure
    ]
    verdict.check(
        not found,
        "Nessuna misura serve uno `scaleFactor` (i valori sono gia' scalati)",
        f"comparso su: {found}" if found else "",
    )


def check_units(verdict: Verdict, payloads: dict[str, dict[str, Any]]) -> None:
    """
    AC: ogni unita' servita e' mappata; una fuori mappa non fa fallire nulla.

    Il controllo che invecchia peggio di tutti, ed e' il motivo per cui
    questo script esiste: l'enumerazione delle unita' e' del backend, e il
    giorno che ne aggiunge una l'integrazione continua a funzionare (entita'
    senza unita', WARNING nel log) ma qualcuno deve accorgersene. Qui.
    """
    served: dict[str, list[str]] = {}
    for device_type, payload in payloads.items():
        for name, measure in payload.items():
            if isinstance(measure, dict):
                served.setdefault(measure.get("unit"), []).append(
                    f"{device_type}.{name}"
                )

    unmapped = {unit: where for unit, where in served.items() if unit not in HA_UNITS}
    verdict.check(
        not unmapped,
        f"Tutte le {len(served)} unita' servite sono mappate su Home Assistant",
        "\n".join(f"{unit!r}: {', '.join(where)}" for unit, where in unmapped.items())
        if unmapped
        else ", ".join(sorted(repr(unit) for unit in served)),
    )

    pressure = [
        build_specs(payload, device_type=device_type).get("pressure")
        for device_type, payload in payloads.items()
    ]
    pressure_units = {spec.unit for spec in pressure if spec is not None}
    verdict.check(
        pressure_units == {"Pa"},
        "AC: `pressure` e' servita in Pa (la conversione la fa la UI di HA)",
        f"unita' viste: {sorted(pressure_units)}",
    )


def check_device_classes(verdict: Verdict, payloads: dict[str, dict[str, Any]]) -> None:
    """AC: le misure senza una device_class onesta non ne hanno una."""
    without = ("radon_bqm3", "aqi_value", "ch4", "tvoc")
    wrong = [measure for measure in without if measure in DEVICE_CLASSES]
    verdict.check(
        not wrong,
        "radon/aqi/ch4/tvoc restano senza device_class (scala o unita' non compatibili)",
        f"hanno una device_class: {wrong}" if wrong else "",
    )

    declared = {
        name
        for payload in payloads.values()
        for name, measure in payload.items()
        if isinstance(measure, dict)
    }
    orphan_classes = sorted(set(DEVICE_CLASSES) - declared)
    verdict.check(
        not orphan_classes,
        "Ogni device_class mappata corrisponde a una misura che il catalogo serve",
        f"mappate ma non servite: {orphan_classes}" if orphan_classes else "",
    )

    verdict.check(
        "aqi_value" in DISABLED_BY_DEFAULT,
        "AC: `aqi_value` nasce disabilitata (T-08 D-08, divisore 120)",
        "riabilitare per default quando il backend corregge il calcolo",
    )


def check_unknown_type(verdict: Verdict, api: API) -> None:
    """AC: un `device_type` sconosciuto e' un 404 gestito, non un errore fatale."""
    for device_type in (NO_SCHEMA_TYPE, UNKNOWN_TYPE):
        try:
            payload = api.get_measures_ranges(device_type)
        except APIUnknownDeviceTypeError as err:
            verdict.check(
                bool(err.available),
                f"Tipo '{device_type}': 404 con la lista dei tipi validi",
                f"available={err.available}",
            )
        except Exception as err:  # noqa: BLE001
            verdict.fail(
                f"Tipo '{device_type}': 404 riconosciuto come tipo ignoto",
                f"e' arrivato invece {type(err).__name__}: {err}",
            )
        else:
            verdict.fail(
                f"Tipo '{device_type}': 404 con la lista dei tipi validi",
                f"ha risposto 200 con {len(payload)} misure: il catalogo e' cambiato",
            )


def check_drift_from_fixtures(
    verdict: Verdict, payloads: dict[str, dict[str, Any]]
) -> None:
    """
    Lo schema servito oggi e' ancora quello su cui la card e' stata scritta?

    Il controllo che nessun test mockato puo' fare. Le fixture di M-01 sono
    la fotografia del 2026-09-10; se il backend ha cambiato una soglia,
    un'unita' o un'etichetta, l'integrazione lo segue in silenzio - ed e'
    quello che vogliamo - ma la card va chiusa sapendolo, e le fixture
    vanno ricatturate.
    """
    drifted: list[str] = []
    compared = 0
    for device_type, payload in payloads.items():
        fixture_path = FIXTURES_DIR / f"measures_ranges__{device_type}.json"
        if not fixture_path.is_file():
            drifted.append(f"{device_type}: nessuna fixture di riferimento")
            continue
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        compared += 1
        if fixture == payload:
            continue

        only_live = sorted(set(payload) - set(fixture))
        only_fixture = sorted(set(fixture) - set(payload))
        changed = sorted(
            name
            for name in set(payload) & set(fixture)
            if payload[name] != fixture[name]
        )
        drifted.append(
            f"{device_type}: nuove={only_live or '-'} "
            f"sparite={only_fixture or '-'} cambiate={changed or '-'}"
        )

    verdict.check(
        not drifted,
        f"Lo schema servito coincide con le fixture di M-01 ({compared} tipi)",
        "\n".join(drifted)
        if drifted
        else "nessuna deriva: soglie, unita' ed etichette sono quelle della card",
    )


def check_translations(verdict: Verdict, payloads: dict[str, dict[str, Any]]) -> None:
    """
    AC: ogni misura servita ha un nome, in italiano e in inglese.

    `test_translations.py` lo verifica gia', ma contro la fixture. Qui
    contro il catalogo di oggi: una misura che il backend aggiunge produce
    un'entita' senza nome, e questo e' il posto dove accorgersene prima che
    lo faccia un utente.
    """
    served = {
        name
        for payload in payloads.values()
        for name, measure in payload.items()
        if isinstance(measure, dict)
    }
    banded = {
        f"{name}_index"
        for payload in payloads.values()
        for name, measure in payload.items()
        if isinstance(measure, dict) and measure.get("ranges")
    }

    radoff_dir = REPO_ROOT / "custom_components" / "radoff"
    paths = [
        radoff_dir / "strings.json",
        *sorted((radoff_dir / "translations").glob("*.json")),
    ]

    missing: list[str] = []
    for path in paths:
        entities = json.loads(path.read_text(encoding="utf-8")).get("entity", {})
        sensors = entities.get("sensor", {})
        for slug in sorted(served | banded):
            if not sensors.get(slug, {}).get("name"):
                missing.append(f"{path.name}: {slug}")

    verdict.check(
        not missing,
        f"Le {len(served | banded)} entita' del catalogo hanno un nome in ogni lingua",
        "\n".join(missing) if missing else ", ".join(p.name for p in paths),
    )


def check_devices_against_schema(
    verdict: Verdict, api: API, payloads: dict[str, dict[str, Any]], domain: str | None
) -> None:
    """
    AC: nessun device sparisce, e le sue letture stanno dentro il suo schema.

    Due controlli su device veri, gli unici di questa passata che hanno
    bisogno di un dominio. Il primo e' l'AC di M-04 che ha cambiato
    `_build_device`: ogni entry della response diventa un device, anche di
    un tipo che il catalogo non conosce. Il secondo guarda dall'altro lato -
    una lettura che nessuno schema dichiara diventa un'entita' grezza, e va
    saputo quali sono (su dev ci aspettiamo al massimo `radon_status`).
    """
    if domain is None:
        verdict.skip(
            "Nessun device sparisce, e le letture stanno dentro lo schema",
            "nessun dominio interrogabile con questo account",
        )
        return

    api.domain_prefix = domain
    devices = api.get_devices()
    if not devices:
        verdict.skip(
            "Nessun device sparisce, e le letture stanno dentro lo schema",
            f"il dominio '{domain}' non ha device",
        )
        return

    types_seen = sorted({device.device_type for device in devices})
    verdict.check(
        all(device.serial_number for device in devices),
        f"I {len(devices)} device del dominio sono tutti modellati",
        f"tipi presenti: {types_seen}; "
        f"fuori dal tipo supportato ({', '.join(sorted(SUPPORTED_DEVICE_TYPES))}): "
        f"{[t for t in types_seen if t not in SUPPORTED_DEVICE_TYPES] or '-'}",
    )

    undeclared: list[str] = []
    for device in devices:
        specs = build_specs(
            payloads.get(device.device_type, {}), device_type=device.device_type
        )
        undeclared.extend(
            f"{device.serial_number}.{field}"
            for field in device.readings
            if field not in specs
        )

    verdict.check(
        not undeclared,
        "Ogni lettura dei device e' dichiarata dallo schema del suo tipo",
        "esposte come valore grezzo, senza unita' ne' fasce:\n" + "\n".join(undeclared)
        if undeclared
        else "",
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verifica dal vivo degli AC di M-04 (RT-2942) contro dev."
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
            "srp: come fa l'integrazione, ed e' il default perche' dal "
            "2026-09-10 funziona anche su dev (RT-2952). password: scavalca "
            "l'handshake e inietta un token USER_PASSWORD_AUTH - ripiego, "
            "da usare solo se ALLOW_USER_SRP_AUTH sparisse di nuovo."
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
    print("  Verifica dal vivo degli AC di M-04 (RT-2942)")
    print(f"  host      : {args.base_url}")
    print(f"  pool      : {args.pool_id} / client {args.client_id} / {region}")
    print(f"  auth flow : {args.auth_flow}")
    print(f"  fuori card: {', '.join(EXCLUDED_FROM_CARD)} (decisione 2026-09-10)")
    if args.auth_flow == "password":
        print()
        print("  ATTENZIONE: l'handshake SRP dell'integrazione e' SCAVALCATO.")
        print("  Questa passata NON verifica l'autenticazione, solo lo schema")
        print("  e il modello. Vedi la richiesta aperta di M-01 su")
        print("  ALLOW_USER_SRP_AUTH per il pool dev.")
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

    # Il contatore misura solo la fase "catalogo": e' li' che l'AC parla di
    # una chiamata per tipo. Le richieste del dominio si attaccano dopo.
    counter = RequestCounter()
    api.session.hooks["response"].append(counter.hook)

    started = time.monotonic()
    try:
        payloads = fetch_catalogue(api, verdict)
    except Exception as err:  # noqa: BLE001
        print(f"\n  Impossibile leggere lo schema: {type(err).__name__}: {err}")
        if args.auth_flow == "srp":
            print(
                "  Se e' un errore di autenticazione, e' probabilmente la "
                "richiesta aperta di M-01:\n  l'app client del pool dev non ha "
                "ALLOW_USER_SRP_AUTH. Riprova con --auth-flow password."
            )
        return 1
    elapsed_ms = (time.monotonic() - started) * 1000
    print(f"          {len(CATALOGUE_TYPES)} tipi letti in {elapsed_ms:.0f} ms")

    if not payloads:
        print("\n  Nessuno schema letto: non c'e' nulla da verificare.")
        return verdict.exit_code()

    check_one_call_per_type(verdict, counter)
    check_pos_ordering(verdict, payloads)
    check_band_order(verdict, payloads)
    check_no_scale_factor(verdict, payloads)
    check_units(verdict, payloads)
    check_device_classes(verdict, payloads)
    check_drift_from_fixtures(verdict, payloads)
    check_translations(verdict, payloads)
    check_unknown_type(verdict, api)

    counter.paths.clear()
    try:
        check_devices_against_schema(
            verdict, api, payloads, pick_domain(api, args.domain_prefix)
        )
    except Exception as err:  # noqa: BLE001
        verdict.fail(
            "Nessun device sparisce, e le letture stanno dentro lo schema",
            f"{type(err).__name__}: {err}",
        )

    print()
    print("  Nota: quali entita' nascano davvero (nomi, `_index`, AQI")
    print("  disabilitata) dipende dal wiring di Home Assistant e non e'")
    print("  osservabile da qui: lo coprono tests/test_sensor.py e")
    print("  tests/test_init.py, che girano dentro `hass`.")

    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
