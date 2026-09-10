#!/usr/bin/env python3
"""
M-01 - Ricognizione dell'API arch 2.0 su dev, con salvataggio di fixture reali.

One-shot, eseguito a mano contro un account reale su
`https://v2.api.dev.iot.radoff.life`. NON fa parte dell'integrazione: vive
fuori da `custom_components/`, che non lo importa e non deve mai importarlo.
La dipendenza va nella direzione opposta - questo script carica
`custom_components/radoff/api/auth.py` per riusare l'handshake SRP del
client, cosi' il token che verifichiamo (D-24) e' esattamente quello che
l'integrazione usera'.

Cosa fa, in sequenza:

1. login SRP sul pool Cognito **attuale** (quello di `const.py`, usato da
   arch 1.x) e, se configurato, su un **pool dev** dedicato;
2. la stessa `GET /data/user/me/domains` sull'host v2 con entrambi i token,
   per stabilire quale dei due l'API 2.0 accetta (D-24). Il path e' sotto
   il base path `data`, non `auth` come diceva la risposta a T-02 D-03:
   `/auth/user/me/domains` su arch 2.0 non esiste;
3. `GET /analytics/measures-ranges` per ognuno dei sei `device_type` del
   catalogo e una volta senza `device_type` (che unisce tutti i tipi, ed e'
   da li' che esce l'enumerazione completa delle `unit`);
4. `GET /data/devices` paginata con `page_size` piccolo per forzare almeno
   due pagine, ripetuta per misurare la stabilita' dell'ordinamento, piu'
   una passata con `page_size=200` come fa il client;
5. `GET /data/devices/{serial}` su un device della lista;
6. errori provocati deliberatamente: `domain_prefix` di un dominio non
   proprio (403 atteso), serial inesistente, `device_type` ignoto su
   `measures-ranges` (404 con `available` atteso), `/data/devices` senza
   `domain_prefix` (D-29);
7. sonde su `sort`/`order_by` per stabilire se filtri e ordinamenti
   esistono (D-19).

Di ogni chiamata registra **anche gli header di risposta**, non solo il
body: e' da li' che esce il nome effettivo dell'header di request id (D-30).

Redazione: nessun body grezzo tocca il disco. Ogni response passa per
`redact()` prima di essere scritta. Politica allineata a
`custom_components/radoff/diagnostics.py` (card S-17), con due differenze
deliberate, documentate nel README che questo script genera:

- i **serial restano in chiaro** (la card lo dice esplicitamente, e in arch
  2.0 `deviceId` == `serial_number` == `deviceSerial`, quindi redigerli
  svuoterebbe le fixture);
- la redazione e' **basata sul valore**, non solo sulla chiave: qualunque
  UUID, email o JWT viene sostituito con un segnaposto stabile
  (`<uuid-1>`, `<email-1>`, ...), cosi' i riferimenti incrociati fra
  fixture restano verificabili senza che nulla di identificante finisca nel
  repo. Le chiavi note di coordinate/indirizzo sono trattate a parte: le
  coordinate numeriche vengono arrotondate a 1 decimale (~11 km), il resto
  sostituito.

Uso:

    export RADOFF_DEV_USERNAME='...'
    export RADOFF_DEV_PASSWORD='...'
    # facoltativi, per il confronto fra pool di D-24:
    export RADOFF_DEV_POOL_ID='eu-west-1_XXXXXXXX'
    export RADOFF_DEV_CLIENT_ID='...'
    python3 scripts/probe_arch2.py

    # oppure, senza esportare nulla:
    python3 scripts/probe_arch2.py --env-file .env.dev

    # diagnostica, quando l'app client non ha SRP abilitato: chiede la
    # password senza eco e senza lasciarla nella history della shell
    python3 scripts/probe_arch2.py --auth-flow password --ask-password \
        --username 'utente@dev' --domain-prefix '<prefisso>'

Nessuna credenziale e' scritta nel sorgente, e nessuna finisce nell'output:
lo script e' ripetibile da chiunque con le proprie.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_HOST = "https://v2.api.dev.iot.radoff.life"
DEFAULT_OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "dev"

# D-13: enumerazione confermata dal backend.
DEVICE_TYPES = ("sense", "now", "nowplus", "city", "life", "sismoff")

# Valori deliberatamente inesistenti per le sonde d'errore (D-29).
UNKNOWN_DEVICE_TYPE = "not-a-real-device-type"
UNKNOWN_SERIAL = "RADOFF-NOT-A-REAL-SERIAL-0000"
DEFAULT_FOREIGN_DOMAIN_PREFIX = "radoff-hq"

# Il primo e' quello indicato dalla risposta a T-02 D-03. Gli altri servono a
# distinguere "endpoint autorizzato in modo diverso" da "path sbagliato": API
# Gateway, davanti a una route inesistente CON un header `Authorization`,
# prova a leggerlo come firma SigV4 e risponde `IncompleteSignatureException`
# invece di dire che la route non c'e'. I due casi hanno la stessa faccia.
DISCOVERY_PATH = "/data/user/me/domains"

# Varianti storiche, sondate solo se quella buona fallisce. `/auth/...` e' il
# path che la risposta a T-02 D-03 indicava: su arch 2.0 non esiste, la
# discovery e' passata sotto il base path `data` (swagger
# `yama-be-core-platform`, operationId `getUserDomains`).
DISCOVERY_PATH_CANDIDATES = (
    DISCOVERY_PATH,
    "/auth/user/me/domains",
    "/user/me/domains",
    "/auth/users/me/domains",
)

HTTP_OK = 200
REQUEST_TIMEOUT_SECONDS = 30
# D-21: 50 rps per stage, condivisi con app mobile e web. Una pausa fra le
# chiamate tiene la ricognizione a due ordini di grandezza dal tetto.
PAUSE_BETWEEN_CALLS_SECONDS = 0.25

# Misure che le verifiche di T-08 6 vogliono osservare nei payload reali.
MEASURES_OF_INTEREST = (
    "radon",
    "radon_status",
    "radonstatus",
    "pressure",
    "internal_temperature",
    "internaltemperature",
    "temperature",
    "relative_humidity",
    "pm1",
    "pm25",
    "pm10",
    "tvoc",
    "eco2",
    "aqi_value",
    "airqualityindex",
)


# --------------------------------------------------------------------------
# Caricamento dei moduli dell'integrazione, senza importarne il package
# --------------------------------------------------------------------------
def _load_integration_module(name: str, relative_path: str) -> Any:
    """
    Carica un singolo file dell'integrazione come modulo isolato.

    Non usiamo `import custom_components.radoff...`: importare il package
    eseguirebbe il suo `__init__.py`, che tira dentro Home Assistant. Sia
    `const.py` che `api/auth.py` non hanno import relativi (solo stdlib,
    boto3 e pycognito), quindi caricarli per path li rende usabili qui con
    le sole dipendenze di `requirements.txt`.
    """
    path = REPO_ROOT / relative_path
    spec = importlib_util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - path fisso
        msg = f"Impossibile caricare {relative_path}"
        raise RuntimeError(msg)
    module = importlib_util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_const = _load_integration_module("_radoff_const", "custom_components/radoff/const.py")
_auth = _load_integration_module("_radoff_auth", "custom_components/radoff/api/auth.py")

CognitoSession = _auth.CognitoSession
USER_AGENT = _const.USER_AGENT
CURRENT_POOL_ID = _const.DEFAULT_POOL_ID
CURRENT_CLIENT_ID = _const.DEFAULT_CLIENT_ID
CURRENT_POOL_REGION = _const.DEFAULT_POOL_REGION


class PasswordSession:
    """
    Login via `USER_PASSWORD_AUTH`, come diagnostica - mai come flusso normale.

    Espone la stessa superficie minima di `CognitoSession` (`.tokens` e
    `get_bearer()`) cosi' che il resto dello script non debba sapere quale
    delle due sta usando.

    **Questo NON e' il flusso dell'integrazione, e non deve diventarlo.**
    `USER_PASSWORD_AUTH` manda la password a Cognito in chiaro dentro la
    richiesta (protetta dal TLS, ma il server la riceve in chiaro), mentre
    SRP dimostra di conoscerla senza trasmetterla. L'integrazione usa e
    continuera' a usare SRP: questa strada esiste solo perche' l'app client
    di dev ha SRP disabilitato, e serve a stabilire se l'utente esiste in
    quel pool e a ottenere un token con cui proseguire la ricognizione
    mentre si aspetta che SRP venga abilitato. Va usata solo con un account
    di prova su dev, mai con credenziali di produzione.
    """

    def __init__(
        self,
        username: str,
        password: str,
        client_id: str,
        pool_id: str,  # noqa: ARG002 - accettato per simmetria con CognitoSession
        pool_region: str,
    ) -> None:
        """Memorizza l'identita' Cognito. Nessuna chiamata di rete qui."""
        self.username = username
        self.password = password
        self.client_id = client_id
        self.pool_region = pool_region
        self.tokens: dict[str, Any] = {}

    def get_bearer(self) -> str:
        """Esegue il login e restituisce l'IdToken."""
        import boto3  # noqa: PLC0415 - usato solo da questa classe

        client = boto3.client("cognito-idp", region_name=self.pool_region)
        response = client.initiate_auth(
            ClientId=self.client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": self.username, "PASSWORD": self.password},
        )
        result = response.get("AuthenticationResult")
        if not result:
            challenge = response.get("ChallengeName", "SCONOSCIUTA")
            msg = (
                f"Cognito ha risposto con una challenge ({challenge}) invece di "
                "un login completo: va conclusa nell'app prima di riprovare."
            )
            raise RuntimeError(msg)
        self.tokens = result
        return result["IdToken"]


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------
@dataclass
class PoolConfig:
    """Un pool Cognito su cui provare il login."""

    label: str
    pool_id: str
    client_id: str
    region: str


@dataclass
class Config:
    """Tutto quello che lo script prende dall'ambiente, mai dal sorgente."""

    username: str
    password: str
    host: str
    pools: list[PoolConfig]
    foreign_domain_prefix: str
    small_page_size: int
    full_page_size: int
    out_dir: Path
    keep_domain_prefix: bool
    forced_domain_prefix: str | None
    auth_flow: str


