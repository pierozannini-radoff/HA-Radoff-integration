# M-06 (RT-2945) — decisioni, scostamenti e verifica dal vivo

Card: **[M-06] Disponibilità delle entità basata su `connection_status`**.
Fase 6 di 8 della migrazione ad arch 2.0.
Riferimenti: T-02 (RT-2810), T-08 (RT-2938) e S-07 (RT-2813), di cui questa
card sostituisce l'euristica. Dipende da M-03 (RT-2941), parallelizzabile
con M-05 (RT-2944).

Questo documento tiene le due cose che la card da sola non tiene: **le
decisioni prese durante l'implementazione, e da chi**, e **come si verifica
dal vivo** ciò che una suite mockata non può verificare.

## Decisioni prese in implementazione

Le quattro decisioni sotto sono state poste prima di scrivere una riga, il
2026-09-10, e decise con Piero. Tre su quattro riguardano cosa *non* fare.

### 1. Un device connesso e muto mostra l'ultimo valore noto, non `unknown`

È la domanda che M-04 ha reso viva: prima, un device con `telemetry: null`
non aveva entità affatto, quindi non c'era niente da mostrare. Ora le
entità nascono dallo schema del tipo e ci sono comunque.

**Decisione: l'ultimo valore noto, ripristinato anche dopo un riavvio**
(`RestoreSensor`). Lo storico resta continuo e un'automazione che legge lo
stato continua a leggere un numero attraverso il buco.

**Il costo, dichiarato.** Reintroduce una cache che S-07 aveva tolto (per
le finding C1/C2) e permette di mostrare come corrente un valore vecchio di
giorni. Tre cose lo contengono, e sono scritte nei docstring, non qui:

- la cache è **un valore e il suo timestamp**, mai un oggetto `Device`:
  nessun percorso legge identità o disponibilità da lì, che è ciò che
  rendeva pericolosa la cache di S-06;
- `last_measured_at` porta **sempre** il timestamp del valore mostrato, e
  non ne presta mai uno più fresco preso da un altro campo dello stesso
  device — per questo è caduto anche il fallback su `telemetry_timestamp`
  che S-07 aveva in `_freshness_reference`;
- un'entità che non ha mai avuto un valore resta `unknown`: niente viene
  inventato.

**Ciò che resta scoperto, e va detto:** `last_measured_at` è un attributo,
quindi un'automazione a soglia non lo vede. Un utente che guarda la
dashboard vede un numero attuale. L'alternativa scartata era `unknown` a
ogni buco, che rende il problema visibile ma spezza storico e automazioni
su ogni device che sta zitto più di sei ore — cioè la maggioranza dei 120
device censiti da M-01.

### 2. La rete di sicurezza a 6 ore segnala, non decide

`CONNECTION_STATUS_STALE_WINDOW = 6h` è la finestra **dichiarata dal
backend** (T-02 D-16), non un multiplo dell'intervallo di polling. Se un
device dice `connected` ma il suo `connection_status_updated_at` è più
vecchio, escono un WARNING (uno per episodio, non per ciclo) e l'attributo
`connection_status_stale`.

**Le entità restano disponibili.** La cadenza con cui il backend aggiorna
quel campo è esattamente il residuo aperto di D-17 (b): farne una soglia di
disponibilità rimetterebbe un numero non verificato al posto di quello
appena rimosso. Alternativa scartata: oltre la finestra → non disponibili.

### 3. Il ripiego per il radon è documentato, non implementato

Con le cadenze note (1 msg/min, radon ≥ 5 min) si potrebbe *stimare* la
staleness per campo. Non si fa: sarebbe un'euristica nostra, sbaglierebbe
ai limiti, e nessun AC di questa card ne ha bisogno una volta che la
disponibilità non dipende più dall'età del dato. Il limite è scritto per
esteso in `api/models.py::Reading.measured_at` e la richiesta resta T-08
D-15, aggiornata di conseguenza.

### 4. `status` entra nel modello come diagnostico

Il campo amministrativo (`active`) non era nemmeno letto dal client. Ora è
nel modello, negli attributi di ogni entità e nel dump di diagnostica — e
non decide nulla. Fino a D-17 (c) usare uno dei due campi come proxy
dell'altro sarebbe una supposizione.

