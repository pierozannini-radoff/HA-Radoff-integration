"""
Verifica dal vivo, su dev, della discovery del dominio e del 403.

La migrazione del registry non e' qui: e' deterministica e offline, e la
suite la esercita su una ricostruzione del registry di un'installazione
reale. Il config flow invece dipende interamente da cosa risponde l'API, e
le risposte mockate della suite sono una fotografia. Questa passata
verifica che la forma su cui il flow e' scritto sia ancora quella servita:

1. **la discovery e' chiamabile col solo bearer**, sul path sotto `/data/*`
   e non su quello sotto `/auth/*`. Il tranello: un path inesistente con un
   header `Authorization` risponde `IncompleteSignatureException` - API
   Gateway prova a leggerlo come firma SigV4 - quindi un path sbagliato ha
   la stessa faccia di un problema di autorizzazione, e qui i due casi
   vengono distinti per nome;
2. **la forma della risposta e' annidata**: ogni elemento e'
   `{"domain": {...}, "role": {...}}`, non un oggetto dominio piatto. E' il
   punto su cui `config_flow.py::domain_choices` e' scritto;
3. **l'etichetta leggibile e' `name`, non `prefix`**: su dev quasi tutti i
   prefissi sono frammenti di UUID. Questa passata ricalcola il rapporto,
   perche' se un giorno diventassero leggibili la scelta dell'etichetta
   andrebbe ridiscussa;
4. **il dominio scelto ha dei device**, il ramo opposto dell'abort
   `no_devices`;
5. **un `domain_prefix` a cui l'account non appartiene risponde 403**, e non
   401 ne' 200 con i device di qualcun altro. E' il presupposto del flusso
   Repairs: senza un 403 distinguibile non c'e' niente da riparare.

Come si esegue
--------------
    # credenziali nel proprio .env (gitignored), come RADOFF_USERNAME /
    # RADOFF_PASSWORD - oppure RADOFF_DEV_USERNAME / RADOFF_DEV_PASSWORD
    python3 scripts/verify_m07_live.py --domain-prefix 875fe89b

    # i due override di pool servono finche' const.py punta a un pool
    # diverso dall'ambiente di DEFAULT_BASE_URL
    python3 scripts/verify_m07_live.py \
        --pool-id eu-west-1_XXXXXXXX --client-id XXXXXXXX \
        --domain-prefix XXXXXXXX

Cosa NON verifica, e perche'
----------------------------
- **La migrazione degli unique_id.** Offline e deterministica: vive in
  `tests/test_init.py`, sulla ricostruzione del registry reale. Quello che
  nessuno script puo' fare e' la passata su un backup vero.
- **Il flusso Repairs end to end.** Richiede l'interfaccia di Home
  Assistant. Qui si verifica solo il suo presupposto, il 403 al punto 5;
  il flusso e' in `tests/test_repairs.py` e si guarda a mano con
  `./scripts/develop`.
- **Un dominio senza device.** Non e' provocabile senza crearne uno su dev.
  Il punto 4 dice se il dominio in uso ne ha, il che verifica il ramo
  opposto; l'abort e' in `tests/test_config_flow.py`.

Politica di stampa
------------------
Stessa di `probe_arch2.py` e degli altri `verify_*_live.py`: i prefissi di
dominio e i serial si stampano, le etichette scritte da persone no - `name`,
`room_name`, `building_name` non compaiono mai, ne' le coordinate, ne'
l'email, ne' alcun token. Del `name` dei domini si stampa solo *se* c'e' e
se e' diverso dal prefisso, che e' tutto cio' che questa verifica deve
sapere.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_components.radoff.api import (  # noqa: E402
    API,
    APIConnectionError,
    APIDomainAccessError,
    AuthExpiredError,
    AuthInvalidError,
)
from custom_components.radoff.config_flow import domain_choices  # noqa: E402
from custom_components.radoff.const import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_CLIENT_ID,
    DEFAULT_POOL_ID,
    DEFAULT_POOL_REGION,
)

# Un prefisso "non leggibile": otto caratteri esadecimali, cioe' il primo
# blocco di un UUID. Non e' una regola
# del backend, e' cio' che dev serve di fatto - ed e' esattamente il motivo
# per cui l'etichetta della scelta e' `name`.
UUID_FRAGMENT = re.compile(r"^[0-9a-f]{8}$")

# Un prefisso che non esiste, per provocare il 403 del punto 5. Deve essere
# sintatticamente plausibile e non appartenere a nessuno: se per assurdo
# esistesse, il controllo lo direbbe invece di passare per sbaglio.
FOREIGN_DOMAIN_PREFIX = "zzzzzzzz"

POOL_LABELS = {
    "eu-west-1_5SsvW9t6S": "dev",
    "eu-west-1_zD4CSIZ6i": "prod (il default di const.py)",
}


def auth_hint(err: Exception, pool_id: str) -> str:
    """
    Spiegare un fallimento di autenticazione invece di riportarlo e basta.

    Identica a quella di `verify_m06_live.py`, e per la stessa ragione: i
    due modi in cui questa passata puo' non partire si assomigliano nel log
    e portano a conclusioni opposte (vedi
    `memory/srp-non-abilitato-pool-dev-blocca-e2e`).
    """
    label = POOL_LABELS.get(pool_id, "sconosciuto")
    if isinstance(err, AuthInvalidError):
        return (
            f"Cognito ha rifiutato le credenziali sul pool {pool_id} "
            f"({label}).\n"
            "Le utenze dei due pool sono separate: credenziali valide su un "
            "ambiente non lo sono sull'altro."
        )
    if isinstance(err, AuthExpiredError):
        return (
            f"L'handshake sul pool {pool_id} ({label}) e' riuscito, ma l'API "
            "ha risposto 401.\n"
            "E' il token a essere del pool sbagliato per questo host: l'API "
            "di dev accetta solo il pool dev (accepted_pool)."
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


def check_discovery_answers_on_the_data_path(
    verdict: Verdict, api: API, pool_id: str
) -> list[dict[str, Any]] | None:
    """
    Punto 1: la discovery risponde, col solo bearer, sul path sotto `/data`.

    Il fallimento va letto con attenzione, ed e' per questo che il dettaglio
    nomina il tranello: su una route inesistente API Gateway legge
    l'`Authorization` come firma SigV4 e risponde
    `IncompleteSignatureException`, che sembra un problema di credenziali e
    invece e' un 404 travestito.
    """
    try:
        domains = api.list_domains()
    except (AuthInvalidError, AuthExpiredError) as err:
        verdict.fail(
            "Discovery col solo bearer su /data/user/me/domains",
            f"{type(err).__name__}: {err}\n{auth_hint(err, pool_id)}",
        )
        return None
    except APIConnectionError as err:
        verdict.fail(
            "Discovery col solo bearer su /data/user/me/domains",
            f"{err}\n"
            "Attenzione: su una route inesistente API Gateway prova a leggere "
            "l'header Authorization come firma SigV4 e risponde "
            "IncompleteSignatureException, che si legge come un problema di "
            "autorizzazione e non lo e'. Verificare DISCOVERY_PATH.",
        )
        return None

    verdict.ok(
        "Discovery col solo bearer su /data/user/me/domains",
        f"{len(domains)} domini, nessun header di dominio inviato",
    )
    return domains


def check_the_response_is_nested(
    verdict: Verdict, domains: list[dict[str, Any]]
) -> None:
    """
    Punto 2: ogni elemento annida il dominio sotto `domain`, con un `prefix`.

    E' la forma su cui `domain_choices` e' scritto. Se il backend tornasse a
    un oggetto piatto, il config flow non troverebbe nessun prefisso e
    l'utente vedrebbe `no_domains` su un account che ha dei domini - un
    fallimento silenzioso, che e' il genere che questo controllo esiste per
    anticipare.
    """
    if not domains:
        verdict.skip(
            "Forma annidata della risposta",
            "l'account non ha nessun dominio su questo host",
        )
        return

    flat = [
        element
        for element in domains
        if not isinstance(element.get("domain"), dict)
        or not element["domain"].get("prefix")
    ]
    verdict.check(
        not flat,
        "Forma annidata della risposta: {'domain': {'prefix', 'name'}, 'role'}",
        f"{len(domains) - len(flat)}/{len(domains)} elementi con domain.prefix",
    )


def check_the_label_is_worth_showing(
    verdict: Verdict, domains: list[dict[str, Any]]
) -> None:
    """
    Punto 3: `name` e' leggibile dove `prefix` non lo e'.

    Ricalcola quanti prefissi sono frammenti di UUID, perche' e' l'unica
    giustificazione dell'etichetta.
    Non e' un FAIL se i prefissi diventassero leggibili: sarebbe una buona
    notizia e una decisione da ridiscutere, quindi viene detta e basta.
    """
    choices = domain_choices(domains)
    if not choices:
        verdict.skip(
            "Etichetta leggibile della scelta", "nessun dominio da etichettare"
        )
        return

    opaque = [prefix for prefix in choices if UUID_FRAGMENT.match(prefix)]
    named = [prefix for prefix, label in choices.items() if label != prefix]

    verdict.check(
        len(named) == len(choices),
        "Ogni dominio porta un name diverso dal prefisso",
        f"{len(named)}/{len(choices)} con etichetta propria; "
        f"{len(opaque)}/{len(choices)} prefissi sono frammenti di UUID",
    )


def check_the_chosen_domain_has_devices(
    verdict: Verdict, api: API, domain_prefix: str
) -> None:
    """
    Punto 4: il dominio scelto ha almeno un device.

    Il config flow chiude il setup con l'abort `no_devices` quando la
    risposta e' vuota. Qui si verifica che su un dominio popolato la
    chiamata risponda e conti qualcosa: e' la stessa chiamata, con l'esito
    opposto.
    """
    api.domain_prefix = domain_prefix
    try:
        devices = api.get_devices()
    except Exception as err:  # noqa: BLE001
        verdict.fail(
            "Il dominio scelto ha dei device",
            f"{type(err).__name__}: {err}",
        )
        return

    verdict.check(
        bool(devices),
        "Il dominio scelto ha dei device",
        f"{len(devices)} device sul dominio {domain_prefix}",
    )


def check_a_foreign_domain_is_a_403(verdict: Verdict, api: API) -> None:
    """
    Punto 5: un dominio altrui risponde 403, distinguibile da un 401.

    E' il presupposto del flusso Repairs interattivo: il client traduce il
    403 in `APIDomainAccessError`, il coordinator lo traduce in un
    `ConfigEntryError` piu' una issue riparabile, e il flusso ri-seleziona
    il dominio. Se l'API rispondesse 401 sarebbe indistinguibile da una
    sessione scaduta e l'utente finirebbe a reinserire una password che non
    e' il problema; se rispondesse 200 sarebbe molto peggio.
    """
    original = api.domain_prefix
    api.domain_prefix = FOREIGN_DOMAIN_PREFIX
    try:
        devices = api.get_devices()
    except APIDomainAccessError:
        verdict.ok(
            "Un domain_prefix altrui risponde 403 (non 401, non 200)",
            f"prefisso provato: {FOREIGN_DOMAIN_PREFIX}",
        )
        return
    except Exception as err:  # noqa: BLE001
        verdict.fail(
            "Un domain_prefix altrui risponde 403 (non 401, non 200)",
            f"ha risposto {type(err).__name__}: {err}\n"
            "Un 401 qui e' indistinguibile da una sessione scaduta e manda "
            "l'utente al re-auth invece che alla ri-selezione del dominio.",
        )
        return
    finally:
        api.domain_prefix = original

    verdict.fail(
        "Un domain_prefix altrui risponde 403 (non 401, non 200)",
        f"ha risposto 200 con {len(devices)} device: la query non e' "
        "filtrata dal domain_prefix.",
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
    print("  Verifica dal vivo della discovery del dominio e del 403")
    print(f"  host      : {args.base_url}")
    print(f"  pool      : {args.pool_id} / client {args.client_id} / {region}")
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

    domains = check_discovery_answers_on_the_data_path(verdict, api, args.pool_id)
    if domains is None:
        return verdict.exit_code()

    check_the_response_is_nested(verdict, domains)
    check_the_label_is_worth_showing(verdict, domains)

    choices = domain_choices(domains)
    domain_prefix = args.domain_prefix or next(iter(choices), "")
    if not domain_prefix:
        verdict.skip(
            "I controlli sul dominio scelto",
            "nessun dominio disponibile: passare --domain-prefix",
        )
        return verdict.exit_code()

    print()
    print(f"          dominio: {domain_prefix}")
    print()

    check_the_chosen_domain_has_devices(verdict, api, domain_prefix)
    check_a_foreign_domain_is_a_403(verdict, api)

    print()
    print("  Nota: la migrazione del registry non e' qui e non puo' esserlo -")
    print("  e' offline. Vive in tests/test_init.py, sulla ricostruzione di")
    print("  un registry reale; la passata su un backup vero si fa a mano.")

    return verdict.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