def _read_env_file(path: Path) -> None:
    """Carica `KEY=value` da un file nell'ambiente, senza sovrascrivere."""
    if not path.is_file():
        msg = f"--env-file: {path} non esiste"
        raise SystemExit(msg)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _derive_region(pool_id: str, explicit: str | None) -> str:
    """La region e' il prefisso del pool id (`eu-west-1_XXXX`)."""
    if explicit:
        return explicit
    region, _, _ = pool_id.partition("_")
    return region


def build_config(args: argparse.Namespace) -> Config:
    """Compone la configurazione da CLI + ambiente, e spiega cosa manca."""
    if args.env_file:
        _read_env_file(Path(args.env_file))

    username = args.username or os.environ.get("RADOFF_DEV_USERNAME", "")
    password = os.environ.get("RADOFF_DEV_PASSWORD", "")
    if args.ask_password:
        # `getpass` non fa eco e non lascia la password nella history della
        # shell ne' nella tabella dei processi, a differenza di una env var
        # scritta sulla riga di comando.
        password = getpass.getpass(f"Password per {username or '<utente>'}: ")
    missing = [
        name
        for name, value in (
            ("RADOFF_DEV_USERNAME", username),
            ("RADOFF_DEV_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        msg = (
            "Mancano le variabili d'ambiente: "
            + ", ".join(missing)
            + ".\nEsportale, oppure passa --env-file <file>. Vedi il docstring "
            "di questo script.\nNessuna credenziale e' scritta nel sorgente: "
            "ognuno usa le proprie."
        )
        raise SystemExit(msg)

    pools = [
        PoolConfig(
            label="pool_current",
            pool_id=CURRENT_POOL_ID,
            client_id=CURRENT_CLIENT_ID,
            region=CURRENT_POOL_REGION,
        )
    ]
    dev_pool_id = os.environ.get("RADOFF_DEV_POOL_ID", "")
    dev_client_id = os.environ.get("RADOFF_DEV_CLIENT_ID", "")
    if dev_pool_id and dev_client_id:
        pools.append(
            PoolConfig(
                label="pool_dev",
                pool_id=dev_pool_id,
                client_id=dev_client_id,
                region=_derive_region(dev_pool_id, os.environ.get("RADOFF_DEV_REGION")),
            )
        )

    return Config(
        username=username,
        password=password,
        host=(args.host or os.environ.get("RADOFF_DEV_HOST") or DEFAULT_HOST).rstrip(
            "/"
        ),
        pools=pools,
        foreign_domain_prefix=os.environ.get(
            "RADOFF_FOREIGN_DOMAIN_PREFIX", DEFAULT_FOREIGN_DOMAIN_PREFIX
        ),
        small_page_size=args.small_page_size,
        full_page_size=args.full_page_size,
        out_dir=Path(args.out).resolve(),
        keep_domain_prefix=not args.redact_domain_prefix,
        forced_domain_prefix=args.domain_prefix,
        auth_flow=args.auth_flow,
    )


# --------------------------------------------------------------------------
# Redazione
# --------------------------------------------------------------------------
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_JWT_RE = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b")

# Chiavi il cui valore non ha nessun interesse diagnostico e va via intero.
# Allineate a `diagnostics.TO_REDACT`, meno `serial`/`device_id`: in arch 2.0
# quei due SONO il serial, che la card lascia esplicitamente in chiaro.
_SECRET_KEYS = frozenset(
    {
        "password",
        "pwd",
        "secret",
        "clientsecret",
        "token",
        "idtoken",
        "accesstoken",
        "refreshtoken",
        "sessiontoken",
        "authorization",
        "apikey",
        "xapikey",
        "cookie",
        "setcookie",
    }
)

# Chiavi di posizione. `pos` di `measures-ranges` (indice di ordinamento) non
# e' qui e non deve entrarci: e' un intero d'ordinamento, non una coordinata.
_COORD_KEYS = frozenset(
    {
        "lat",
        "latitude",
        "lng",
        "lon",
        "long",
        "longitude",
        "alt",
        "altitude",
        "elevation",
        "coord",
        "coords",
        "coordinate",
        "coordinates",
        "geo",
        "geolocation",
        "geopoint",
        "location",
        "position",
        "address",
        "street",
        "housenumber",
        "zip",
        "zipcode",
        "postalcode",
        "postcode",
    }
)

# Il `domain_prefix` e' human-readable (`radoff-hq`) e il client lo mostra
# all'utente nel menu di scelta del dominio: di default resta in chiaro,
# perche' e' esattamente il percorso che le fixture devono testare.
# `--redact-domain-prefix` lo pseudonimizza per chi non vuole il nome del
# proprio dominio nel repo.
_DOMAIN_PREFIX_KEYS = frozenset({"prefix", "domainprefix"})

# Etichette libere scritte da un umano. Su dev il campo `name` di un dominio
# contiene nomi e cognomi di persone reali ("Mario Sessa", "Matteo Chiesa"),
# e quello di un device il nome che gli ha dato il proprietario: sono
# esattamente i "riferimenti sensibili di persone" che la card vieta di
# mettere nel repo. Vengono sostituiti con un segnaposto stabile, cosi' la
# fixture conserva il campo - e i test possono esercitare il percorso che lo
# mostra all'utente - senza portarsi dietro il contenuto.
#
# `name` NON puo' essere redatto per nome di chiave e basta: compare anche in
# `role.name` ("Super Admin") e nelle etichette di `measures-ranges` ("PM10"),
# dove non ha niente di personale e serve intatto. Vale solo quando l'oggetto
# che lo contiene e' un dominio o un device - vedi `_is_human_label`.
_HUMAN_LABEL_KEYS = frozenset({"name", "roomname", "buildingname"})

# Le chiavi che identificano l'oggetto contenitore come dominio o device.
_LABELLED_OBJECT_MARKERS = frozenset(
    {"prefix", "serialnumber", "serial", "deviceid", "domainprefix"}
)

REDACTED = "**REDACTED**"
# Le coordinate non spariscono: vengono arrotondate. Un decimale e' ~11 km -
# inutilizzabile per localizzare qualcuno, sufficiente perche' la fixture
# conservi tipo, segno e ordine di grandezza del campo.
_COORD_DECIMALS = 1


def _normalize_key(key: str) -> str:
    """`domain_id`, `domainId`, `Domain-ID` -> `domainid`."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


class Redactor:
    """
    Sostituisce identificatori e segreti con segnaposto stabili.

    Stabili significa che lo stesso valore riceve lo stesso segnaposto in
    tutte le fixture della stessa esecuzione: `<uuid-1>` e' sempre lo stesso
    dominio, quindi i riferimenti incrociati fra `/auth/user/me/domains` e
    `/data/devices` restano verificabili senza che l'UUID vero esista da
    nessuna parte nel repo.
    """

    def __init__(
        self,
        extra_literals: tuple[str, ...] = (),
        *,
        redact_domain_prefix: bool = False,
    ) -> None:
        self._placeholders: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._redact_domain_prefix = redact_domain_prefix
        # Valori noti da cancellare ovunque compaiano, anche dentro un
        # messaggio d'errore: le credenziali di chi esegue lo script.
        self._extra_literals = tuple(literal for literal in extra_literals if literal)

    def _placeholder(self, kind: str, value: str) -> str:
        known = self._placeholders.get(value)
        if known is not None:
            return known
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = f"<{kind}-{self._counters[kind]}>"
        self._placeholders[value] = token
        return token

    def scrub_text(self, text: str) -> str:
        """Applica le sostituzioni basate sul valore a una stringa."""
        for literal in self._extra_literals:
            if literal in text:
                text = text.replace(literal, REDACTED)
        text = _JWT_RE.sub("<jwt>", text)
        text = _EMAIL_RE.sub(lambda m: self._placeholder("email", m.group(0)), text)
        return _UUID_RE.sub(lambda m: self._placeholder("uuid", m.group(0)), text)

    def _coord(self, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), _COORD_DECIMALS)
        if isinstance(value, (dict, list)):
            return self.redact(value)
        return REDACTED

    def redact(self, obj: Any) -> Any:
        """Restituisce una copia di `obj` priva di dati identificanti."""
        if isinstance(obj, dict):
            out: dict[str, Any] = {}
            labelled = bool(
                {_normalize_key(str(k)) for k in obj} & _LABELLED_OBJECT_MARKERS
            )
            for key, value in obj.items():
                normalized = _normalize_key(str(key))
                if labelled and normalized in _HUMAN_LABEL_KEYS:
                    out[key] = (
                        self._placeholder("label", value)
                        if isinstance(value, str) and value
                        else value
                    )
                elif normalized in _SECRET_KEYS:
                    out[key] = REDACTED
                elif (
                    self._redact_domain_prefix
                    and normalized in _DOMAIN_PREFIX_KEYS
                    and isinstance(value, str)
                ):
                    out[key] = self._placeholder("domain-prefix", value)
                elif normalized in _COORD_KEYS:
                    out[key] = self._coord(value)
                else:
                    out[key] = self.redact(value)
            return out
        if isinstance(obj, list):
            return [self.redact(item) for item in obj]
        if isinstance(obj, str):
            return self.scrub_text(obj)
        return obj

    @property
    def placeholder_count(self) -> int:
        """Quanti valori distinti sono stati sostituiti."""
        return len(self._placeholders)


# --------------------------------------------------------------------------
# Registrazione delle chiamate
# --------------------------------------------------------------------------
@dataclass
class Call:
    """Una chiamata eseguita, con tutto quello che serve rileggerla dopo."""

    name: str
    step: str
    method: str
    url: str
    params: dict[str, Any]
    auth_style: str
    status: int | None
    elapsed_ms: int | None
    response_headers: dict[str, str]
    body_file: str | None
    body: Any = field(default=None, repr=False)
    # Solo in memoria: serve per proseguire la ricognizione (il
    # `domain_prefix` reale). Non finisce nel manifest ne' su disco.
    raw_body: Any = field(default=None, repr=False)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Vero se la chiamata ha risposto 2xx."""
        return self.status is not None and 200 <= self.status < 300

    def to_manifest_entry(self) -> dict[str, Any]:
        """La riga di questa chiamata nel manifest (senza il body)."""
        return {
            "name": self.name,
            "step": self.step,
            "method": self.method,
            "url": self.url,
            "params": self.params,
            "auth_style": self.auth_style,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "response_headers": self.response_headers,
            "body_file": self.body_file,
            "error": self.error,
        }


class Recorder:
    """Esegue le richieste, le reda e le scrive su disco."""

    def __init__(self, config: Config, redactor: Redactor) -> None:
        self.config = config
        self.redactor = redactor
        self.calls: list[Call] = []
        self.session = requests.Session()
        self.session.headers.update({"user-agent": USER_AGENT})
        self.config.out_dir.mkdir(parents=True, exist_ok=True)

    def get(
        self,
        name: str,
        step: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        save: bool = True,
        auth_style: str = "bearer",
    ) -> Call:
        """Esegue una GET. Vedi `_request` per il significato di `auth_style`."""
        return self._request(
            name,
            step,
            "GET",
            path,
            token=token,
            params=params,
            save=save,
            auth_style=auth_style,
        )

    def options(self, name: str, step: str, path: str) -> Call:
        """
        Esegue una OPTIONS senza autenticazione.

        Il preflight CORS e' quasi sempre un'integrazione MOCK senza
        autorizzazione: se risponde, la risorsa **esiste**. E' l'unico modo
        di distinguere "route inesistente" da "route dietro autorizzazione
        IAM" senza avere un token valido - da fuori le due danno la stessa
        risposta a una GET.
        """
        return self._request(name, step, "OPTIONS", path, token=None, save=False)

    def _request(
        self,
        name: str,
        step: str,
        method: str,
        path: str,
        *,
        token: str | None,
        params: dict[str, Any] | None = None,
        save: bool = True,
        auth_style: str = "bearer",
    ) -> Call:
        """
        Esegue una GET, la registra e restituisce l'esito.

        `auth_style` decide come viaggia il token: `bearer` e' la forma che
        usa il client 1.x (`Authorization: Bearer <IdToken>`), `raw` manda
        il JWT nudo. La distinzione non e' accademica - un authorizer
        Cognito accetta entrambe, un metodo con autorizzazione IAM nessuna
        delle due, e sapere quale forma passa e' parte di cosa questa
        ricognizione deve scoprire.
        """
        url = f"{self.config.host}{path}"
        params = params or {}
        if not token:
            headers: dict[str, str] = {}
        elif auth_style == "raw":
            headers = {"authorization": token}
        else:
            headers = {"authorization": f"Bearer {token}"}

        started = time.monotonic()
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as err:
            call = Call(
                name=name,
                step=step,
                method=method,
                url=url,
                params=params,
                auth_style=auth_style if token else "none",
                status=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                response_headers={},
                body_file=None,
                error=self.redactor.scrub_text(f"{type(err).__name__}: {err}"),
            )
            self.calls.append(call)
            self._log(call)
            return call

        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            raw_body: Any = response.json()
        except ValueError:
            raw_body = {"__non_json_body__": response.text[:2000]}

        body = self.redactor.redact(raw_body)
        # Gli header di risposta servono per intero (D-30): l'unico da
        # togliere e' `set-cookie`, che qui non porta informazione.
        response_headers = {
            key.lower(): (REDACTED if _normalize_key(key) in _SECRET_KEYS else value)
            for key, value in response.headers.items()
        }

        body_file = None
        if save:
            body_file = f"{name}.json"
            (self.config.out_dir / body_file).write_text(
                json.dumps(body, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
                encoding="utf-8",
            )

        call = Call(
            name=name,
            step=step,
            method=method,
            url=url,
            params=params,
            auth_style=auth_style if token else "none",
            status=response.status_code,
            elapsed_ms=elapsed_ms,
            response_headers=response_headers,
            body_file=body_file,
            body=body,
            raw_body=raw_body,
        )
        self.calls.append(call)
        self._log(call)
        time.sleep(PAUSE_BETWEEN_CALLS_SECONDS)
        return call

    @staticmethod
    def _log(call: Call) -> None:
        status = call.status if call.status is not None else "ERR"
        detail = f" {call.error}" if call.error else ""
        query = ", ".join(f"{k}={v}" for k, v in call.params.items())
        verb = "" if call.method == "GET" else f"{call.method} "
        print(f"  [{status}] {verb}{call.name:<40} {query}{detail}")

    def write_manifest(self, extra: dict[str, Any]) -> None:
        """Scrive `_manifest.json` con una riga per chiamata."""
        manifest = {
            "generated_at": datetime.now(UTC).isoformat(),
            "host": self.config.host,
            "user_agent": USER_AGENT,
            "note": (
                "Fixture reali raccolte da scripts/probe_arch2.py (card M-01). "
                "Ogni body e' redatto: vedi README.md in questa cartella."
            ),
            **extra,
            "calls": [call.to_manifest_entry() for call in self.calls],
        }
        (self.config.out_dir / "_manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


# --------------------------------------------------------------------------
# Lettura tollerante dei payload
#
# La forma esatta delle response 2.0 e' cio' che questa card sta scoprendo:
# questi helper non assumono una struttura, la cercano. Ogni assunzione
# sbagliata qui produrrebbe un "non osservato" invece di un dato, non un
# crash a meta' ricognizione.
# --------------------------------------------------------------------------
_LIST_KEYS = ("devices", "items", "results", "data", "content", "domains")
_SERIAL_KEYS = (
    "serial_number",
    "serialnumber",
    "deviceserial",
    "deviceid",
    "device_id",
    "serial",
    "id",
)


def find_list_of_objects(body: Any) -> list[dict[str, Any]]:
    """Restituisce la prima lista di oggetti trovata nel payload."""
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if not isinstance(body, dict):
        return []
    for key in _LIST_KEYS:
        value = body.get(key)
        if isinstance(value, list) and any(isinstance(i, dict) for i in value):
            return [item for item in value if isinstance(item, dict)]
    for value in body.values():
        if isinstance(value, (dict, list)):
            found = find_list_of_objects(value)
            if found:
                return found
    return []


def serial_of(item: dict[str, Any]) -> str | None:
    """Estrae il serial da un oggetto device, qualunque nome abbia la chiave."""
    normalized = {_normalize_key(k): v for k, v in item.items()}
    for key in _SERIAL_KEYS:
        value = normalized.get(_normalize_key(key))
        if isinstance(value, str) and value:
            return value
    return None


def serials_of(body: Any) -> list[str]:
    """La sequenza dei serial di una pagina, nell'ordine in cui arrivano."""
    return [s for s in (serial_of(item) for item in find_list_of_objects(body)) if s]


def walk_measures(obj: Any) -> dict[str, list[Any]]:
    """
    Raccoglie i valori osservati per ogni nome di misura nel payload.

    Copre le due forme che conosciamo - `{"propertyName": x, "value": y}`
    dell'arch 1.x e una mappa `nome -> valore` inline in `telemetry` - piu'
    il caso generale di una chiave scalare il cui nome e' una misura nota.
    """
    found: dict[str, list[Any]] = {}

    def record(name: Any, value: Any) -> None:
        if not isinstance(name, str):
            return
        if _normalize_key(name) not in {
            _normalize_key(m) for m in MEASURES_OF_INTEREST
        }:
            return
        found.setdefault(name, []).append(value)

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            normalized = {_normalize_key(k): k for k in node}
            if "propertyname" in normalized:
                name = node[normalized["propertyname"]]
                value = node.get("value", node.get("aggregationValue"))
                record(name, value)
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit(value)
                else:
                    record(key, value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(obj)
    return found


def collect_units(body: Any) -> dict[str, list[str]]:
    """Mappa `unit` -> misure che la dichiarano, da un payload measures-ranges."""
    units: dict[str, list[str]] = {}

    def visit(node: Any, label: str | None) -> None:
        if isinstance(node, dict):
            own_label = None
            for key, value in node.items():
                if _normalize_key(key) in {"acronym", "label", "name", "measure"} and (
                    isinstance(value, str)
                ):
                    own_label = own_label or value
            unit = None
            for key, value in node.items():
                if _normalize_key(key) == "unit" and isinstance(value, str):
                    unit = value
            if unit is not None:
                units.setdefault(unit, [])
                name = own_label or label or "?"
                if name not in units[unit]:
                    units[unit].append(name)
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    visit(value, own_label or (key if isinstance(key, str) else label))
        elif isinstance(node, list):
            for item in node:
                visit(item, label)

    visit(body, None)
    return units


def find_measure_entry(body: Any, measure: str) -> dict[str, Any] | None:
    """Trova il blocco di `measures-ranges` che descrive una data misura."""
    target = _normalize_key(measure)

    def visit(node: Any, key_hint: str | None) -> dict[str, Any] | None:
        if isinstance(node, dict):
            names = {
                _normalize_key(str(v))
                for k, v in node.items()
                if _normalize_key(k) in {"acronym", "label", "name", "measure"}
                and isinstance(v, str)
            }
            if key_hint is not None:
                names.add(_normalize_key(key_hint))
            if target in names and any(
                _normalize_key(k) in {"ranges", "unit", "datatype"} for k in node
            ):
                return node
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    hit = visit(value, key if isinstance(key, str) else None)
                    if hit is not None:
                        return hit
        elif isinstance(node, list):
            for item in node:
                hit = visit(item, key_hint)
                if hit is not None:
                    return hit
        return None

    return visit(body, None)


# --------------------------------------------------------------------------
# Passi della ricognizione
# --------------------------------------------------------------------------
_COGNITO_CODE_HINTS = {
    "UserNotFoundException": (
        "l'utente NON esiste in questo pool - se le stesse credenziali "
        "funzionano nell'app dev, il pool di arch 2.0 e' un altro (D-24 b)"
    ),
    "NotAuthorizedException": (
        "password errata, utente disabilitato, OPPURE utente inesistente "
        "in questo pool: con `PreventUserExistenceErrors` attivo (default "
        "Cognito) i due casi sono indistinguibili da fuori"
    ),
    "UserNotConfirmedException": (
        "l'utente esiste ma non ha mai completato la conferma: va "
        "confermato dall'app o dalla console prima di poterlo usare qui"
    ),
    "PasswordResetRequiredException": (
        "Cognito pretende un reset della password prima di autenticare"
    ),
    "InvalidParameterException": (
        "parametro rifiutato da Cognito. Se il messaggio dice `<FLOW> is not "
        "enabled for the client`, l'app client esiste ma non ha quel flusso "
        "abilitato: il rifiuto avviene PRIMA di guardare le credenziali, "
        "quindi non dice nulla sull'utente. Per l'integrazione serve "
        "`ALLOW_USER_SRP_AUTH`, che e' l'unico flusso che usa"
    ),
    "TooManyRequestsException": "Cognito sta limitando le richieste: riprova",
    "ResourceNotFoundException": (
        "pool id o client id inesistenti: e' la configurazione del pool a "
        "essere sbagliata, non le credenziali"
    ),
}


def _explain_cognito_code(code: str | None) -> str:
    """Traduce il codice Cognito in cosa significa per questa ricognizione."""
    if code is None:
        return "nessun codice Cognito nella catena di eccezioni"
    return _COGNITO_CODE_HINTS.get(code, "codice non fra quelli attesi")


def cognito_error_code(err: BaseException) -> str | None:
    """
    Recupera il codice d'errore Cognito grezzo dalla catena di eccezioni.

    `api/auth.py` classifica l'errore e rilancia un'eccezione propria
    (`AuthInvalidError`, ...) con `raise ... from err`, il che e' la cosa
    giusta per l'integrazione ma qui perde l'informazione che serve:
    `UserNotFoundException` e `NotAuthorizedException` finiscono entrambi in
    `AuthInvalidError`, e vogliono dire cose molto diverse - "l'utente non
    esiste in questo pool" contro "la password non e' quella". Il
    `__cause__` conserva la `ClientError` originale.
    """
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            if isinstance(code, str) and code:
                return code
        current = current.__cause__ or current.__context__
    return None


@dataclass
class PoolAttempt:
    """Esito del login su un pool e della prima chiamata all'host v2."""

    label: str
    pool_id: str
    client_id: str
    region: str
    login_ok: bool
    login_error: str | None
    login_error_code: str | None
    expires_in: int | None
    v2_status: int | None
    working_auth_style: str | None = None
    used_flow: str | None = None
    token: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """La forma serializzabile, senza il token."""
        return {
            "label": self.label,
            "pool_id": self.pool_id,
            "client_id": self.client_id,
            "region": self.region,
            "login_ok": self.login_ok,
            "login_error": self.login_error,
            "cognito_error_code": self.login_error_code,
            "flusso_usato": self.used_flow,
            "token_expires_in_seconds": self.expires_in,
            "v2_domains_status": self.v2_status,
            "auth_header_format_accettato": self.working_auth_style,
        }


def _warn_if_flow_unavailable(config: Config, auth_flows: list[dict[str, Any]]) -> None:
    """Avvisa subito se il flusso richiesto non e' abilitato da nessuna parte."""
    if config.auth_flow == "srp":
        return  # SRP non e' sondabile senza credenziali: lo dira' il login.
    wanted = "USER_PASSWORD_AUTH"
    enabled = [
        entry["pool"]
        for entry in auth_flows
        if not entry["flussi"].get(wanted, "").startswith("NON")
    ]
    if enabled:
        return
    configured = ", ".join(p.label for p in config.pools)
    print(
        f"\n  ATTENZIONE: {wanted} non e' abilitato su nessuno dei pool "
        f"configurati ({configured}).\n"
        "  Se l'app client che lo ha e' un altro, impostalo prima di "
        "rilanciare:\n"
        "    RADOFF_DEV_POOL_ID=<pool> RADOFF_DEV_CLIENT_ID=<client> ...\n"
        "  Il login qui sotto fallira' prima di guardare le credenziali."
    )


def attempt_pools(
    config: Config, recorder: Recorder, redactor: Redactor
) -> list[PoolAttempt]:
    """
    Login SRP su ogni pool configurato, poi la stessa chiamata su host v2.

    E' il test di D-24 nella sua forma minima: lo stesso account, la stessa
    richiesta, due token diversi. Quale dei due l'API 2.0 accetta e' la
    risposta.
    """
    attempts: list[PoolAttempt] = []
    for pool in config.pools:
        token: str | None = None
        login_error: str | None = None
        login_error_code: str | None = None
        expires_in: int | None = None
        used_flow: str | None = None

        flows = (
            ("srp", "password") if config.auth_flow == "auto" else (config.auth_flow,)
        )
        for flow in flows:
            label = "SRP" if flow == "srp" else "USER_PASSWORD_AUTH"
            print(f"\n[auth] login {label} su {pool.label} ({pool.pool_id})")
            if flow == "password":
                print(
                    "  ATTENZIONE: questo flusso manda la password a Cognito "
                    "in chiaro.\n  E' una diagnostica, non il flusso "
                    "dell'integrazione: usalo solo con un account di prova."
                )
            factory = CognitoSession if flow == "srp" else PasswordSession
            session = factory(
                username=config.username,
                password=config.password,
                client_id=pool.client_id,
                pool_id=pool.pool_id,
                pool_region=pool.region,
            )
            try:
                token = session.get_bearer()
            except Exception as err:  # noqa: BLE001 - qualunque esito e' un dato
                login_error = redactor.scrub_text(f"{type(err).__name__}: {err}")
                login_error_code = cognito_error_code(err)
                print(f"  login fallito: {login_error}")
                print(f"  codice Cognito: {login_error_code or 'non disponibile'}")
                print(f"  {_explain_cognito_code(login_error_code)}")
            else:
                used_flow = flow
                login_error = None
                login_error_code = None
                expires_in = session.tokens.get("ExpiresIn")
                print(f"  login ok con {label}, ExpiresIn={expires_in}s")
                break

        v2_status: int | None = None
        working_auth_style: str | None = None
        if token:
            for style in ("bearer", "raw"):
                suffix = "" if style == "bearer" else f"__{style}"
                call = recorder.get(
                    f"auth_domains__{pool.label}{suffix}",
                    step="D-24",
                    path=DISCOVERY_PATH,
                    token=token,
                    auth_style=style,
                )
                if v2_status is None or call.ok:
                    v2_status = call.status
                if call.ok:
                    working_auth_style = style
                    break

        attempts.append(
            PoolAttempt(
                label=pool.label,
                pool_id=pool.pool_id,
                client_id=pool.client_id,
                region=pool.region,
                login_ok=token is not None,
                login_error=login_error,
                login_error_code=login_error_code,
                used_flow=used_flow,
                expires_in=expires_in,
                v2_status=v2_status,
                working_auth_style=working_auth_style,
                token=token,
            )
        )
    return attempts


def pick_working_token(attempts: list[PoolAttempt]) -> PoolAttempt | None:
    """Il primo pool il cui token l'host v2 ha accettato."""
    for attempt in attempts:
        if attempt.token and attempt.v2_status == 200:
            return attempt
    return None


def probe_discovery_paths(
    recorder: Recorder, token: str, auth_style: str = "bearer"
) -> None:
    """
    Prova i path alternativi della discovery quando il primo fallisce.

    Serve a separare due cause che API Gateway rende indistinguibili: un
    endpoint autorizzato diversamente (IAM invece dell'authorizer Cognito) e
    un path che semplicemente non esiste. In entrambi i casi, se l'header
    `Authorization` c'e', la risposta e' `403 IncompleteSignatureException`.
    Un candidato che risponde 401 `UnauthorizedException`, o qualunque cosa
    di applicativo, e' un path che ESISTE ed e' dietro l'authorizer: quello
    e' l'endpoint giusto.
    """
    print("\n[discovery] sonda dei path alternativi")
    for path in DISCOVERY_PATH_CANDIDATES[1:]:
        slug = path.strip("/").replace("/", "_")
        recorder.get(
            f"discovery_path__{slug}",
            step="D-03",
            path=path,
            token=token,
            auth_style=auth_style,
        )


def probe_route_existence(recorder: Recorder) -> None:
    """
    Stabilisce se le risorse esistono, senza bisogno di un token valido.

    Due sonde per path, entrambe non autenticate:

    - **GET senza header `Authorization`**. Senza header, API Gateway non
      puo' tentare la lettura SigV4, quindi `IncompleteSignatureException`
      sparisce e resta la risposta vera: `MissingAuthenticationTokenException`
      se la risorsa non esiste (o e' IAM), 401 `UnauthorizedException` se
      esiste ed e' dietro l'authorizer Cognito.
    - **OPTIONS**. Il preflight CORS e' quasi sempre un'integrazione MOCK
      senza autorizzazione: se risponde 200, la risorsa **esiste**, e allora
      il 403 sulla GET e' un fatto di autorizzazione, non di routing.

    Insieme separano le due letture che una GET autenticata confonde:
    "la route non c'e'" e "la route c'e' ma vuole una firma IAM".
    """
    print("\n[routing] esistenza delle risorse, senza autenticazione")
    for path in (
        *DISCOVERY_PATH_CANDIDATES[:2],
        "/analytics/measures-ranges",
        "/data/devices",
    ):
        slug = path.strip("/").replace("/", "_")
        recorder.get(
            f"route__{slug}__noauth",
            step="D-03",
            path=path,
            token=None,
        )
        recorder.options(f"route__{slug}__options", step="D-03", path=path)


def probe_auth_flows(config: Config) -> list[dict[str, Any]]:
    """
    Enumera i flussi di autenticazione abilitati su ogni app client.

    Cognito rifiuta un flusso non abilitato con `InvalidParameterException`
    **prima** di guardare le credenziali, quindi si puo' interrogare con un
    utente inesistente e una password fittizia: nessun account reale viene
    toccato e nessun contatore di login falliti si muove. Un flusso
    abilitato risponde `NotAuthorizedException`/`UserNotFoundException` -
    cioe' "sono arrivato a cercare l'utente", che e' esattamente il segnale
    che ci serve.

    Serve a trasformare "il login non funziona" in "su questo app client
    sono abilitati questi flussi e non quest'altro", che e' una richiesta
    verificabile invece di una segnalazione.
    """
    import boto3  # noqa: PLC0415 - usati solo da questa funzione
    from botocore.exceptions import ClientError  # noqa: PLC0415
    from pycognito.aws_srp import AWSSRP  # noqa: PLC0415

    print("\n[cognito] flussi di autenticazione abilitati per app client")
    probe_user = "probe-user-does-not-exist@example.invalid"
    results: list[dict[str, Any]] = []
    for pool in config.pools:
        client = boto3.client("cognito-idp", region_name=pool.region)
        flows: dict[str, str] = {}
        # SRP non passa da `initiate_auth` diretto: pycognito fa l'handshake.
        # Va sondato con lo stesso utente fittizio degli altri, perche' e'
        # l'unico modo di distinguere "il flusso non e' abilitato sull'app
        # client" da "quell'utente non va bene": se l'errore e' identico con
        # un utente che non esiste, la causa non puo' essere l'utenza.
        try:
            AWSSRP(
                username=probe_user,
                password="not-a-real-password",  # noqa: S106 - utente inesistente
                pool_id=pool.pool_id,
                client_id=pool.client_id,
                pool_region=pool.region,
            ).authenticate_user()
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "?")
            flows["USER_SRP_AUTH"] = (
                "NON abilitato"
                if code == "InvalidParameterException"
                else f"abilitato (risposta: {code})"
            )
        except Exception as err:  # noqa: BLE001 - qualunque esito e' un dato
            flows["USER_SRP_AUTH"] = f"esito non classificato: {type(err).__name__}"
        else:  # pragma: no cover - un login fittizio non puo' riuscire
            flows["USER_SRP_AUTH"] = "abilitato"

        for flow, params in (
            ("USER_PASSWORD_AUTH", {"USERNAME": probe_user, "PASSWORD": "x"}),
            ("REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": "not-a-real-token"}),
        ):
            try:
                client.initiate_auth(
                    ClientId=pool.client_id, AuthFlow=flow, AuthParameters=params
                )
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "?")
                flows[flow] = (
                    "NON abilitato"
                    if code == "InvalidParameterException"
                    else f"abilitato (risposta: {code})"
                )
            else:  # pragma: no cover - un login fittizio non puo' riuscire
                flows[flow] = "abilitato"
        results.append(
            {"pool": pool.label, "client_id": pool.client_id, "flussi": flows}
        )
        for flow, verdict in flows.items():
            print(f"  {pool.label:<14} {flow:<22} {verdict}")
    return results


def _total_of(body: Any) -> int:
    """Il `pagination.total` di una risposta, o il numero di righe trovate."""
    if isinstance(body, dict):
        pagination = body.get("pagination")
        if isinstance(pagination, dict) and isinstance(pagination.get("total"), int):
            return pagination["total"]
    return len(find_list_of_objects(body))


def pick_domain_with_devices(
    recorder: Recorder,
    token: str,
    domains_body: Any,
    auth_style: str = "bearer",
) -> str | None:
    """
    Sceglie il dominio piu' adatto alla ricognizione, e censisce gli altri.

    Non basta "ha device": il primo dominio della lista puo' essere vuoto, e
    uno pieno di device che non hanno mai trasmesso (`telemetry: null`, →
    D-16) fa uscire "non osservato" da ogni verifica sui valori. Nemmeno
    basta "ha telemetria": con due device la verifica sull'ordinamento in
    paginazione (V1) non dimostra niente, perche' la seconda pagina e'
    vuota.

    Scandisce quindi tutti i domini e sceglie, in ordine di preferenza, **il
    piu' popoloso fra quelli che hanno telemetria**, poi il piu' popoloso in
    assoluto, poi il primo. Il censimento che ne esce - quanti device per
    dominio - e' anche il dato che serve a D-20.
    """
    items = find_list_of_objects(domains_body)
    if not items:
        return None
    print(f"\n[domini] censimento dei {len(items)} domini dell'utente")
    census: list[tuple[str, int, bool]] = []
    for item in items:
        prefix = _domain_object(item).get("prefix")
        if not isinstance(prefix, str) or not prefix:
            continue
        call = recorder.get(
            f"domain_scan__{prefix}",
            step="D-20",
            path="/data/devices",
            token=token,
            auth_style=auth_style,
            params={"domain_prefix": prefix, "page_size": 5},
            save=False,
        )
        if not call.ok:
            continue
        total = _total_of(call.body)
        live = _has_telemetry(call.body)
        census.append((prefix, total, live))
        if total:
            print(
                f"  {prefix:<18} {total:>4} device"
                + ("  (con telemetria)" if live else "  (tutti muti)")
            )
    if not census:
        return None

    with_data = [row for row in census if row[2]]
    populated = [row for row in census if row[1]]
    if with_data:
        chosen = max(with_data, key=lambda row: row[1])
        print(f"  scelto {chosen[0]}: {chosen[1]} device, con telemetria")
    elif populated:
        chosen = max(populated, key=lambda row: row[1])
        print(
            f"  nessun dominio ha telemetria: uso {chosen[0]} ({chosen[1]} "
            "device).\n  Le verifiche sui valori resteranno non osservate."
        )
    else:
        chosen = census[0]
        print("  nessun dominio ha device: proseguo con il primo")
    return chosen[0]


def _has_telemetry(body: Any) -> bool:
    """
    Vero se almeno un device della risposta porta telemetria valorizzata.

    Un device che non ha mai trasmesso arriva con `telemetry: null` (→ D-16),
    ed e' indistinguibile da uno vivo finche' non si guarda quel campo. Un
    dominio pieno di device muti produce una ricognizione che risponde 200 a
    tutto e non osserva niente: e' il caso che questa funzione evita.
    """
    for device in find_list_of_objects(body):
        telemetry = device.get("telemetry")
        if isinstance(telemetry, dict) and telemetry:
            return True
        if isinstance(telemetry, list) and telemetry:
            return True
    return False


def probe_measures_ranges(
    recorder: Recorder, token: str, auth_style: str = "bearer"
) -> None:
    """Un `measures-ranges` per tipo, uno senza tipo, uno con tipo ignoto."""
    print("\n[schema] measures-ranges")
    for device_type in DEVICE_TYPES:
        recorder.get(
            f"measures_ranges__{device_type}",
            step="schema",
            path="/analytics/measures-ranges",
            token=token,
            auth_style=auth_style,
            params={"device_type": device_type},
        )
    recorder.get(
        "measures_ranges__all",
        step="schema",
        path="/analytics/measures-ranges",
        token=token,
        auth_style=auth_style,
    )
    recorder.get(
        "error__measures_ranges_unknown_type",
        step="D-29",
        path="/analytics/measures-ranges",
        token=token,
        auth_style=auth_style,
        params={"device_type": UNKNOWN_DEVICE_TYPE},
    )


def probe_devices(
    config: Config,
    recorder: Recorder,
    token: str,
    domain_prefix: str,
    auth_style: str = "bearer",
) -> list[Call]:
    """Paginazione, ripetizione, passata piena e sonde di ordinamento."""
    print("\n[devices] paginazione e ordinamento")
    base = {"domain_prefix": domain_prefix}
    small = config.small_page_size

    calls = [
        recorder.get(
            "devices__page1_small",
            step="D-19",
            path="/data/devices",
            token=token,
            auth_style=auth_style,
            params={**base, "page": 1, "page_size": small},
        ),
        recorder.get(
            "devices__page2_small",
            step="D-19",
            path="/data/devices",
            token=token,
            auth_style=auth_style,
            params={**base, "page": 2, "page_size": small},
        ),
        # Stessa richiesta della prima: se l'ordinamento non e' stabile fra
        # chiamate ripetute, e' qui che si vede.
        recorder.get(
            "devices__page1_small_repeat",
            step="D-19",
            path="/data/devices",
            token=token,
            auth_style=auth_style,
            params={**base, "page": 1, "page_size": small},
        ),
        recorder.get(
            "devices__full",
            step="polling",
            path="/data/devices",
            token=token,
            auth_style=auth_style,
            params={**base, "page": 1, "page_size": config.full_page_size},
        ),
    ]

    # Esistono filtri o ordinamenti? Gli swagger non li documentano: l'unico
    # modo di saperlo e' provarli e guardare status e forma della risposta.
    for param in ("sort", "order_by", "order"):
        recorder.get(
            f"devices__probe_{param}",
            step="D-19",
            path="/data/devices",
            token=token,
            auth_style=auth_style,
            params={**base, "page_size": small, param: "serial_number"},
        )
    return calls


def probe_device_detail(
    recorder: Recorder, token: str, serial: str, auth_style: str = "bearer"
) -> None:
    """Il dettaglio di un device reale, piu' quello di un serial inesistente."""
    print("\n[device] dettaglio")
    recorder.get(
        "device_detail",
        step="polling",
        path=f"/data/devices/{serial}",
        token=token,
        auth_style=auth_style,
    )
    recorder.get(
        "error__device_detail_unknown_serial",
        step="D-29",
        path=f"/data/devices/{UNKNOWN_SERIAL}",
        token=token,
        auth_style=auth_style,
    )


def probe_errors(
    config: Config, recorder: Recorder, token: str, auth_style: str = "bearer"
) -> None:
    """Errori provocati deliberatamente, per la tassonomia di M-02 (D-29)."""
    print("\n[errori] provocati deliberatamente")
    recorder.get(
        "error__devices_foreign_domain",
        step="D-29",
        path="/data/devices",
        token=token,
        auth_style=auth_style,
        params={
            "domain_prefix": config.foreign_domain_prefix,
            "page_size": config.small_page_size,
        },
    )
    recorder.get(
        "error__devices_no_domain_prefix",
        step="D-29",
        path="/data/devices",
        token=token,
        auth_style=auth_style,
        params={"page_size": config.small_page_size},
    )
    recorder.get(
        "error__unauthenticated",
        step="D-29",
        path="/data/devices",
        token=None,
        params={"page_size": config.small_page_size},
    )


# --------------------------------------------------------------------------
# Verifiche (T-08 6) ricavate dalle response, non scritte a mano
# --------------------------------------------------------------------------
_REQUEST_ID_HINTS = ("request-id", "requestid", "apigw-id", "trace-id", "correlation")


def _body_of(calls: list[Call], name: str) -> Any:
    for call in calls:
        if call.name == name:
            return call.body
    return None


def _raw_body_of(calls: list[Call], name: str) -> Any:
    for call in calls:
        if call.name == name:
            return call.raw_body
    return None


def _call_named(calls: list[Call], name: str) -> Call | None:
    for call in calls:
        if call.name == name:
            return call
    return None


# API Gateway mette in `x-amzn-errortype` il motivo del rifiuto quando a
# rispondere e' lui e non l'applicazione. `IncompleteSignatureException` in
# particolare vuol dire che ha provato a leggere l'header `Authorization`
# come una firma SigV4: il JWT non e' stato nemmeno valutato, quindi un
# rifiuto del genere NON dice niente sul pool Cognito di provenienza.
# L'authorizer Cognito, quando valuta il JWT e lo respinge, risponde 401
# `{"message": "Unauthorized"}` con questo errortype. E' l'unico rifiuto
# che risponde davvero a D-24: gli altri avvengono prima della validazione.
_AUTHORIZER_LEVEL_ERRORS = frozenset({"UnauthorizedException", "AccessDeniedException"})

_GATEWAY_LEVEL_ERRORS = frozenset(
    {
        "IncompleteSignatureException",
        "MissingAuthenticationTokenException",
        "InvalidSignatureException",
        "UnrecognizedClientException",
    }
)


def _amzn_error_type(call: Call | None) -> str | None:
    """L'`x-amzn-errortype` della risposta, se API Gateway lo ha messo."""
    if call is None:
        return None
    return call.response_headers.get("x-amzn-errortype")


def _base_path(url: str) -> str:
    """`https://host/analytics/measures-ranges` -> `/analytics/*`."""
    path = url.split("://", 1)[-1].partition("/")[2]
    first = path.partition("/")[0]
    return f"/{first}/*" if first else "/"


NOT_OBSERVED = "non osservato"


def _observed(calls: list[Call], *names: str) -> bool:
    """
    Vero se almeno una delle chiamate da cui la verifica dipende ha risposto 2xx.

    Serve a non spacciare per esito il valore di default di un'analisi che
    non ha mai visto un payload: senza questo controllo una lista vuota di
    serial produce "ordinamento stabile", e un `measures-ranges` andato in
    401 produce "il sismoff non espone i PM". Sono affermazioni false, e
    finiscono dritte nel commento su Jira.
    """
    return any(call.ok for call in calls if call.name in names or not names)


def _finding(fid: str, question: str, outcome: str, evidence: Any) -> dict[str, Any]:
    return {
        "id": fid,
        "question": question,
        "outcome": outcome,
        "evidence": evidence,
    }


def analyze(
    calls: list[Call],
    attempts: list[PoolAttempt],
    domains_body: Any,
    auth_flows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Calcola un esito per ogni verifica dell'elenco della card."""
    findings: list[dict[str, Any]] = []

    # --- D-24 -------------------------------------------------------------
    accepted = [a.label for a in attempts if a.v2_status == 200]
    rejected = [
        f"{a.label}={a.v2_status if a.login_ok else 'login fallito'}"
        for a in attempts
        if a.v2_status != 200
    ]
    # Tre esiti, non due: "il login non e' nemmeno riuscito" non e' una
    # risposta a D-24, che chiede se l'host v2 accetta un token del pool
    # attuale. Senza token, l'host v2 non e' mai stato interrogato.
    current = next((a for a in attempts if a.label == "pool_current"), None)
    gateway_error = next(
        (
            err
            for call in calls
            for err in [_amzn_error_type(call)]
            if err in _GATEWAY_LEVEL_ERRORS
        ),
        None,
    )
    # Un rifiuto dell'authorizer su QUALUNQUE base path risponde a D-24: il
    # JWT e' stato valutato e respinto. Vale piu' del rifiuto della sola
    # discovery, che avviene prima della validazione e non guarda il token.
    authorizer_rejections = sorted(
        {
            _base_path(call.url)
            for call in calls
            # Solo chiamate AUTENTICATE: un 401 su una richiesta senza token
            # e' l'authorizer che fa il suo lavoro, non un JWT respinto, e
            # contarlo faceva concludere "token non accettato" proprio nelle
            # esecuzioni in cui il token era stato accettato.
            if call.auth_style != "none"
            and _amzn_error_type(call) in _AUTHORIZER_LEVEL_ERRORS
        }
    )
    if "pool_current" in accepted:
        d24_outcome = "accettato"
    elif authorizer_rejections:
        d24_outcome = (
            "NON accettato: l'authorizer Cognito ha valutato il JWT e lo ha "
            "respinto (401 UnauthorizedException) su "
            + ", ".join(authorizer_rejections)
            + " - l'host v2 usa un user pool diverso da quello in const.py"
        )
    elif gateway_error is not None:
        d24_outcome = (
            "NON DETERMINATO: la discovery e' stata rifiutata da API Gateway "
            f"con `{gateway_error}`, cioe' PRIMA di validare il JWT. Il "
            "rifiuto riguarda come l'endpoint e' autorizzato, non da quale "
            "pool viene il token - vedi D-24-surface"
        )
    elif current is not None and not current.login_ok:
        d24_outcome = (
            "NON DETERMINATO: il login SRP sul pool attuale non ha prodotto "
            "un token ("
            + (current.login_error_code or "codice Cognito non disponibile")
            + "), quindi l'host v2 non e' mai stato interrogato"
        )
    else:
        d24_outcome = "NON accettato - la migrazione ha una dipendenza in piu'"
    findings.append(
        _finding(
            "D-24",
            "Il token del pool Cognito attuale e' accettato dall'host v2?",
            d24_outcome,
            {
                "pools": [a.to_dict() for a in attempts],
                "accettati": accepted,
                "rifiutati": rejected,
            },
        )
    )

    # --- D-28 -------------------------------------------------------------
    # Un app client puo' esistere, essere quello giusto, e comunque non
    # permettere il login: i flussi di autenticazione si abilitano uno per
    # uno. Se SRP non e' fra quelli, questa integrazione non ha un piano B -
    # e' l'unico flusso che pycognito implementa e l'unico che non manda la
    # password in chiaro all'API.
    login_failures = [a for a in attempts if not a.login_ok]
    # Il dato che conta viene dall'enumerazione con utente inesistente, non
    # dal login: un login fallito puo' dipendere dalle credenziali, mentre
    # un flusso rifiutato per un utente che non esiste puo' dipendere solo
    # dall'app client.
    srp_disabled = [
        entry["pool"]
        for entry in (auth_flows or [])
        if entry["flussi"].get("USER_SRP_AUTH", "").startswith("NON")
    ] or [
        a.label
        for a in login_failures
        if a.login_error and "USER_SRP_AUTH is not enabled" in a.login_error
    ]
    if srp_disabled:
        d28_outcome = (
            "SRP NON abilitato sull'app client di "
            + ", ".join(srp_disabled)
            + ". Verificato con un utente INESISTENTE, quindi la causa e' "
            "l'app client e non l'utenza: serve `ALLOW_USER_SRP_AUTH` su "
            "quel client, oppure il client id di un altro app client dello "
            "stesso pool che lo abbia gia'"
        )
    elif login_failures and any(a.login_ok for a in attempts):
        d28_outcome = (
            "login riuscito su "
            + ", ".join(a.label for a in attempts if a.login_ok)
            + "; fallito su "
            + ", ".join(
                f"{a.label}={a.login_error_code or 'errore non classificato'}"
                for a in login_failures
            )
            + " (atteso se quel pool non ha il flusso richiesto)"
        )
    elif login_failures:
        d28_outcome = "login fallito su: " + ", ".join(
            f"{a.label}={a.login_error_code or 'errore non classificato'}"
            for a in login_failures
        )
    else:
        d28_outcome = "login riuscito su tutti i pool provati"
    findings.append(
        _finding(
            "D-28",
            "L'app client Cognito permette il flusso SRP che l'integrazione usa?",
            d28_outcome,
            {
                "flussi_per_app_client": auth_flows,
                "per_pool": [
                    {
                        "label": a.label,
                        "pool_id": a.pool_id,
                        "client_id": a.client_id,
                        "login_ok": a.login_ok,
                        "cognito_error_code": a.login_error_code,
                        "errore": a.login_error,
                        "token_expires_in_seconds": a.expires_in,
                    }
                    for a in attempts
                ],
            },
        )
    )

    # --- D-24-surface -----------------------------------------------------
    # Tre base path mapping distinti sullo stesso host (T-02 D-01): possono
    # avere autorizzazioni diverse, e un rifiuto su uno non implica gli altri.
    surface: dict[str, dict[str, Any]] = {}
    for call in calls:
        # Solo chiamate AUTENTICATE: una OPTIONS di preflight risponde 200
        # senza token, e contarla direbbe che il base path "accetta il
        # token" quando non gliene e' stato mostrato nessuno.
        if call.status is None or call.auth_style == "none":
            continue
        entry = surface.setdefault(
            _base_path(call.url),
            {"status": {}, "errortype": set(), "auth_style": set()},
        )
        entry["status"][str(call.status)] = entry["status"].get(str(call.status), 0) + 1
        errortype = _amzn_error_type(call)
        if errortype:
            entry["errortype"].add(errortype)
        entry["auth_style"].add(call.auth_style)
    surface_serializable = {
        path: {
            "status": entry["status"],
            "errortype": sorted(entry["errortype"]),
            "auth_style_provati": sorted(entry["auth_style"]),
        }
        for path, entry in surface.items()
    }
    reachable = sorted(
        path
        for path, entry in surface.items()
        if any(status.startswith("2") for status in entry["status"])
    )
    findings.append(
        _finding(
            "D-24-surface",
            "Quali base path dell'host v2 accettano il token, e quali no?",
            (
                "accettano: " + ", ".join(reachable)
                if reachable
                else "nessun base path ha accettato il token"
            ),
            surface_serializable,
        )
    )

    # --- D-03 -------------------------------------------------------------
    discovery_calls = [c for c in calls if c.name.startswith("auth_domains__")]
    discovery_errors = {err for c in discovery_calls if (err := _amzn_error_type(c))}
    # Ragiona per BASE PATH, non per singolo path. Un base path dietro
    # l'authorizer Cognito risponde 401 a qualunque cosa, path inesistenti
    # compresi: un 401 li' non prova che quel path esista, prova solo che
    # l'authorizer intercetta prima del routing. Con un token che l'authorizer
    # rifiuta, "quale path esiste" non e' una domanda a cui si possa
    # rispondere dall'esterno - e dirlo e' piu' utile che tirare a indovinare.
    path_probes = [c for c in calls if c.name.startswith("discovery_path__")]
    all_discovery = discovery_calls + path_probes
    per_base_path: dict[str, set[str]] = {}
    for call in all_discovery:
        per_base_path.setdefault(_base_path(call.url), set()).add(
            _amzn_error_type(call) or f"applicativo/{call.status}"
        )
    authorizer_fronted = sorted(
        path
        for path, kinds in per_base_path.items()
        if kinds & _AUTHORIZER_LEVEL_ERRORS
    )
    gateway_only = sorted(
        path
        for path, kinds in per_base_path.items()
        if kinds and kinds <= _GATEWAY_LEVEL_ERRORS
    )
    if any(c.ok for c in discovery_calls):
        d03_outcome = "chiamabile con il solo bearer token"
    elif discovery_errors & _GATEWAY_LEVEL_ERRORS and gateway_only:
        d03_outcome = (
            "NESSUNA variante sotto "
            + ", ".join(gateway_only)
            + " raggiunge l'authorizer Cognito: tutte rifiutate a livello di "
            "firma SigV4 ("
            + ", ".join(sorted(discovery_errors))
            + "). Gli altri base path ("
            + (", ".join(authorizer_fronted) if authorizer_fronted else "nessuno")
            + ") lo raggiungono, quindi non e' un problema di token ma di come "
            "quel base path e' esposto. NB: con un token che l'authorizer "
            "rifiuta non e' possibile stabilire dall'esterno QUALE path esista"
        )
    elif discovery_errors & _GATEWAY_LEVEL_ERRORS:
        d03_outcome = (
            "rifiutata a livello di firma SigV4 ("
            + ", ".join(sorted(discovery_errors))
            + "), path alternativi non sondati"
        )
    else:
        d03_outcome = "rifiutata: " + ", ".join(
            f"{c.auth_style}={c.status}" for c in discovery_calls
        )
    findings.append(
        _finding(
            "D-03",
            "`GET /auth/user/me/domains` e' chiamabile con il solo bearer "
            "token, come previsto dal config flow?",
            d03_outcome,
            {
                "tentativi": [
                    {
                        "auth_style": c.auth_style,
                        "status": c.status,
                        "errortype": _amzn_error_type(c),
                        "body": c.body,
                    }
                    for c in discovery_calls
                ],
                "path_alternativi": [
                    {
                        "name": c.name,
                        "url": c.url,
                        "status": c.status,
                        "errortype": _amzn_error_type(c),
                        "body": c.body,
                    }
                    for c in path_probes
                ],
                "errortype_per_base_path": {
                    path: sorted(kinds) for path, kinds in per_base_path.items()
                },
                "base_path_dietro_authorizer": authorizer_fronted,
                "base_path_solo_gateway": gateway_only,
            },
        )
    )

    # --- D-03-routes ------------------------------------------------------
    # Le sonde non autenticate: e' qui che si stabilisce se una risorsa
    # esiste. `MissingAuthenticationTokenException` su una GET senza header
    # non discrimina da sola (anche un metodo IAM risponde cosi'), ma una
    # OPTIONS che fallisce mentre le sorelle rispondono 200 dice che su quel
    # path non c'e' nemmeno il preflight CORS - cioe' non c'e' la risorsa.
    route_calls = [c for c in calls if c.name.startswith("route__")]
    routes: dict[str, dict[str, Any]] = {}
    for call in route_calls:
        key = call.url[call.url.index("/", len("https://")) :]
        entry = routes.setdefault(key, {})
        entry[call.method] = {
            "status": call.status,
            "errortype": _amzn_error_type(call),
            "cors": call.response_headers.get("access-control-allow-methods"),
        }
    existing = sorted(
        path
        for path, methods in routes.items()
        if (methods.get("OPTIONS") or {}).get("status") == HTTP_OK
    )
    missing = sorted(set(routes) - set(existing))
    if route_calls:
        findings.append(
            _finding(
                "D-03-routes",
                "Quali risorse esistono davvero sull'host v2, a prescindere dal token?",
                (
                    "esistono: "
                    + (", ".join(existing) if existing else "nessuna")
                    + " | NON raggiungibili: "
                    + (", ".join(missing) if missing else "nessuna")
                ),
                routes,
            )
        )

    # --- D-30 -------------------------------------------------------------
    header_names: dict[str, str] = {}
    for call in calls:
        for header, value in call.response_headers.items():
            if any(hint in header for hint in _REQUEST_ID_HINTS):
                header_names.setdefault(header, value)
    findings.append(
        _finding(
            "D-30",
            "Qual e' il nome dell'header di request id nelle risposte?",
            ", ".join(sorted(header_names)) if header_names else "nessuno osservato",
            {
                "header_candidati": header_names,
                "header_visti_su_una_risposta": sorted(
                    {h for c in calls for h in c.response_headers}
                ),
            },
        )
    )

    # --- D-29 -------------------------------------------------------------
    error_calls = [c for c in calls if c.name.startswith("error__")]
    findings.append(
        _finding(
            "D-29",
            "Tassonomia degli errori: status e forma del body per ogni caso.",
            f"{len(error_calls)} casi provocati",
            [
                {
                    "caso": c.name,
                    "status": c.status,
                    "chiavi_body": sorted(c.body) if isinstance(c.body, dict) else None,
                    "body": c.body,
                }
                for c in error_calls
            ],
        )
    )

    # --- V1: ordinamento e filtri ----------------------------------------
    page1 = serials_of(_body_of(calls, "devices__page1_small"))
    page2 = serials_of(_body_of(calls, "devices__page2_small"))
    repeat = serials_of(_body_of(calls, "devices__page1_small_repeat"))
    full = serials_of(_body_of(calls, "devices__full"))
    paginated = page1 + page2
    overlap = sorted(set(page1) & set(page2))
    stable_repeat = page1 == repeat
    stable_vs_full = full[: len(paginated)] == paginated if full else None
    sort_probes = {
        name: _call_named(calls, f"devices__probe_{name}").status
        for name in ("sort", "order_by", "order")
        if _call_named(calls, f"devices__probe_{name}") is not None
    }
    findings.append(
        _finding(
            "V1",
            "L'ordinamento di GET /data/devices e' stabile fra pagine e fra "
            "chiamate ripetute? Esistono filtri o ordinamenti?",
            (
                (
                    "stabile"
                    if stable_repeat and stable_vs_full is not False
                    else "NON stabile"
                )
                if _observed(
                    calls, "devices__page1_small", "devices__page1_small_repeat"
                )
                else NOT_OBSERVED + " (nessuna chiamata a /data/devices riuscita)"
            ),
            {
                "pagina_1": page1,
                "pagina_2": page2,
                "pagina_1_ripetuta": repeat,
                "stabile_fra_chiamate_ripetute": stable_repeat,
                "stabile_fra_paginazione_e_page_size_grande": stable_vs_full,
                "serial_duplicati_fra_pagina_1_e_2": overlap,
                "totale_con_page_size_grande": len(full),
                "sonde_di_ordinamento_status": sort_probes,
            },
        )
    )

    # --- V2: enumerazione delle unit --------------------------------------
    units = collect_units(_body_of(calls, "measures_ranges__all"))
    findings.append(
        _finding(
            "V2",
            "Enumerazione completa dei valori di `unit` "
            "(measures-ranges senza device_type).",
            f"{len(units)} unita' distinte" if units else "non osservato",
            units,
        )
    )

    # --- V3: pm10 excellent upperBound ------------------------------------
    pm10 = find_measure_entry(_body_of(calls, "measures_ranges__all"), "pm10")
    findings.append(
        _finding(
            "V3",
            "pm10 ha davvero `excellent upperBound: 200` a runtime, o solo "
            "negli esempi?",
            (
                ("vedi evidenza" if pm10 else "pm10 non presente in measures-ranges")
                if _observed(calls, "measures_ranges__all")
                else NOT_OBSERVED + " (measures-ranges non ha risposto 2xx)"
            ),
            pm10,
        )
    )

    # --- V4: PM sul sismoff ------------------------------------------------
    sismoff_body = _body_of(calls, "measures_ranges__sismoff")
    sismoff_pm = {
        measure: find_measure_entry(sismoff_body, measure) is not None
        for measure in ("pm1", "pm25", "pm10")
    }
    findings.append(
        _finding(
            "V4",
            "Il sismoff espone pm1/pm25/pm10? (T-02 dice no, lo swagger dice si')",
            (
                ("li espone" if any(sismoff_pm.values()) else "non li espone")
                if _observed(calls, "measures_ranges__sismoff")
                else NOT_OBSERVED + " (measures-ranges?device_type=sismoff non ha "
                "risposto 2xx)"
            ),
            {
                "presenza": sismoff_pm,
                "status_chiamata": (
                    _call_named(calls, "measures_ranges__sismoff").status
                    if _call_named(calls, "measures_ranges__sismoff")
                    else None
                ),
            },
        )
    )

    # --- V5, V7, V8: valori realmente osservati nei payload ---------------
    observed: dict[str, list[Any]] = {}
    for call in calls:
        if call.name.startswith(("devices__", "device_detail")):
            for measure, values in walk_measures(call.body).items():
                observed.setdefault(measure, []).extend(values)

    radon_status = {
        name: sorted({str(v) for v in values})
        for name, values in observed.items()
        if _normalize_key(name) == "radonstatus"
    }
    radon_types = {
        name: sorted({type(v).__name__ for v in values})
        for name, values in observed.items()
        if _normalize_key(name) == "radonstatus"
    }
    all_null = bool(radon_status) and all(
        types == ["NoneType"] for types in radon_types.values()
    )
    if not _observed(calls, "devices__full", "devices__page1_small", "device_detail"):
        radon_outcome = NOT_OBSERVED + " (nessun payload di device osservato)"
    elif not radon_status:
        radon_outcome = (
            "campo NON presente nei payload di questo account (nessun "
            "sense/city: richiesta operativa T-08 7)"
        )
    elif all_null:
        radon_outcome = (
            "campo presente ma sempre `null` su questo account - il tipo "
            "reale resta non osservato (serve un sense o un city)"
        )
    else:
        radon_outcome = "osservato"
    findings.append(
        _finding(
            "V5",
            "Tipo reale e valori osservati di `radon_status`.",
            radon_outcome,
            {"valori": radon_status, "tipi_python": radon_types},
        )
    )

    # --- V6: forma di /auth/user/me/domains -------------------------------
    domain_items = find_list_of_objects(domains_body)
    domain_keys = sorted({k for item in domain_items for k in _domain_object(item)})
    has_prefix = any(_normalize_key(k) == "prefix" for k in domain_keys)
    has_name = any(
        _normalize_key(k) in {"name", "displayname", "label"} for k in domain_keys
    )
    findings.append(
        _finding(
            "V6",
            f"La response di {DISCOVERY_PATH} contiene `prefix` e un nome "
            "visualizzabile distinto dal prefisso?",
            (
                (
                    "prefix + nome presenti"
                    if has_prefix and has_name
                    else f"prefix={has_prefix}, nome_visualizzabile={has_name}"
                )
                if domain_items
                else NOT_OBSERVED + " (la discovery non ha restituito domini)"
            ),
            {
                "chiavi_per_dominio": domain_keys,
                "numero_domini": len(domain_items),
                "esempio": domain_items[0] if domain_items else None,
                "chiavi_di_primo_livello": sorted(
                    {k for item in domain_items for k in item}
                ),
            },
        )
    )

    pressure = [
        v
        for name, values in observed.items()
        if _normalize_key(name) == "pressure"
        for v in values
        if isinstance(v, (int, float))
    ]
    findings.append(
        _finding(
            "V7",
            "Ordine di grandezza di `pressure`: Pa (~101300) come atteso?",
            _magnitude_verdict(pressure, expected=101_300, alternative=1013),
            {"valori_osservati": pressure[:20]},
        )
    )

    internal_temp = [
        v
        for name, values in observed.items()
        if _normalize_key(name) == "internaltemperature"
        for v in values
        if isinstance(v, (int, float))
    ]
    findings.append(
        _finding(
            "V8",
            "`internal_temperature` arriva in C (~21) o in centesimi (~2100)?",
            _magnitude_verdict(internal_temp, expected=21, alternative=2100),
            {"valori_osservati": internal_temp[:20]},
        )
    )

    return findings


def _magnitude_verdict(values: list[float], expected: float, alternative: float) -> str:
    """Dice quale dei due ordini di grandezza attesi corrisponde ai valori."""
    if not values:
        return "non osservato"
    sample = values[0]
    if abs(sample - expected) < abs(sample - alternative):
        return f"ordine di grandezza {expected} (es. {sample})"
    return f"ordine di grandezza {alternative} (es. {sample})"


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def _domain_object(item: dict[str, Any]) -> dict[str, Any]:
    """
    Il blocco che descrive il dominio dentro una voce della discovery.

    Lo swagger di `getUserDomains` annida: ogni voce e'
    `{domain: {prefix, name, ...}, role: {...}, assigned_at}`. Il `prefix`
    sta un livello sotto, non in cima. Accetta comunque la forma piatta, che
    e' quella dell'arch 1.x e delle fixture sintetiche.
    """
    nested = item.get("domain")
    return nested if isinstance(nested, dict) else item


def domain_prefix_of(domains_body: Any) -> str | None:
    """Il `domain_prefix` del primo dominio dell'utente."""
    for item in find_list_of_objects(domains_body):
        normalized = {_normalize_key(k): v for k, v in _domain_object(item).items()}
        for key in ("prefix", "domainprefix"):
            value = normalized.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def render_findings_markdown(findings: list[dict[str, Any]], host: str) -> str:
    """Il blocco da incollare in docs/M-01 e nel commento su RT-2938."""
    lines = [
        "# M-01 - esiti della ricognizione",
        "",
        f"Host: `{host}` - generato da `scripts/probe_arch2.py` il "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
    ]
    for item in findings:
        lines += [
            f"## {item['id']} - {item['question']}",
            "",
            f"**Esito:** {item['outcome']}",
            "",
            "<details><summary>Evidenza</summary>",
            "",
            "```json",
            json.dumps(item["evidence"], indent=2, ensure_ascii=False)[:8000],
            "```",
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


def write_findings(config: Config, findings: list[dict[str, Any]]) -> None:
    """Scrive gli esiti in JSON e in markdown accanto alle fixture."""
    (config.out_dir / "_findings.json").write_text(
        json.dumps(findings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (config.out_dir / "_findings.md").write_text(
        render_findings_markdown(findings, config.host), encoding="utf-8"
    )


def print_summary(findings: list[dict[str, Any]], config: Config) -> None:
    """Il riassunto a schermo: una riga per verifica."""
    print("\n" + "=" * 72)
    print("ESITI")
    print("=" * 72)
    for item in findings:
        print(f"  {item['id']:<5} {item['outcome']}")
    print("=" * 72)
    print(f"Fixture e report in {config.out_dir}")
    print("Da riportare come commento su RT-2938: _findings.md")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Gli argomenti di questo script."""
    parser = argparse.ArgumentParser(
        description=(
            "Ricognizione dell'API Radoff arch 2.0 su dev, con salvataggio "
            "di fixture reali redatte (card M-01)."
        )
    )
    parser.add_argument(
        "--env-file",
        help="File KEY=value da cui leggere le credenziali (es. .env.dev).",
    )
    parser.add_argument("--host", help=f"Base URL (default {DEFAULT_HOST}).")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help="Cartella di output delle fixture.",
    )
    parser.add_argument(
        "--domain-prefix",
        help="Forza il domain_prefix invece di ricavarlo dalla discovery.",
    )
    parser.add_argument(
        "--small-page-size",
        type=int,
        default=2,
        help="page_size usato per forzare la paginazione (default 2).",
    )
    parser.add_argument(
        "--full-page-size",
        type=int,
        default=200,
        help="page_size della passata piena, come il client (default 200).",
    )
    parser.add_argument(
        "--username",
        help="Utente da usare, invece di RADOFF_DEV_USERNAME.",
    )
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help=(
            "Chiede la password in modo interattivo invece di leggerla da "
            "RADOFF_DEV_PASSWORD: non finisce nella history della shell."
        ),
    )
    parser.add_argument(
        "--auth-flow",
        choices=("srp", "password", "auto"),
        default="srp",
        help=(
            "Flusso Cognito. `srp` (default) e' quello dell'integrazione. "
            "`password` usa USER_PASSWORD_AUTH, che manda la password in "
            "chiaro a Cognito: SOLO come diagnostica, e solo con un account "
            "di prova su dev. `auto` prova SRP e ripiega su password se "
            "l'app client non ha SRP abilitato."
        ),
    )
    parser.add_argument(
        "--redact-domain-prefix",
        action="store_true",
        help=(
            "Sostituisce anche il domain_prefix nelle fixture. Di default "
            "resta in chiaro: e' human-readable, il client lo mostra "
            "all'utente, e le fixture servono a testare quel percorso."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Esegue l'intera ricognizione. Restituisce il codice di uscita."""
    config = build_config(parse_args(argv))
    redactor = Redactor(
        extra_literals=(config.username, config.password),
        redact_domain_prefix=not config.keep_domain_prefix,
    )
    recorder = Recorder(config, redactor)

    print(f"Host: {config.host}")
    print(f"Output: {config.out_dir}")
    print(f"Pool provati: {', '.join(p.label for p in config.pools)}")
    if len(config.pools) == 1:
        print(
            "  NOTA: RADOFF_DEV_POOL_ID/RADOFF_DEV_CLIENT_ID non impostati - "
            "D-24 resta parziale (nessun confronto con un pool dev)."
        )

    # I flussi si sondano PRIMA di tentare il login: costano una chiamata a
    # vuoto e dicono in anticipo se il flusso richiesto e' abilitato sull'app
    # client che stiamo per usare. Farlo dopo significava diagnosticare un
    # fallimento gia' avvenuto, con l'informazione utile stampata sotto
    # l'errore che avrebbe evitato.
    auth_flows = probe_auth_flows(config)
    _warn_if_flow_unavailable(config, auth_flows)
    attempts = attempt_pools(config, recorder, redactor)
    working = pick_working_token(attempts)
    logged_in = next((a for a in attempts if a.token), None)

    if logged_in is None:
        findings = analyze(recorder.calls, attempts, None, auth_flows)
        recorder.write_manifest(
            {"pools": [a.to_dict() for a in attempts], "auth_flows": auth_flows}
        )
        write_findings(config, findings)
        print(
            "\nNessun login SRP e' riuscito: la ricognizione non e' partita.\n"
            "ATTENZIONE: questo NON e' l'esito di D-24 - l'host v2 non e' mai "
            "stato interrogato.\nIl problema e' fra credenziali e pool "
            "Cognito, non fra token e API 2.0.\n"
            "Vedi il codice Cognito stampato sopra e la sezione "
            "'Diagnosi del login' in docs/M-01-ricognizione-dev.md."
        )
        return 4

    # Un token c'e'. Che la discovery lo abbia rifiutato NON e' una ragione
    # per fermarsi: `/auth/*`, `/analytics/*` e `/data/*` sono tre base path
    # mapping distinti sullo stesso host (T-02 D-01), e possono benissimo
    # avere autorizzazioni diverse. Sapere QUALI accettano il token e quali
    # no e' informazione che vale quanto la ricognizione stessa.
    token = logged_in.token
    assert token is not None  # noqa: S101 - garantito da `logged_in`
    auth_style = logged_in.working_auth_style or "bearer"
    if working is not None:
        print(f"\n[auth] token accettato dall'host v2: {working.label}")
        domains_body = _body_of(recorder.calls, f"auth_domains__{working.label}")
        raw_domains = _raw_body_of(recorder.calls, f"auth_domains__{working.label}")
    else:
        print(
            "\n[auth] la discovery ha rifiutato il token: proseguo comunque "
            "sugli altri base path,\n       per stabilire se il rifiuto "
            "riguarda tutto l'host o solo /auth/*."
        )
        domains_body = None
        raw_domains = None

    domain_prefix = config.forced_domain_prefix
    if not domain_prefix and raw_domains is not None:
        domain_prefix = pick_domain_with_devices(
            recorder, token, raw_domains, auth_style
        ) or domain_prefix_of(raw_domains)
    if domain_prefix:
        print(f"[auth] domain_prefix in uso: {domain_prefix}")
    else:
        print(
            "[auth] nessun domain_prefix disponibile: le chiamate a "
            "/data/devices vengono saltate.\n"
            "       Passa --domain-prefix <valore> per eseguirle comunque."
        )

    if working is None:
        probe_discovery_paths(recorder, token, auth_style)
        probe_route_existence(recorder)

    probe_measures_ranges(recorder, token, auth_style)

    serials: list[str] = []
    if domain_prefix:
        probe_devices(config, recorder, token, domain_prefix, auth_style)
        serials = serials_of(_body_of(recorder.calls, "devices__full")) or serials_of(
            _body_of(recorder.calls, "devices__page1_small")
        )
        if serials:
            probe_device_detail(recorder, token, serials[0], auth_style)
        else:
            print("\n[device] nessun device nella lista: dettaglio non eseguito.")
        probe_errors(config, recorder, token, auth_style)

    findings = analyze(recorder.calls, attempts, domains_body, auth_flows)
    recorder.write_manifest(
        {
            "pools": [a.to_dict() for a in attempts],
            "auth_flows": auth_flows,
            "accepted_pool": working.label if working else None,
            "domain_prefix_redacted": not config.keep_domain_prefix,
            "device_count": len(serials),
        }
    )
    write_findings(config, findings)
    print_summary(findings, config)
    return 0 if working is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
