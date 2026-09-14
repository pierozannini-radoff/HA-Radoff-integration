"""
Verifica dal vivo, su dev, del budget di richieste di un ciclo di polling.

Lo scheduling e' quasi tutto locale - l'offset di un'installazione, il
minimo del form, il comportamento dopo un 429 - e vive nella suite mockata,
che lo verifica meglio e senza toccare l'API. Quello che una suite mockata
non puo' verificare e' il numero su cui poggia il resto: che un ciclo,
sull'API vera e sul dominio vero, costi **una richiesta**. E'
l'affermazione che il README fa in pubblico ("una richiesta ogni N secondi,
piu' una per tipo di device a ogni riavvio") ed e' la ragione per cui 300 s
e' difendibile su una quota condivisa. Se il backend cambiasse il
`page_size` massimo o smettesse di servire la telemetria inline, la suite
resterebbe verde e quel numero diventerebbe falso.

Come si esegue
--------------
    # credenziali nel proprio .env (gitignored), come RADOFF_USERNAME /
    # RADOFF_PASSWORD - oppure RADOFF_DEV_USERNAME / RADOFF_DEV_PASSWORD
    python3 scripts/verify_m05_live.py --domain-prefix 875fe89b

    # i due override di pool servono finche' const.py punta a un pool
    # diverso dall'ambiente di DEFAULT_BASE_URL
    python3 scripts/verify_m05_live.py \
        --pool-id eu-west-1_XXXXXXXX --client-id XXXXXXXX \
        --domain-prefix XXXXXXXX

`--domain-prefix` non e' facoltativo se si vogliono tutti i controlli:
senza, si prende il primo dominio della discovery, che su questo account e'
vuoto, e i controlli sul costo di un ciclo finiscono in `skip`.

Cosa NON verifica, e perche'
----------------------------
- **Il 429.** Non e' provocabile in modo onesto: il tetto e' 50 req/s
  steady per *stage*, condiviso con l'app mobile e con il web, e
  saturarlo per vedere l'errore significherebbe degradare il servizio di
  chiunque altro stia usando dev in quel momento. Il comportamento dopo un
  429 e' verificato in `tests/test_coordinator.py` con una response
  montata, nella forma del corpo che il backend ha documentato. Qui compare
  come `skip` esplicito, non come `PASS`.
- **La cadenza reale nel tempo.** Osservare che due cicli distino davvero
  300 s vorrebbe dire tenere lo script acceso per un quarto d'ora per
  guardare un timer di Home Assistant: e' il framework a garantirlo, e la
  delega e' verificata in `tests/test_coordinator.py`.
- **L'offset fra installazioni.** E' aritmetica locale sull'entry_id.
  Questo script la calcola su entry_id sintetici e la stampa (nessuna
  richiesta), perche' e' l'unico controllo che si legge meglio con dei
  numeri davanti; non e' una verifica dal vivo e viene dichiarato tale.

Politica di stampa
------------------
Stessa di `probe_arch2.py`, `verify_m03_live.py` e `verify_m04_live.py`: i
serial si stampano, le etichette scritte da persone no - `name`,
`room_name`, `building_name` non compaiono mai, ne' le coordinate, ne'
l'email, ne' alcun token.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.api import API  # noqa: E402
from custom_components.radoff.api.client import (  # noqa: E402
    DEVICES_PAGE_SIZE,
    DEVICES_PATH,
    MEASURES_RANGES_PATH,
)
from custom_components.radoff.const import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    POLL_JITTER_FRACTION,
    UPDATE_TIMEOUT_FACTOR,
)
from custom_components.radoff.coordinator import _stable_fraction  # noqa: E402

# Il tetto del catalogo: cinque tipi, quindi al massimo cinque richieste di
# schema a ogni riavvio, qualunque sia il numero di device.
CATALOGUE_SIZE = 5

# Quanti entry_id sintetici usare per mostrare la dispersione dell'offset.
JITTER_SAMPLE = 1000


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
        self.paths.append(urlsplit(response.request.url).path)
        return response

    def count(self, path: str) -> int:
        return sum(1 for seen in self.paths if seen == path)


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


def check_one_request_per_cycle(
    verdict: Verdict, api: API, counter: RequestCounter
) -> list[Any]:
    """
    Il numero su cui poggia tutto il resto: un ciclo, una richiesta.

    E' il presupposto di ogni numero scritto nel README e nel form, e vale
    rimisurarlo sull'API vera prima di pubblicarlo.
    """
    counter.paths.clear()
    started = time.monotonic()
    devices = api.get_devices()
    elapsed = time.monotonic() - started

    calls = counter.count(DEVICES_PATH)
    expected = max(1, -(-len(devices) // DEVICES_PAGE_SIZE))
    verdict.check(
        calls == expected and counter.count(MEASURES_RANGES_PATH) == 0,
        "Un ciclo di poll costa una richiesta (piu' una per pagina oltre la prima)",
        f"{len(devices)} device serviti in {calls} richiesta/e a {DEVICES_PATH} "
        f"(page_size={DEVICES_PAGE_SIZE}), {elapsed * 1000:.0f} ms",
    )

    budget = DEFAULT_SCAN_INTERVAL * UPDATE_TIMEOUT_FACTOR
    verdict.check(
        elapsed < budget,
        f"Il ciclo sta dentro il budget di {budget:.0f}s",
        f"misurato {elapsed:.2f}s, cioe' il {elapsed / budget * 100:.2f}% del budget",
    )
    return devices


def check_setup_cost(
    verdict: Verdict, api: API, counter: RequestCounter, devices: list[Any]
) -> None:
    """
    L'altra meta' del budget: una richiesta per tipo, a ogni riavvio.

    Il setup costa una `measures-ranges` per tipo distinto, e il tetto e' il
    catalogo - cinque - qualunque sia il numero di device. E' cio' che il
    README promette; si misura contando le richieste, non fidandosi.
    """
    types = sorted({device.device_type for device in devices if device.device_type})
    if not types:
        verdict.skip(
            "Il setup costa una richiesta per tipo di device",
            "nessun device su questo dominio: non c'e' nessun tipo da chiedere",
        )
        return

    counter.paths.clear()
    for device_type in types:
        try:
            api.get_measures_ranges(device_type)
        except Exception as err:  # noqa: BLE001
            verdict.fail(
                "Il setup costa una richiesta per tipo di device",
                f"schema del tipo '{device_type}': {type(err).__name__}: {err}",
            )
            return

    calls = counter.count(MEASURES_RANGES_PATH)
    verdict.check(
        calls == len(types) <= CATALOGUE_SIZE,
        "Il setup costa una richiesta per tipo di device, non per device",
        f"{len(devices)} device, {len(types)} tipo/i ({', '.join(types)}), "
        f"{calls} richiesta/e; tetto del catalogo: {CATALOGUE_SIZE}",
    )


def check_page_size_is_honoured(verdict: Verdict, api: API) -> None:
    """
    Il `page_size` che chiediamo e' quello che il backend applica.

    Se il tetto scendesse sotto 200, un dominio grande costerebbe piu'
    richieste per ciclo di quante il README ne dichiari - senza che nessun
    test se ne accorga, perche' la paginazione continuerebbe a funzionare.
    """
    page = api._get_devices_page(1)  # noqa: SLF001
    pagination = page.get("pagination") or {}
    served = pagination.get("page_size")
    verdict.check(
        served == DEVICES_PAGE_SIZE,
        f"Il backend applica il page_size richiesto ({DEVICES_PAGE_SIZE})",
        f"pagination servita: page_size={served}, total={pagination.get('total')}, "
        f"total_pages={pagination.get('total_pages')}",
    )


def report_jitter_spread(verdict: Verdict) -> None:
    """
    Aritmetica locale, stampata perche' si legge meglio con i numeri davanti.

    Non tocca la rete e non e' una verifica dal vivo: e' la dispersione che
    l'offset produce su entry_id sintetici, cioe' cosa vede il backend
    quando molte installazioni partono insieme.
    """
    offsets = sorted(
        _stable_fraction(f"entry-{i}") * POLL_JITTER_FRACTION * DEFAULT_SCAN_INTERVAL
        for i in range(JITTER_SAMPLE)
    )
    ceiling = DEFAULT_SCAN_INTERVAL * POLL_JITTER_FRACTION
    distinct = len(set(offsets))
    verdict.check(
        distinct == JITTER_SAMPLE and 0 <= offsets[0] and offsets[-1] < ceiling,
        f"L'offset disperde {JITTER_SAMPLE} installazioni su [0, {ceiling:.0f}s)",
        f"{distinct} valori distinti; min {offsets[0]:.2f}s, "
        f"mediana {offsets[JITTER_SAMPLE // 2]:.2f}s, max {offsets[-1]:.2f}s "
        f"(calcolo locale sull'entry_id, nessuna richiesta)",
    )


def report_rate_limit_not_provoked(verdict: Verdict) -> None:
    """Il 429 non si prova qui, e la ragione va scritta, non sottintesa."""
    verdict.skip(
        "Un 429 salta il ciclo e non marca le entita' non disponibili",
        "il tetto e' 50 req/s per stage, condiviso con l'app mobile e il web "
        "saturarlo per vedere l'errore degraderebbe il servizio di "
        "chiunque altro stia usando dev. Verificato in "
        "tests/test_coordinator.py con una response montata",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--pool-id", default=DEFAULT_POOL_ID)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--pool-region", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--domain-prefix", default="")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="          log | %(levelname)s %(name)s: %(message)s",
    )

    username, password = credentials(args)
    region = args.pool_region or args.pool_id.partition("_")[0]

    print()
    print("  Verifica dal vivo del budget di richieste")
    print(f"  host      : {args.base_url}")
    print(f"  pool      : {args.pool_id} / client {args.client_id} / {region}")
    print(
        f"  intervalli: minimo {MIN_SCAN_INTERVAL}s, default "
        f"{DEFAULT_SCAN_INTERVAL}s, offset fino a "
        f"{timedelta(seconds=DEFAULT_SCAN_INTERVAL * POLL_JITTER_FRACTION)}"
    )
    print()

    verdict = Verdict()
    report_jitter_spread(verdict)
    report_rate_limit_not_provoked(verdict)

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
            "Il costo di un ciclo sull'API vera",
            "nessun dominio disponibile: passare --domain-prefix",
        )
        return verdict.exit_code()

    api.domain_prefix = domain
    print(f"          dominio: {domain}")

    counter = RequestCounter()
    api.session.hooks["response"].append(counter.hook)

    try:
        devices = check_one_request_per_cycle(verdict, api, counter)
        check_page_size_is_honoured(verdict, api)
        check_setup_cost(verdict, api, counter, devices)
    except Exception as err:  # noqa: BLE001
        verdict.fail(
            "Il costo di un ciclo sull'API vera",
            f"{type(err).__name__}: {err}",
        )

    print()
    print("  Nota: quando parte il ciclo successivo lo decide il timer di")
    print("  Home Assistant, non questo script. Che il coordinator gli")
    print("  chieda intervallo + offset, e almeno il backoff dopo un 429,")
    print("  e' verificato in tests/test_coordinator.py.")

    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