## Scostamenti dalla card

- **`_freshness_reference` è stato rinominato in `measured_at`** e ha perso
  il fallback su `telemetry_timestamp`. La card chiede di aggiornare i
  docstring di `available`; il fallback è caduto perché dopo M-04 un'entità
  esiste anche per una misura che quel poll non ha portato, e prestarle il
  timestamp di un altro campo avrebbe fatto sembrare fresco proprio il
  valore ricordato.
- **Il WARNING sul valore inatteso non è in `available`** ma in
  `api/client.py::_build_device`, dove la stringa entra nel modello.
  `available` è letto a ogni lettura di stato di ogni entità: dieci entità
  avrebbero prodotto dieci righe per ciclo. È deduplicato per *valore*, non
  per device.
- **`POST /data/devices/status`** (T-08 D-35) resta fuori scopo: il
  contratto non è arrivato.

## Rilanciare tutte le verifiche

```bash
./scripts/verify_m06            # lint, suite, AC uno per riga, tipi, passata su dev
./scripts/verify_m06 --offline  # tutto tranne la passata su dev
```

Non si ferma al primo errore: li raccoglie e li elenca alla fine. Le
credenziali vengono dal proprio `.env`; gli override di pool sono dentro lo
script e si cambiano con `M06_POOL_ID` / `M06_CLIENT_ID` /
`M06_DOMAIN_PREFIX`.

Il passo sui tipi non confronta con zero: `mypy` su questo repo esce 1 da
prima di questa card (21 errori in `config_flow.py`, `repairs.py`,
`coordinator.py` e negli stub di `requests`). Il criterio è che il conto
non salga e che nessun errore venga dai quattro file che M-06 riscrive.

### Esito della passata offline

Eseguita il **2026-09-11** sul branch
`feature/RT-2945-availability-connection-status`:

```
✓ Formattazione (nessuna riscrittura)
✓ Lint
✓ Suite completa            366 passed
✓ AC di M-06, uno per riga   16 passed
✓ test_sensor.py e test_init.py interi   52 passed
✓ Tipi: nessun errore nuovo  21 su baseline 21
```

Copertura 91%. Ogni AC della card ha il suo test e il commento in testa al
passo 3 di `scripts/verify_m06` dice quale:

| AC | Test |
|---|---|
| `connected` + `telemetry: null` non rende non disponibili | `test_a_connected_device_without_telemetry_keeps_its_entities_available` |
| Non connesso → non disponibile, anche con telemetria | `test_availability_follows_connection_status[disconnected-False]` |
| Valore mai visto → disponibile + WARNING | `test_an_unknown_connection_status_warns_once` |
| 429 o ciclo fallito ≠ device offline | `test_a_429_skips_the_cycle_without_making_entities_unavailable`, `test_a_500_costs_the_whole_cycle` |
| Il moltiplicatore non esiste più nel codice | `test_the_staleness_multiplier_is_gone_from_the_codebase` |
| Gli attributi espongono entrambi i timestamp | `test_the_diagnostic_attributes_carry_both_timestamps` |

## La verifica dal vivo

```bash
# credenziali nel proprio .env (gitignored), come RADOFF_USERNAME/RADOFF_PASSWORD.
# I due override di pool servono perché const.py punta ancora all'altro pool:
# vedi «Un residuo che blocca il collaudo end-to-end» in docs/M-04-verifica-dev.md.
python3 scripts/verify_m06_live.py \
    --pool-id eu-west-1_5SsvW9t6S \
    --client-id 2i63gbc9sim3b8paasaga7jb6g \
    --domain-prefix 875fe89b
```

### Cosa verifica, e perché serve una passata vera

La suite mockata verifica cosa fa il codice per ogni valore di
`connection_status`, e lo fa meglio di qualunque passata dal vivo: può
provocare tutti e quattro i casi a comando. Ciò che non può dire è quali
valori l'API serva **davvero**, e con che età.

