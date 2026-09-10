# Fixture reali — API arch 2.0 su dev

Response **reali** dell'API arch 2.0, catturate su
`https://v2.api.dev.iot.radoff.life` da
[`scripts/probe_arch2.py`](../../../scripts/probe_arch2.py) (card **M-01**) e
redatte prima di essere scritte su disco.

Sono distinte dalle fixture sintetiche che stanno un livello sopra
(`tests/fixtures/*.json`), scritte a mano contro l'arch 1.x. Esistono per una
ragione precisa: senza payload reali ogni fase della migrazione si scriverebbe
su forme immaginate, ed è esattamente così che sono nati i difetti che stiamo
correggendo — il fattore `0.00835`, le soglie hardcodate, l'AQI nel bucket
sbagliato.

## Come si rigenerano

```bash
export RADOFF_DEV_USERNAME='...'          # le TUE credenziali su dev
export RADOFF_DEV_PASSWORD='...'
# facoltativi, per il confronto fra pool Cognito di T-02 D-24:
export RADOFF_DEV_POOL_ID='eu-west-1_XXXXXXXX'
export RADOFF_DEV_CLIENT_ID='...'

python3 scripts/probe_arch2.py
```

Lo script non contiene credenziali e non ne scrive: chiunque nel team lo esegue
con le proprie. Vedi il suo docstring per le opzioni (`--env-file`, `--host`,
`--domain-prefix`, `--redact-domain-prefix`).

## Cosa c'è dentro

| File | Contenuto |
|---|---|
| `auth_domains__pool_*.json` | `GET /auth/user/me/domains`, una per pool Cognito provato |
| `measures_ranges__<tipo>.json` | `GET /analytics/measures-ranges?device_type=<tipo>`, per i sei tipi di D-13 |
| `measures_ranges__all.json` | lo stesso endpoint **senza** `device_type`: unisce tutti i tipi, ed è da qui che esce l'enumerazione completa delle `unit` |
| `devices__page1_small.json`, `devices__page2_small.json`, `devices__page1_small_repeat.json` | `GET /data/devices` paginata con `page_size` piccolo, con la prima pagina richiesta due volte per misurare la stabilità dell'ordinamento |
| `devices__full.json` | la stessa lista con `page_size=200`, come la chiamerà il client |
| `devices__probe_*.json` | sonde su `sort` / `order_by` / `order`, per stabilire se filtri e ordinamenti esistono |
| `device_detail.json` | `GET /data/devices/{serial}` su un device reale |
| `error__*.json` | errori provocati deliberatamente, per la tassonomia di M-02 |
| `_manifest.json` | una riga per chiamata: URL, parametri, status, durata e **header di risposta completi** |
| `_findings.json`, `_findings.md` | gli esiti delle verifiche, calcolati dalle response e non scritti a mano |

Gli header stanno nel manifest, non nei file di body, così ogni `*.json` resta
caricabile direttamente come payload da un test
(`tests/conftest.py::load_dev_fixture`).

## Politica di redazione

Nessun body grezzo tocca il disco: la redazione avviene in memoria, prima
della scrittura. La politica segue quella di
[`custom_components/radoff/diagnostics.py`](../../../custom_components/radoff/diagnostics.py)
(card S-17), con tre scelte deliberate che vale la pena esplicitare.

**1. La redazione è basata sul valore, non solo sulla chiave.** Qualunque
UUID, indirizzo mail o JWT viene sostituito ovunque compaia — anche dentro un
messaggio d'errore, dove nessuna regola per chiave lo avrebbe trovato. I
segnaposto sono **stabili nell'ambito di una esecuzione**: `<uuid-1>` è sempre
lo stesso dominio in tutte le fixture, quindi i riferimenti incrociati fra
`/auth/user/me/domains` e `/data/devices` restano verificabili senza che il
valore vero esista da nessuna parte nel repo.

**2. I serial restano in chiaro.** Lo dice la card, e in arch 2.0
`deviceId` == `serial_number` == `deviceSerial` (T-02 D-02): redigerli
svuoterebbe le fixture del loro contenuto. Per la stessa ragione `device_id` e
`serial` **non** sono nell'insieme delle chiavi redatte, benché lo siano in
`diagnostics.TO_REDACT` — lì erano UUID di arch 1.x, qui sono il serial.

**3. Le etichette scritte da persone vengono sostituite.** Il campo `name` di
un dominio, su dev, contiene nomi e cognomi di persone reali; quello di un
device contiene il nome che gli ha dato il proprietario. Sono i "riferimenti
sensibili di persone" che non devono entrare nel repo, quindi diventano
`<label-N>` — il campo resta, così i test possono esercitare il percorso che lo
mostra all'utente, ma il contenuto no. Vale anche per `room_name` e
`building_name`.

La regola è **contestuale, non per nome di chiave**: `name` viene sostituito
solo se l'oggetto che lo contiene ha anche un `prefix` o un `serial_number`,
cioè è un dominio o un device. In `role.name` ("Super Admin") e nelle etichette
di `measures-ranges` ("PM10") lo stesso campo non ha niente di personale e resta
intatto — redigerlo avrebbe svuotato proprio le fixture dello schema.

**4. Le coordinate sono arrotondate, non cancellate.** Un decimale è circa
11 km: inutilizzabile per localizzare qualcuno, sufficiente perché la fixture
conservi tipo, segno e ordine di grandezza del campo. Le chiavi di indirizzo
(`address`, `street`, `zip`, …) sono invece sostituite per intero.

Il `domain_prefix` resta **in chiaro** di default: è human-readable
(`radoff-hq`), il client lo mostra all'utente nel menu di scelta del dominio, e
le fixture servono proprio a testare quel percorso. Chi non vuole il nome del
proprio dominio nel repo esegue lo script con `--redact-domain-prefix`.

`tests/test_dev_fixtures.py` verifica in CI, a ogni commit, che qui dentro non
sia rimasto un JWT, una mail, un UUID o una coordinata a piena precisione: la
redazione gira una volta sola sulla macchina di chi esegue la ricognizione, il
risultato resta nel repo per sempre.
