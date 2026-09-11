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
La seconda è stata **riaperta e ribaltata il 2026-09-11** da ciò che la
passata dal vivo ha misurato: è scritta sotto nella sua forma finale, con
la versione precedente e il motivo della retromarcia.

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

### 2. Nessuna rete di sicurezza sull'età di `connection_status` — rimossa

**Decisione finale (2026-09-11, con Piero): la rete non esiste.**
`connection_status_updated_at` resta un attributo grezzo e nient'altro;
non c'è costante, non c'è WARNING, non c'è attributo derivato.

**Cosa era stato scritto prima.** Una costante
`CONNECTION_STATUS_STALE_WINDOW = 6h` — la finestra dichiarata dal backend
in T-02 D-16, non un multiplo dell'intervallo di polling — confrontata con
`connection_status_updated_at` sui device che dicono `connected`: oltre la
finestra, un WARNING (uno per episodio) e un attributo
`connection_status_stale`, mai un verdetto di disponibilità. L'intento era
rendere visibile il caso in cui il campo su cui ora poggia tutta la
disponibilità smette di essere aggiornato.

**Perché è stata rimossa.** Presupponeva che
`connection_status_updated_at` fosse il momento dell'ultimo *controllo*.
La passata dal vivo l'ha smentito, e questa è l'evidenza:

| device | telemetria | `connection_status_updated_at` | `connection_status` |
|---|---|---|---|
| `3D90E0` (nowplus) | 58 secondi fa | ~2 giorni fa | `connected` |
| `57FA28` (sense) | mai | 64 giorni fa | `disconnected` |

Un device che trasmette *adesso* e porta quel campo a due giorni prima non
lascia alternative: è il momento dell'ultimo **cambio** di stato. È la
risposta empirica a T-08 D-17 (e), che era aperta.

Con quella semantica la rete era l'esatto contrario del suo intento: un
WARNING e un attributo su **ogni device sano connesso da più di sei ore**,
cioè rumore permanente. E non si aggiusta con una soglia diversa — se il
campo è "ultimo cambio", nessuna età distingue un device connesso e
stabile da un campo congelato. Restava solo la scelta fra un controllo che
sbaglia sempre e nessun controllo: nessun controllo.

**Cosa resta scoperto, e va detto.** Il caso che la rete voleva coprire —
il campo si congela, le entità restano disponibili sulla fede di un valore
che non viene più aggiornato — non è coperto da niente. Non è coprilbile
con i dati che l'API dà oggi: servirebbe sapere con quale cadenza il
backend riscrive quel campo, che è la metà ancora aperta di D-17 (b).
Quando arriverà quella risposta, questo è il punto da riaprire.

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
✓ Suite completa            365 passed
✓ AC di M-06, uno per riga   15 passed
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
| Quale dei due AC esemplifica `57FA28` | è il caso che la card indica per nome, e la card lo dà per connesso e muto: dal vivo è `disconnected`, quindi illustra il secondo AC. Riporta quale, non lo pretende |
| Il primo AC ha un soggetto sul dominio | «connesso e muto resta disponibile» si osserva solo se un device in quello stato esiste. Se non esiste è uno `skip`, non un FAIL: il caso è provocato a comando dalla suite mockata |
| Quanti device guadagnano o perdono entità disponibili | l'effetto della card sul dominio vero, in device |
| Cosa misura `connection_status_updated_at` | il confronto fra l'età della telemetria e quella dello stato su uno stesso device connesso: è ciò che ha risposto a D-17 (e), e ciò che se ne accorgerebbe se il campo cambiasse semantica |
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

### Esito: 8 PASS, 0 FAIL, 1 skip

Eseguita il **2026-09-11** sul dominio `875fe89b` di dev, 2 device:

```
[  ok  ] Ogni connection_status servito da dev e' nell'enumerazione del client
         2 device - connected: 1, disconnected: 1
[  ok  ] connection_status e' presente nel payload
[  ok  ] status e connection_status sono entrambi presenti su ogni device
[  ok  ] I due campi divergono davvero (status active, non connesso)
         1 device su 2: 57FA28
[  ok  ] Il caso riproducibile della card (57FA28) esemplifica AC2
[ skip ] AC1 dal vivo: un device connesso e muto mantiene le entita' disponibili
[  ok  ] Effetto della card sul dominio, in device
[  ok  ] Cosa misura connection_status_updated_at (D-17 (e))
         3D90E0: telemetria 0:00:58 fa, connection_status 1 day, 23:55:00 fa
[  ok  ] Un solo timestamp per blocco di telemetria (D-15 ancora aperta)
```

Tre cose che questa passata ha stabilito e che la card non sapeva:

**1. `connection_status_updated_at` è l'ultimo cambio di stato.** La
risposta a T-08 D-17 (e), con l'evidenza riportata nella decisione 2 qui
sopra. È il motivo per cui la rete di sicurezza a 6 ore è stata rimossa
dentro questa card invece di essere consegnata.

**2. L'enumerazione tiene.** `connected` e `disconnected`, nessun terzo
valore, `connection_status` presente su tutti i device: il controllo che
pesava di più è passato. L'enumerazione completa resta comunque la
richiesta aperta T-08 D-17 — due device non sono un censimento.

