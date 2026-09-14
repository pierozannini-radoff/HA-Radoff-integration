# Fixture reali — API arch 2.0 su dev

Response reali dell'API arch 2.0, catturate su
`https://v2.api.dev.iot.radoff.life` da
[`scripts/probe_arch2.py`](../../../scripts/probe_arch2.py) e redatte prima di
essere scritte su disco. Sono distinte dalle fixture sintetiche che stanno un
livello sopra (`tests/fixtures/*.json`), scritte a mano.

## Come si rigenerano

```bash
export RADOFF_DEV_USERNAME='...'          # le TUE credenziali su dev
export RADOFF_DEV_PASSWORD='...'
export RADOFF_DEV_POOL_ID='eu-west-1_XXXXXXXX'   # facoltativi
export RADOFF_DEV_CLIENT_ID='...'

python3 scripts/probe_arch2.py
```

Lo script non contiene credenziali e non ne scrive: chiunque nel team lo esegue
con le proprie. Vedi il suo docstring per le opzioni (`--env-file`, `--host`,
`--domain-prefix`, `--redact-domain-prefix`).

## Cosa c'è dentro

| File | Contenuto |
|---|---|
| `auth_domains__pool_*.json` | `GET /data/user/me/domains` (la discovery: sotto `/data/*`, non `/auth/*`), una per pool Cognito provato |
| `measures_ranges__<tipo>.json` | `GET /analytics/measures-ranges?device_type=<tipo>`, uno per tipo di device |
| `measures_ranges__all.json` | lo stesso endpoint **senza** `device_type`: unisce tutti i tipi, ed è da qui che esce l'enumerazione completa delle `unit` |
| `devices__page1_small.json`, `devices__page2_small.json`, `devices__page1_small_repeat.json` | `GET /data/devices` paginata con `page_size` piccolo, con la prima pagina richiesta due volte per misurare la stabilità dell'ordinamento |
| `devices__full.json` | la stessa lista con `page_size=200`, come la chiama il client |
| `devices__probe_*.json` | sonde su `sort` / `order_by` / `order`, per stabilire se filtri e ordinamenti esistono |
| `device_detail.json` | `GET /data/devices/{serial}` su un device reale |
| `error__*.json` | errori provocati deliberatamente, per la tassonomia degli errori |
| `_manifest.json` | una riga per chiamata: URL, parametri, status, durata e header di risposta completi |
| `_findings.json` | gli esiti delle verifiche, calcolati dalle response e non scritti a mano |

Gli header stanno nel manifest, non nei file di body, così ogni `*.json` resta
caricabile direttamente come payload da un test
(`tests/conftest.py::load_dev_fixture`).

## Politica di redazione

Nessun body grezzo tocca il disco: la redazione avviene in memoria, prima della
scrittura, sulla stessa politica di
[`custom_components/radoff/diagnostics.py`](../../../custom_components/radoff/diagnostics.py).
Quattro regole, tutte deliberate:

1. **Si reda per valore, non per chiave.** Ogni UUID, mail o JWT è sostituito
   ovunque compaia, anche dentro un messaggio d'errore. I segnaposto sono
   stabili nell'ambito di una esecuzione — `<uuid-1>` è sempre lo stesso
   dominio — così i riferimenti incrociati fra le fixture restano verificabili.
2. **I serial restano in chiaro**, perché sono l'identificativo del device e
   redigerli svuoterebbe le fixture. Per la stessa ragione `device_id` e
   `serial` non sono fra le chiavi redatte, benché lo siano in
   `diagnostics.TO_REDACT`.
3. **Le etichette scritte da persone diventano `<label-N>`.** Il `name` di un
   dominio o di un device contiene nomi propri; il campo resta, il contenuto
   no. Vale anche per `room_name` e `building_name`. La regola è contestuale:
   `name` è sostituito solo dentro un oggetto che ha anche un `prefix` o un
   `serial_number`, quindi `role.name` e le etichette di `measures-ranges`
   restano intatte.
4. **Le coordinate sono arrotondate a un decimale** (~11 km), non cancellate.
   Le chiavi di indirizzo (`address`, `street`, `zip`, …) sono invece
   sostituite per intero.

Il `domain_prefix` resta in chiaro di default: il client lo mostra all'utente
nel menu di scelta del dominio, e le fixture servono a testare quel percorso.
Chi non lo vuole nel repo esegue lo script con `--redact-domain-prefix`.

`tests/test_dev_fixtures.py` verifica in CI che qui dentro non sia rimasto un
JWT, una mail, un UUID o una coordinata a piena precisione.