| Controllo | Perché non basta un test mockato |
|---|---|
| Ogni `connection_status` servito da dev è nell'enumerazione del client | se ne comparisse un terzo, ogni device che lo porta sarebbe già "non determinabile" — disponibile per la ragione sbagliata (T-08 D-17) |
| `status` e `connection_status` coesistono e divergono | la card vieta di confonderli: questo misura se la distinzione è osservabile o solo teorica |
| `57FA28` è connesso e senza telemetria | è il caso riproducibile che la card indica per nome: se fosse `disconnected`, il primo AC non avrebbe soggetto dal vivo |
| Quanti device guadagnano o perdono entità disponibili | l'effetto della card sul dominio vero, in device |
| L'età di `connection_status_updated_at` sui device connessi | se fosse sistematicamente oltre le 6 h, la rete di sicurezza sarebbe rumore — ed è metà del residuo di D-17 (b) |
| Un solo timestamp per blocco di telemetria | il giorno che D-15 venisse implementata, il ripiego documentato per il radon andrebbe riletto |

### Cosa non verifica, e perché

- **Il valore inatteso di `connection_status`.** Lo scrive il backend, non
  è provocabile. Se la passata ne trovasse uno sarebbe un FAIL che chiede
  di aggiornare l'enumerazione, non un test.
- **Il ripristino dopo un riavvio.** Riguarda il registro di stato di Home
  Assistant, non l'API: è verificato in `tests/test_sensor.py` e a mano con
  `./scripts/develop`.
- **Il 429.** Come per M-05: saturare una quota condivisa con l'app mobile
  per vedere un errore degraderebbe dev per chiunque altro.

### Esito: non eseguita, l'autenticazione su dev non passa più

Tentata il **2026-09-11**. Non è arrivata a fare una sola chiamata di
dominio, e **il problema non è di questa card**: lo stesso script di M-05,
verde il 2026-09-10 con gli stessi parametri, oggi fallisce identicamente.

| Pool usato | Esito |
|---|---|
| `eu-west-1_5SsvW9t6S` (dev, l'override di M-04/M-05) | `NotAuthorizedException: Incorrect username or password` |
| `eu-west-1_zD4CSIZ6i` (**prod**, il default di `const.py`) | handshake SRP **riuscito**, poi 401 su `/data/devices` |

**La causa, isolata sondando lo stesso utente sui due pool:** le
credenziali in `.env` sono quelle di un'utenza **prod**, non dev. I due
pool hanno utenze separate — `eu-west-1_zD4CSIZ6i` è il pool della
versione rilasciata (prod), `eu-west-1_5SsvW9t6S` è dev — e l'account in
`.env` esiste sul primo e non sul secondo. La seconda riga della tabella è
il residuo già noto e documentato in `docs/M-04-verifica-dev.md` («Un
residuo che blocca il collaudo end-to-end»): un token prod contro l'API di
dev prende 401, perché dev accetta solo `pool_dev`
(`accepted_pool` in `tests/fixtures/dev/_manifest.json`).

I due errori si assomigliano e portano a conclusioni opposte —
credenziali sbagliate contro pool sbagliato — quindi
`verify_m06_live.py::auth_hint` ora li distingue e stampa quale dei due è,
invece di riportare l'eccezione e basta.

**Cosa resta da fare**, con credenziali valide per il pool dev:

```bash
python3 scripts/verify_m06_live.py \
    --pool-id eu-west-1_5SsvW9t6S \
    --client-id 2i63gbc9sim3b8paasaga7jb6g \
    --domain-prefix 875fe89b
```

Serve un'utenza valida sul **pool dev**: quella con cui hanno chiuso
M-03, M-04 e M-05, non l'utenza prod attualmente in `.env`.

Il controllo che pesa di più è il primo della tabella qui sopra: finché
non è passato, l'enumerazione `connected` / `disconnected` resta quella
osservata da M-01 il 2026-09-09 e non riconfermata oggi. Il codice non si
rompe su un valore nuovo — è il motivo per cui esiste il terzo stato — ma
un terzo valore comparso nel frattempo lascerebbe disponibili le entità di
device che potrebbero essere offline, e lo sapremmo solo dal WARNING nel
log di un utente.