**3. Il primo AC non ha soggetto su questo dominio.** La nota in coda alla
card indica `57FA28` come «un sense con `telemetry: null`» e lo presenta
come soggetto del primo AC. Dal vivo quel device è `disconnected` da 64
giorni, quindi esemplifica il **secondo**: con M-06 le sue 10 entità
restano `unavailable` — per la ragione giusta (è disconnesso) invece che
per quella sbagliata (non ha letture). Su `875fe89b` non c'è nessun device
connesso e muto, quindi «connesso e muto resta disponibile» non è
osservabile dal vivo adesso. Non è un fallimento del criterio: è un caso
che la suite mockata provoca a comando e che lo script ora segna `skip`
invece di FAIL, perché un FAIL lì direbbe soltanto che il mondo si è
mosso. **La nota di M-04 in coda alla card è superata dai dati.**

### Il pool giusto, perché il primo tentativo era fallito

Il tentativo del mattino del 2026-09-11 non era arrivato a fare una sola
chiamata di dominio, e la causa non era di questa card: in `.env` c'erano
le credenziali di un'utenza **prod**. I due pool hanno utenze separate —
`eu-west-1_zD4CSIZ6i` è prod (il default di `const.py`),
`eu-west-1_5SsvW9t6S` è dev — e i due fallimenti si assomigliano nel log
portando a conclusioni opposte:

| Pool usato | Esito con un'utenza prod |
|---|---|
| `eu-west-1_5SsvW9t6S` (dev) | `NotAuthorizedException: Incorrect username or password` |
| `eu-west-1_zD4CSIZ6i` (prod) | handshake SRP riuscito, poi 401 su `/data/devices` |

La seconda riga è il residuo già documentato in
`docs/M-04-verifica-dev.md` («Un residuo che blocca il collaudo
end-to-end»): un token prod contro l'API di dev prende 401, perché dev
accetta solo `pool_dev` (`accepted_pool` in
`tests/fixtures/dev/_manifest.json`). Per questo
`verify_m06_live.py::auth_hint` ora stampa quale dei due fallimenti è,
invece di riportare l'eccezione e basta. La passata verde qui sopra è
stata fatta con un'utenza valida sul pool dev.

## I controlli a occhio: cosa si è visto, e cosa non ha soggetto

Fatto il **2026-09-11** con `./scripts/develop` sul dominio `875fe89b`,
allineando temporaneamente i pool di `const.py` a quelli dev (modifica
locale, non committata — vedi «Due difetti preesistenti» sotto).

**AC2 dal vivo.** `57FA28`, disconnesso da 64 giorni, ha le sue 10 entità
`unavailable`: la card ha l'effetto dichiarato, e per la ragione giusta.
`3D90E0`, connesso, mostra le sue 9 misure con i valori del payload.
Nessuno scarto fra schema e payload in nessuna delle due direzioni,
pressione in Pascal (D-06), niente radon (il `nowplus` non ha l'hardware),
`aqi_value` disabilitata di default per il difetto di D-08.

**AC6 dal vivo.** Gli attributi dell'entità, da Strumenti per sviluppatori
→ Stati:

```
last_measured_at:             2026-09-11T15:42:06+00:00
connection_status:            connected
connection_status_updated_at: 2026-09-09T10:06:41+00:00
status:                       active
```

Tutti e quattro, nessun `connection_status_stale`. **È la decisione 2 in
una schermata:** valore di pochi minuti fa, stato di due giorni fa, device
perfettamente funzionante. Con la rete a 6 ore questo device avrebbe
portato l'attributo e un WARNING nel log.

**I due controlli che la card chiede non sono eseguibili su dev**, e non
per un difetto: manca il soggetto. Servirebbe un device connesso e muto —
`57FA28` è disconnesso, `3D90E0` trasmette regolarmente, e staccarlo non è
possibile. Entrambi i casi sono coperti dalla suite mockata, che li
**provoca a comando**:

1. «Connesso e muto mostra l'ultimo valore noto» →
   `test_a_connected_device_without_telemetry_keeps_its_entities_available`.
2. «Dopo un riavvio torna l'ultimo valore con `last_measured_at` vecchio» →
   `test_a_restart_restores_the_last_known_value`, sul registro di stato
   vero di Home Assistant.

Non è un ripiego: è il posto giusto per verificarli. Il giorno in cui su
dev esistesse un device connesso e muto, il controllo «AC1 dal vivo» di
`verify_m06_live.py` lo segnala da solo.

## Due difetti preesistenti trovati lungo la strada

Nessuno dei due appartiene a M-06. Sono emersi provando a fare i controlli
qui sopra, e sono stati aggirati con modifiche **locali e non committate**.

1. **Il config flow non è migrato ad arch 2.0.** `config_flow.py:199` legge
   `domains[0]["id"]`, ma in 2.0 `GET /auth/user/me/domains` restituisce
   `{"domain": {"prefix": ...}, "role": ...}`: `KeyError: 'id'`, e la UI
   dice «Unknown error occurred». **L'integrazione non è installabile da
   interfaccia su nessun branch di questa catena.** È esattamente il lavoro
   che `api/client.py::list_domains` (M-02) dichiara rimandato alla card del
   config flow: «Teaching the config flow to read the new shape (and to
   persist `prefix` rather than a UUID) is that card's job». Da tracciare
   se non lo è già.
2. **Il pool Cognito di `const.py` è di prod, il base URL è di dev.** Un
   account dev viene rifiutato con `invalid_auth` in fase di setup. È il
   residuo noto di `docs/M-04-verifica-dev.md`, «Un residuo che blocca il
   collaudo end-to-end».
