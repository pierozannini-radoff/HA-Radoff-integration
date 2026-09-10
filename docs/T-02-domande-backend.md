# T-02 — Domande al team Backend / Firmware
### Integrazione Home Assistant · epica RT-116, card RT-2810 · **v6 del 2026-09-09 — arch 2.0**

---

## Premessa

Stiamo **migrando il client direttamente all'architettura 2.0**, senza rilasciare la versione
basata su arch 1.x. I due swagger (`GET /analytics/devices/{deviceSerial}/latest` e
`GET|POST /data/devices`) sono la fonte di verità dei contratti; le domande specifiche di
arch 1.x sono state eliminate in v4 e restano in **Appendice B** con il motivo per cui cadono.

**Novità della v6: tutte e nove le domande 🔴 sono chiuse.** Le ultime tre risposte
(D-01 ambiente, D-02 identificativo, D-03 discovery del dominio) sbloccano l'ultimo pezzo di
progettazione che ci mancava. Da qui in avanti nessuna domanda aperta blocca lo sviluppo: quel
che resta è semantica, decisioni vostre, e verifiche che possiamo fare noi su dev.

**Stato:**
✅ = risposta acquisita (dal backend o già in nostro possesso) ·
🟨 = risposta parziale, manca un pezzo · ⬜ = aperta

**Priorità:**
🔴 = blocca il rilascio o rende sbagliato il comportamento in produzione ·
🟠 = impatta la qualità del rilascio · 🟡 = utile, non bloccante

---

## Le tre cose da sapere prima di leggere il resto

### 1. L'ambiente di riferimento è **dev**, e la produzione esce dallo scopo di questa card

InfluxDB e l'intera API `/analytics/*` sono **già deployati su dev**, ed è lì che completiamo la
migrazione e le verifiche. La pubblicazione in produzione avviene **dopo** la migrazione completa
di prod ed **esula dallo scopo di RT-2810**.

Conseguenze, tutte positive: nessuna data da attendere, nessuna dipendenza da mettere a
calendario, e soprattutto **nessun fallback hardcoded** di unità e soglie — `measures-ranges`
c'è, quindi il client nasce schema-driven come previsto. La preoccupazione che avevamo in v5 (che
InfluxDB arrivasse prima di `/analytics/*`, costringendoci a ricablare le tabelle nel codice)
non si applica.

### 2. La forma finale del client è decisa

| | Endpoint | Quando | Frequenza |
|---|---|---|---|
| **Domini** | `GET /auth/user/me/domains` | al setup | 1 volta |
| **Schema** (unità, etichette, soglie) | `GET /analytics/measures-ranges?device_type=<tipo>` | al setup, poi in cache | 1 richiesta **per tipo**, non per device |
| **Valori** | `GET /data/devices?domain_prefix=<prefix>&page_size=200` | a ogni ciclo | 1 richiesta per installazione, default **5 min** + jitter |
| **Telemetria per device** | `GET /analytics/devices/{deviceSerial}/latest` | **mai** nel percorso normale | — |

Il polling passa da `1 + N` richieste per ciclo a **1 sola**. `measures-ranges` è l'endpoint che
non era in nessuno dei due swagger e che risolve il problema centrale del client: soglie e unità
non stanno più nel codice.

### 3. L'identificativo è `serial_number`, e vale per tutti e tre i nomi

`deviceId` == `serial_number` == `deviceSerial`: un solo identificativo con tre nomi diversi nei
path. È quindi la base dell'`unique_id` delle entità di Home Assistant — l'unica scelta
possibile, dato che il modello device non espone nessun altro identificativo.

---

## Quadro di sintesi

| Stato | Quante | Quali |
|---|---:|---|
| ✅ risposta acquisita | **14** | D-01÷D-06, D-09, D-13, D-15, D-18, D-21, D-22, D-25, D-28 |
| 🟨 risposta parziale | **15** | D-07, D-08, D-10, D-11, D-12, D-14, D-16, D-17, D-19, D-20, D-23, D-24, D-27, D-29, D-31 |
| ⬜ aperta | **6** | D-26, D-30, D-32, D-33, D-34, D-35 |
| **Totale** | **35** | |

### Le 🔴 · 9 su 9 chiuse

| | Domanda | Esito |
|---|---|---|
| D-01 | API 2.0: ambiente, URL, versionamento | ✅ **dev**, già completo. Prod fuori scopo |
| D-02 | Identificativo stabile del device | ✅ `deviceId` == `serial_number` == `deviceSerial` |
| D-03 | Come scopriamo il dominio | ✅ `GET /auth/user/me/domains` |
| D-04 | Quale endpoint per il polling | ✅ `/data/devices` + `measures-ranges` |
| D-05 | Valori già nell'unità dichiarata | ✅ sì, nessuna eccezione |
| D-06 | `pressure` | ✅ Pascal |
| D-13 | Enumerazione di `type` | ✅ `{sense, now, nowplus, city, life, sismoff}` |
| D-21 | Rate limit | ✅ 50 rps / 100 burst **per stage** |
| D-25 | Rotazione dei refresh token | ✅ informazione completa, **serve una vostra decisione** |

### Cosa resta, in quattro secchi

**A. Una sola richiesta, che da sola riduce sei domande.** Lo `swagger.yaml` completo di
`yama-be-core-platform`: contiene `GET /data/devices/{deviceId}`, `/auth/user/me/domains` in
versione 2.0, l'API dei job storici, `POST /data/devices/status` e la forma di `ErrorResponse`.
Copre in tutto o in parte **D-03, D-19, D-23, D-29, D-33, D-35**.

**B. Semantica che solo voi potete dirci** (nessuna osservazione su dev la ricava):
`tvoc` e l'unità `V - Ix` (**D-07**) · il calcolo dell'AQI e il divisore 120 (**D-08**) ·
l'enumerazione di `radon_status` (**D-10**) · gli override `deviceConfig` per singolo device
(**D-12**) · i PM del `sismoff` (**D-14**) · di quanto allarga la finestra il dettaglio
(**D-16**) · cadenza ed enumerazione di `connection_status` (**D-17**) · versionamento dello
schema (**D-11**).

**C. Decisioni vostre, non informazioni:** rotazione dei refresh token (**D-25**) · app client
dedicato (**D-28**) · usage plan / API key per l'integrazione (**D-27**, **D-31**) ·
`superadmin` dentro o fuori (**D-20**) · se e quando correggere l'AQI (**D-08**).

**D. Operativo, per poter verificare noi il resto su dev:** accesso a un device **`sense` o
`city`** su dev (il nostro Now+ non ha l'hardware radon, quindi il radon non è testabile) e a un
account **senza device** (**D-32**).

**Quello che verifichiamo noi su dev, senza disturbarvi:** stabilità dell'ordinamento in
paginazione (D-19) · nome dell'header di request id (D-30) · enumerazione delle `unit` (chiamando
`measures-ranges` senza `device_type`, che unisce tutti i tipi) · se il PM10 vale davvero 200
(D-09) · i PM del `sismoff` (D-14) · se il token del pool attuale è accettato dall'host v2
(D-24) · la forma dei body d'errore (D-29).

---

## Sezione 1 — Prerequisiti della migrazione

### D-01 · 🔴 · ✅ · L'API 2.0: ambiente, URL, versionamento

**Domanda.** (1) Gli endpoint sono già attivi in produzione o solo su INT, e con quale data?
(2) Base URL e base path per ambiente? (3) Versionamento nel path? (4) 1.x e 2.0 convivono?
(5) Ci mandate lo swagger completo, non solo i due path?

**Risposta backend (2026-09-09).**

**Host, per ambiente** (da `infrastructure/terraform/environments/*.tfvars`):
* prod → `https://v2.api.iot.radoff.life`
* stg → `https://v2.api.stg.iot.radoff.life`
* dev → `https://v2.api.dev.iot.radoff.life`
* int → `https://api.int.iot.radoff.life` (**senza** prefisso `v2.`)

**Versionamento: nell'hostname, non nel path.** Nessun `/v2/...`. 1.x e 2.0 sono due API
Gateway su **host diversi**, quindi **convivono nativamente** e non c'è un cut-over che imponga
una data: il client cambia base URL quando è pronto. L'unica data che serve è quella di
dismissione di 1.x.

**Base path, sullo stesso host** (`aws_api_gateway_base_path_mapping`, `main.tf:707-753`):
root → `/admin/*` · `data` → `/data/*` · `auth` → `/auth/*` · `analytics` → `/analytics/*`.

**Ambiente di lavoro: dev.** InfluxDB e `/analytics/*` sono **entrambi già deployati su dev**, ed
è lì che si procede con il resto delle attività. La pubblicazione in produzione verrà fatta dopo
tutta la migrazione di prod e **esula dallo scopo di queste card**. (In produzione, oggi, il
piano dati 2.0 non è ancora utilizzabile: mancano InfluxDB e `/analytics/*` — ma non è un nostro
vincolo.)

**Swagger:** `GET /data/devices/{deviceId}` **è già specificato** in
`yama-be-core-platform/swagger.yaml` (~riga 1982), insieme a tutto il resto. Chiedete quel file:
non manca un pezzo, ne avete ricevuti due estratti.

**Cosa ne facciamo.**
* Sviluppo, test e validazione su **`https://v2.api.dev.iot.radoff.life`**. Il base URL diventa
  un parametro di configurazione dell'integrazione (non una costante), così il passaggio a prod
  non richiede una modifica del codice.
* **Nessuna dipendenza di data e nessun fallback hardcoded**: `measures-ranges` è disponibile
  dove lavoriamo, quindi il client nasce schema-driven (→ D-11).
* Il rilascio pubblico dell'integrazione è fuori dallo scopo di RT-2810 e verrà pianificato a
  valle della migrazione di prod, con una card propria.

**Una sola cosa ci serve ancora, ed è una richiesta, non una domanda:** mandateci
`yama-be-core-platform/swagger.yaml`. Da solo riduce sei delle domande ancora aperte (D-03, D-19,
D-23, D-29, D-33, D-35): preferiamo leggerlo che farvi scrivere sei risposte.

---

### D-02 · 🔴 · ✅ · Qual è l'identificativo stabile del device?

**Domanda.** I tre nomi negli swagger sembrano riferirsi alla stessa cosa ma sono scritti in tre
modi diversi — `deviceSerial` (path analytics), `deviceId` (path `/data/devices/{deviceId}`),
`serial_number` (unico campo del modello, che non espone nessun altro identificativo). Sono la
stessa cosa? E `serial_number` è immutabile per la vita del dispositivo?

**Risposta backend (2026-09-09).** **Sì: `deviceId` == `serial_number` == `deviceSerial`.** Un
solo identificativo, tre nomi diversi nei path.

**Cosa ne facciamo.**
* `serial_number` diventa la base dell'**`unique_id`** delle entità
  (`<serial_number>_<nome_campo>`) e l'identificativo del device nel registro di Home Assistant.
  È anche l'unica scelta possibile: il modello device non espone nessun altro identificativo.
* Dove serve un solo valore lo prendiamo dal payload di `/data/devices`, mai costruito o
  normalizzato da noi: lo usiamo verbatim, così qualunque forma abbia (con o senza prefisso) è
  quella giusta.

**Due residui, che non ci bloccano più ma che ci conviene sapere.**
1. **Immutabilità.** `serial_number` sopravvive a un rename, a un cambio di stanza, a uno
   spostamento di dominio? E in caso di **sostituzione in RMA** il device conserva il serial o ne
   riceve uno nuovo? Se cambia, per l'utente equivale a un dispositivo nuovo: entità ricreate e
   storico perso. Non possiamo evitarlo — ma se ci confermate che l'RMA cambia il serial, lo
   scriviamo nella documentazione dell'integrazione invece di lasciare l'utente a scoprirlo.
2. **Prefisso `RADOFF-`.** Su `type` avete stabilito che il prefisso `radoff-` degli esempi è un
   refuso (→ D-13). I serial d'esempio hanno la stessa doppia forma (`SENSE-001` in analytics,
   `RADOFF-SENSE-001` in `/data/devices`): se anche lì è un refuso, vale la pena correggerlo,
   perché è il valore che un integratore vede per primo. Per noi è indifferente: lo usiamo come
   arriva.

### D-03 · 🔴 · ✅ · Il dominio serve ancora al client? E come lo scopriamo?

**Domanda.** In arch 2.0 `domain_prefix` è un query param opzionale di `GET /data/devices`, ma
nella risposta a D-04 ci dite di passarlo **sempre**. Per passarlo dobbiamo conoscerlo, e non
avevamo trovato nessun endpoint che ce lo dica: niente header `x-domain`, niente claim `d_*`, e
`/auth/user/me/domains` lo credevamo solo di arch 1.x.

**Risposta backend (2026-09-09).** **`GET /auth/user/me/domains` esiste anche in arch 2.0 e
restituisce la lista dei domini dell'utente.** È il passo di discovery che cercavamo.

**Cosa ne facciamo — il config flow è deciso.**
1. L'utente inserisce le credenziali → login SRP;
2. `GET /auth/user/me/domains` con il solo bearer token → lista dei domini;
3. **un dominio** → lo selezioniamo automaticamente, nessuna domanda all'utente;
   **più domini** → glielo facciamo scegliere;
4. persistiamo il `domain_prefix` scelto e lo passiamo **sempre** esplicitamente su
   `GET /data/devices`, come ci indicate.

Il problema dell'uovo e della gallina di arch 1.x non si ripresenta: in 2.0 la chiamata non
richiede un header di dominio noto a priori, quindi non serve più decodificare i claim `d_*`.
E il `domain_prefix` è human-readable (`radoff-hq`), quindi utilizzabile anche come etichetta
nel menu di scelta, senza dover risolvere un UUID.

**Tre dettagli che verifichiamo noi su dev appena abbiamo lo swagger** (li elenchiamo perché se
la risposta è nota vi costa meno dirla che farcela misurare):
1. La forma della response 2.0: contiene `prefix` (la chiave da passare come `domain_prefix`) e
   un **nome visualizzabile** distinto dal prefisso? Sul payload 1.x il `prefix` era il primo
   blocco dell'UUID, quindi non era mostrabile; qui dovrebbe essere il prefisso naturale.
2. Per un utente **multi-dominio**, restituisce l'elenco completo (→ D-20 per la cardinalità).
3. Per un utente **senza domini**, un 200 con lista vuota o un errore (→ D-32).

### D-04 · 🔴 · ✅ · Quale endpoint per il polling, e chi è autorizzato a chiamarlo?

**Domanda.** Due strade: **A** — `GET /data/devices` con `telemetry` inline, una richiesta per
ciclo; **B** — `GET /analytics/devices/{deviceSerial}/latest`, una per device, ma l'unica che
porta `schema.field_metadata`. Quale volete che percorriamo? E su B: è accessibile a un utente
normale? Come è autorizzato, visto che lo swagger non elenca né 401 né 403? È un contratto
stabile? Se la strada è A, come otteniamo `field_metadata` — esiste un endpoint per **tipo** di
device?

**Risposta backend (2026-09-09).**

**La strada proposta è quella giusta, con una correzione: il terzo endpoint che serve esiste
già.**

**Per i valori → A.** `GET /data/devices?domain_prefix=<prefix>&page_size=200` (max 200, con
`pagination.total_pages`). Una richiesta per ciclo. Passare **sempre** `domain_prefix`
esplicitamente.

**Per lo schema → né A né B: `GET /analytics/measures-ranges?device_type=<tipo>`.** È esattamente
il punto 4 della domanda: **una chiamata per tipo, non per device**, da fare al setup e mettere
in cache. `security: [CognitoAuth]`, nessun gruppo richiesto. Restituisce `label`, `unit`,
`dataType`, `ranges` per ogni misura del tipo; senza `device_type` unisce tutti i tipi; con un
tipo ignoto risponde 404 elencando i tipi registrati in `available`. Due trappole: `pos` è
**sparso** (ordinare, mai indicizzare) e l'ordine delle fasce è
`excellent → high → good → poor → terrible` (`high` sta **fra** excellent e good).

Quindi non serve chiamare `/latest` per device nemmeno al setup.

**⚠️ Sull'autorizzazione di B: il sospetto è fondato.** `_route_device_latest(pp, qp, _ev)`
riceve l'event e lo **ignora** (underscore); `_handle_get_latest(device_serial, query_params)`
non lo prende affatto (`data-analytics/handler.py:183-189, 575-577`). **Nessun controllo di
dominio.** L'API Gateway monta un authorizer `COGNITO_USER_POOLS`
(`modules/data-analytics/main.tf:530-534`), quindi serve un token valido — ma **qualsiasi utente
autenticato può leggere la telemetria di qualsiasi serial, anche di un altro dominio**, se ne
conosce il numero. L'assenza di 401/403 nello swagger è fedele all'implementazione, non una
svista di documentazione. Nel data-analytics i soli gate esistenti sono `superadmin` su
`/analytics/user/report` e sugli endpoint raw-data. **Si sta procedendo a fixare il problema.**

**Accessibile a un utente normale:** sì, `/latest` e `measures-ranges` non richiedono alcun
gruppo.

**Correzione al costo stimato:** sul percorso comune `/latest` legge la **telemetry-cache
DynamoDB** (`GetItem` O(1), scritta dal Silver Processor a ogni ingest); InfluxDB è solo il
fallback su cache miss ed è fuori dal percorso caldo. Resta più costoso di A, ma meno del
previsto.

**Contratto stabile:** nessuna garanzia formale nel codice. Ma `measures-ranges` è la sede
pensata per i client (lo swagger dice *«Read the field list from
`/analytics/measures-ranges?device_type=<type>` rather than assuming this example's keys»*),
quindi è quello su cui appoggiarsi.

**⚠️ Vale solo su int/stg/dev: in prod l'intera API `/analytics/*` non è deployata (→ D-01).**

**Cosa ne facciamo.** Adottiamo il pattern della tabella in cima al documento. `/latest` esce
completamente dal nostro percorso di polling — e vi confermiamo che **non lo useremo**, quindi
il buco di autorizzazione non è una dipendenza per noi.

**Due residui, che non ci bloccano ma vanno tracciati.**
1. Il fix di autorizzazione su `/analytics/*` toccherà anche **`measures-ranges`**? Se
   introducesse uno scope per dominio o un gruppo richiesto, il nostro percorso di setup si
   romperebbe. Ci basta sapere che `measures-ranges` resta accessibile a un utente autenticato
   qualsiasi.
2. Segnaliamo per completezza che, non essendoci nessun controllo di dominio su `/latest`, la
   telemetria di qualsiasi serial è oggi leggibile da qualsiasi account autenticato su
   int/stg/dev. Lo riportiamo solo perché la nostra prima ipotesi di design lo usava: ci siamo
   spostati su A anche per questo.

---

## Sezione 2 — Correttezza dei dati mostrati all'utente

### D-05 · 🔴 · ✅ · I valori di `telemetry` sono già nell'unità dichiarata?

**Domanda.** I valori del blocco `telemetry` sono già convertiti nell'unità dichiarata in
`field_metadata`, e il client non deve applicare nessun fattore di scala?

**Risposta backend (2026-09-09).**

**Sì. `field_metadata.unit` è l'unità del valore, il client non applica nessun fattore.
Eccezioni: nessuna** — tranne il dubbio su `pressure` (→ D-06, risolto anch'esso).

Tre riscontri:
1. `scale_factors` è **vuoto per tutti e cinque i device type**: `dynamodb-schemas.tf:91` →
   `_scale_factors = {} # no scale factors needed — values arrive in native units`.
2. Dove un fattore esistesse, è applicato **lato server in scrittura**
   (`silver-processor/handler.py`, `_format_field_value` moltiplica prima di scrivere in
   InfluxDB), mai in lettura.
3. `scaleFactor` è **deliberatamente escluso dalla response**: `schema_sync_module.py:16-26` lo
   definisce authoring-only, l'allowlist verso i client è `("acronym", "pos")`. Un client non
   deve poterlo vedere proprio perché non deve applicarlo.

Eliminare `0.00835` è corretto.

**Precisazione sull'origine di quel fattore, che è anche un bug aperto lato piattaforma.** Non
era senza fonte: `1/120 = 0.008333`, e `120` è `TEMPERATURE_MOLTIPLICATOR_FACTOR` dichiarato in
`measurement-ranges.ts` della piattaforma legacy. Il calcolo AQI **in produzione oggi** divide
ancora per 120 (`silver-processor/aqi_calculator.py::_temp_index` → `temp_c = raw_temp / 120.0`),
e il commento in `yama-ops-migration-module/.../algorithm.py:234` lo ammette: *«the temperature
divisor is 120, not 100. That is deliberate and legacy-faithful … which matters more here than
the physical reading»*. Se `internal_temperature` arriva già in °C, quel divisore confronta 21.8
con soglie pensate per `18*120` / `27*120`: **l'AQI risulta sistematicamente sbagliato sulla
componente temperatura.** Pesa poco (1/17 in categoria 2), ma è un bug reale.

**Cosa ne facciamo.** Rimuoviamo il fattore `0.00835` dal client (era `1/120`, ereditato dal
legacy: mistero risolto). Il bug AQI ricade su **D-08**, dove chiediamo se e quando `aqi_value`
verrà ricalcolato — perché finché resta, il valore che pubblicheremo agli utenti è sbagliato per
colpa vostra e non nostra, e vorremmo saperlo prima di esporlo.

---

### D-06 · 🔴 · ✅ · `pressure`: Pascal o ettopascal?

**Domanda.** `field_metadata` dichiara `unit: "Pa"` ma gli esempi riportano `pressure: 1012.8` —
che è un valore in hPa. Quale dei due è il refuso?

**Risposta backend (2026-09-09).**

**Pa. `unit` è giusto, l'esempio è il refuso — da correggere in entrambi gli swagger.**

* Il source of truth è
  `yama-iot-core-platform/infrastructure/terraform/modules/k-data-stream/dynamodb-schemas.tf:32`
  → `pressure = { unit = "Pa", label = "Atmospheric pressure" }`. Da quel file si generano
  **sia** i parametri SSM letti a runtime, **sia** le colonne Glue del Parquet, **sia** gli item
  DynamoDB `device-schemas` che l'API riespone come `field_metadata`: è un'unica sorgente, non
  tre documenti che possono divergere.
* `scale_factors = {}` (→ D-05) e **nessuna conversione su `pressure` in tutta la catena di
  lettura** (verificato su `data-analytics/modules/influxdb_module.py` e
  `data-manager/modules/influxdb_module.py`): il valore restituito è quello grezzo del firmware,
  cioè esattamente i 90.000–101.800 già osservati oggi.

Quindi si può disambiguare senza chiamare la produzione: **il numero non cambia con la
migrazione**, cambia solo il nome del campo che lo contiene.

Da correggere: `pressure: 1012.8` (Sense) e `1013.2` (Sismoff) negli esempi di
`/analytics/devices/{deviceSerial}/latest` e `/data/devices`.

**Cosa ne facciamo.** `pressure` in Pa, e in Home Assistant lo dichiariamo con
`native_unit_of_measurement = Pa` lasciando alla UI la conversione verso hPa/mbar secondo le
preferenze dell'utente. Nessuna conversione nel client.

---

### D-07 · 🟠 · 🟨 · `tvoc`: unità `V - Ix` e soglie incompatibili

**Domanda.** Qual è l'unità fisica reale di `tvoc`, e quali sono le soglie corrette per quella
scala?

**Cosa sappiamo già.** Nello **stesso** `field_metadata`, due informazioni che non possono essere
entrambe corrette:
* `tvoc: { unit: "V - Ix", dataType: "float" }`, con valori di telemetria `0.132` e `0.185`;
* `ranges` con `excellent ≤100 · high ≤200 · good ≤300 · poor ≤400 · terrible`.

Con valori dell'ordine di 0,1 e soglie a 100–400, l'indice qualitativo del TVOC è "excellent" in
qualsiasi condizione dell'aria: un sensore inerte.

**[agg. 2026-09-09]** La risposta a D-05 esclude che ci sia un fattore di scala a spiegare il
divario (`scale_factors = {}` per tutti i tipi, e nessuna conversione in lettura). Quindi o i
valori d'esempio sono un refuso come quello della pressione, o le soglie appartengono a un'altra
scala. E la risposta a D-04 aggiunge un elemento: le stesse soglie ci arriveranno da
`measures-ranges`, che è la sede su cui ci dite di appoggiarci — quindi l'incoerenza ce la
troveremo in produzione, non solo negli esempi.

**Perché ci serve.** Dobbiamo dichiarare un'unità a Home Assistant, che accetta un'enumerazione
chiusa e non contiene `V - Ix`. Se è un indice adimensionale lo esponiamo senza unità; se è una
concentrazione, ci serve quale.

**Serve.** (a) Cosa significa `V - Ix` e qual è l'unità reale. (b) Le soglie corrette per la
scala dei valori effettivi — o la conferma che i valori d'esempio siano sbagliati e che il
`tvoc` reale sia dell'ordine delle centinaia. (c) Come per la pressione: qual è il
`dynamodb-schemas.tf` di riferimento per `tvoc`? Se il source of truth è quello, la risposta è a
una riga di distanza.

**Risposta backend:**
> _(da compilare)_

---

### D-08 · 🟠 · 🟨 · `aqi_value`: scala, calcolo e il divisore 120

**Domanda.** Come è calcolato `aqi_value`, e possiamo descriverlo all'utente?

**Cosa sappiamo già.** Dallo swagger: `aqi_value: { unit: "", dataType: "float" }`, soglie
`excellent ≤1 · high ≤2 · good ≤3 · poor ≤4 · terrible`, valori d'esempio `2.1` e `2.4`. Quindi
**scala circa 0–5, più basso è meglio**.

**[agg. 2026-09-09] Dalla risposta a D-05 sappiamo due cose nuove, e la seconda è un problema.**
1. Il calcolo vive in `silver-processor/aqi_calculator.py`, è **proprietario**, ed è organizzato
   in **categorie con pesi** (dalla vostra nota: la temperatura pesa "1/17 in categoria 2").
2. La componente temperatura è calcolata con `temp_c = raw_temp / 120.0`, divisore legacy
   mantenuto per fedeltà. Ma se `internal_temperature` arriva già in °C (D-05), quel divisore
   confronta ~21,8 con soglie pensate per `18*120`: **la componente temperatura dell'AQI è
   sistematicamente sbagliata**, e lo dice il vostro commento nel codice.

**Perché ci serve.** Home Assistant ha una `device_class: aqi` che presuppone la scala EPA
0–500: con una scala 1–5 **non possiamo usarla**, e lo esporremo come indice proprietario senza
`device_class` — scelta corretta, ma da motivare nella review pubblica. E soprattutto: se il
valore è sbagliato, pubblicarlo significa mostrare agli utenti un numero errato con il nostro
nome sopra.

**Serve.** (a) È un indice proprietario o segue uno standard (l'EU CAQI è appunto 1–5)? (b) Le
categorie e i pesi, almeno a grandi linee, per la documentazione. (c) Il valore può eccedere 5?
(d) **Il divisore 120 verrà corretto?** Con quale tempistica, e i valori già scritti verranno
ricalcolati? Se la risposta è "non a breve", ditecelo: valuteremo di **non esporre `aqi_value`**
nel primo rilascio, invece di esporre un valore che sapete essere errato.

**Risposta backend:**
> _(da compilare)_

---

### D-09 · 🟠 · ✅ · Soglie e nomi dei livelli qualitativi

**Domanda.** Le soglie e le etichette con cui rendiamo i livelli qualitativi sono quelle
ufficiali, le stesse dell'app mobile?

**Cosa sappiamo già.** Le soglie sono servite dall'API in `ranges` e coincidono con quelle che il
client aveva hardcodate:

| Campo | Nostre soglie | `ranges` | Esito |
|---|---|---|:--:|
| tvoc | 100 / 200 / 300 / 400 | 100 / 200 / 300 / 400 | ✅ (ma → D-07) |
| eco2 | 500 / 1000 / 1500 / 2000 | 500 / 1000 / 1500 / 2000 | ✅ |
| pm25 | 16 / 21 / 26 / 32 | 16 / 21 / 26 / 32 | ✅ |
| pm1 | 6 / 9 / 12 / 15 | 6 / 9 / 12 / 15 | ✅ |
| internal_temperature | 18 / 27 | 18 / 27 | ✅ |
| relative_humidity | 40 / 60 | 40 / 60 | ✅ |
| pm10 | 20 / 30 / 40 / 50 | **200** / 30 / 40 / 50 | ⚠️ |
| radon_bqm3 | — | 100 / 200 / 300 / 500 | 🆕 |
| aqi_value | — | 1 / 2 / 3 / 4 | 🆕 |
| co (Sismoff) | — | 5 / 9 / 25 / 50 | 🆕 |
| ch4 (Sismoff) | — | 500 / 1000 / 3000 / 5000 | 🆕 |

**[agg. 2026-09-09] Due dei quattro punti aperti sono chiusi dalla risposta a D-04.**
* **Ordine e nomi dei livelli: `excellent → high → good → poor → terrible` è intenzionale**, con
  `high` **fra** `excellent` e `good`. Non è un refuso: adeguiamo il client, che usava
  `excellent → good → medium → poor → terrible`.
* **`pos` è sparso:** va usato per **ordinare**, mai come indice. Preso nota — è esattamente il
  tipo di trappola che ci avrebbe fatto sbagliare silenziosamente l'associazione fascia/livello.
* Le soglie non le teniamo più nel codice: le leggiamo da `measures-ranges` (→ D-11).

**Restano due punti.**
1. **PM10, primo bound = 200**, seguito da `high ≤30`: sequenza non monotona, in entrambi gli
   esempi. Assumiamo un typo per `20`. **Ora che leggiamo i `ranges` a runtime questo errore
   finisce direttamente sotto gli occhi dell'utente**, quindi ci serve sia la conferma sia la
   correzione nel `dynamodb-schemas.tf`.
2. L'ultimo elemento di `ranges`, senza `upperBound`, va letto come "tutto il resto":
   confermate?

**Risposta backend:**
> _(da compilare)_

---

### D-10 · 🟠 · 🟨 · `radon_status`: tipo ed enumerazione

**Domanda.** Qual è il tipo e l'enumerazione dei valori di `radon_status`?

**Cosa sappiamo già.** Lo swagger si contraddice: `field_metadata` dichiara
`dataType: "string"`, ma la telemetria d'esempio riporta `radon_status: 2` — un numero, in
entrambi gli esempi.

**[agg. 2026-09-09]** Dalla risposta a D-13 sappiamo che **`now` e `nowplus` non hanno l'hardware
radon** (`NO_RADON_DEVICE_TYPES`), quindi su quei tipi né `radon_bqm3` né `radon_status`
esistono, e il radon si vede su `sense`, `city` e `sismoff`. Sappiamo anche che il calcolo del
radon aggrega per minuto con `MIN_DATA_MINUTES = 5`: sospettiamo che `radon_status` esprima
proprio la maturità di quell'aggregazione.

**Perché ci serve.** Vogliamo esporlo come entità `enum` (l'utente legge un'etichetta, non un
numero) e per farlo serve la mappa valore → significato. Se indica la validità della misura, è
anche il segnale con cui decidere se mostrare o nascondere `radon_bqm3` — e allora diventa
importante.

**Serve.** (a) Tipo reale (int o string). (b) L'enumerazione completa con il significato di
ciascun valore. (c) Se esistono valori che indicano "misura non ancora attendibile": in quel
caso non dobbiamo pubblicare il radon come se fosse un dato buono.

**Risposta backend:**
> _(da compilare)_

---

## Sezione 3 — Modello dinamico dei sensori

### D-11 · 🟠 · 🟨 · `measures-ranges` / `field_metadata` come contratto per un client schema-driven

**Domanda.** Possiamo trattare lo schema servito dall'API come la **sede contrattuale** di
etichette, unità, tipi e soglie, e costruire le entità di Home Assistant dinamicamente invece di
tenere tabelle hardcodate?

**[agg. 2026-09-09] Risposta in gran parte acquisita, e la sede è cambiata.**
* La sede giusta **non** è `field_metadata` dentro `/latest`, ma
  **`GET /analytics/measures-ranges?device_type=<tipo>`**: una chiamata per tipo, da fare al
  setup e mettere in cache. È l'endpoint *pensato per i client*, e lo swagger stesso dice
  *«Read the field list from `/analytics/measures-ranges?device_type=<type>` rather than assuming
  this example's keys»*.
* Senza `device_type` unisce tutti i tipi; con un tipo ignoto → **404 con la lista dei tipi
  validi in `available`** (utile: è anche il modo di scoprire l'enumerazione a runtime).
* Restituisce `label`, `unit`, `dataType`, `ranges`, più `acronym` e `pos` (allowlist verso i
  client). **`scaleFactor` è deliberatamente escluso** perché il client non deve applicarlo
  (→ D-05).
* **`pos` è sparso**: ordinare, mai indicizzare. **Ordine delle fasce:**
  `excellent → high → good → poor → terrible`.
* **Nessuna garanzia formale di stabilità** nel codice, ma è la superficie pensata per i client.

**Cosa ne facciamo.** Costruiamo le entità dallo schema, con `type` usato **solo come chiave di
cache** e mai come filtro (→ D-13).

**[agg. 2026-09-09] Il punto che ci preoccupava è caduto:** `/analytics/*` è già deployata su
**dev**, che è il nostro ambiente di lavoro (→ D-01). Non ci serve nessun fallback di unità e
soglie hardcodate: il client nasce schema-driven, come previsto.

**Restano tre punti.**
1. **Versionamento:** come vediamo che lo schema è cambiato? Basta rileggerlo al riavvio, o
   dobbiamo rileggerlo periodicamente? `firmware_version` del device è un segnale utile?
2. **`unit` viene da un'enumerazione chiusa?** Ci serve per mapparla sulle unità di Home
   Assistant, che sono un'enumerazione chiusa a loro volta. Finora abbiamo visto `"Pa"`, `"%"`,
   `"ppm"`, `"µg/m³"`, `"°C"`, `"Bq/m³"`, `""`, `"V - Ix"`. Un valore fuori enumerazione ci
   costringe a esporre l'entità senza unità. **Questa la verifichiamo noi** chiamando
   `measures-ranges` senza `device_type`, che unisce tutti i tipi: se la lista è quella, ci basta.
3. `dataType` quali valori può assumere oltre a `float` e `string`? E gli `status` dei `ranges`
   sono un'enumerazione chiusa (`excellent`, `high`, `good`, `poor`, `terrible`, `low`)?

**Risposta backend:**
> _(da compilare)_

---

### D-12 · 🟠 · 🟨 · Quali campi sono garantiti, e come li risolviamo per singolo device

**Domanda.** L'insieme delle misure di un device è determinato dal suo `type`, o può variare fra
due esemplari dello stesso tipo?

**[agg. 2026-09-09] Risposta acquisita, ed è più complicata di come l'avevamo posta.** Dalla
risposta a D-13: l'insieme delle misure varia su **tre assi**, non uno.
1. **per device type** — `sismoff` non ha PM1/2.5/10; `now` e `nowplus` non hanno radon
   (`NO_RADON_DEVICE_TYPES`, mancanza di hardware);
2. **per singolo device** — un override `deviceConfig` nel device-registry può disattivare una
   misura su un **esemplare** il cui tipo la prevede (`device_config_resolver.py`);
3. **retroattivamente** — `excluded_from_influx` è applicato **in lettura**, quindi una misura
   ritirata sparisce anche dallo storico già scritto.

A questo si aggiunge, dallo swagger: *"`telemetry` keeps every non-null InfluxDB column"* → un
campo `null` **non compare affatto**, quindi l'assenza di una chiave non distingue "sensore non
presente" da "valore mancante in questa lettura".

**Il problema che questo ci apre.** `measures-ranges` è **per tipo** (→ D-11), ma l'asse 2 è
**per device**: seguendo il pattern che ci avete indicato, dichiareremmo a Home Assistant il
radon su un `sense` che ha il radon disattivato da `deviceConfig`, e l'utente si troverebbe
un'entità permanentemente vuota. In Home Assistant un'entità creata e poi sempre vuota è peggio
di un'entità mai creata: resta nel registro, nelle dashboard e nelle automazioni.

**Serve.** (a) Come fa un client a conoscere gli override **per device**? Il blocco `schema` di
`/latest` li riflette (e allora ci serve chiamarlo una volta per device al setup, il che è
accettabile) o riflette solo il tipo? (b) Esiste un campo nel modello device di `/data/devices`
che elenchi le misure effettive dell'esemplare? (c) Quanto è diffuso nella pratica l'uso di
`deviceConfig` per disattivare misure: è un caso raro o normale? Se è raro, accettiamo il
compromesso e ci basiamo sul tipo. (d) Un `excluded_from_influx` retroattivo, per noi, si
manifesta come un'entità che smette per sempre di aggiornarsi: c'è un modo per accorgercene?

**Risposta backend:**
> _(da compilare)_

---

### D-13 · 🔴 · ✅ · L'enumerazione di `type`

**Domanda.** Qual è l'enumerazione autoritativa e chiusa di `type`, nella forma esatta che l'API
restituisce? (a) `sense` o `radoff-sense`? (b) il valore per Now e Now+? (c) è la stessa chiave
del query param `device_type`? (d) o possiamo trattarlo come opaco?

**Risposta backend (2026-09-09).**

**(a) Senza prefisso.** Catalogo canonico e chiuso, da
`yama-ops-migration-module/scripts/step_8_rds_entities/modules/rds_entities/mapping.py:82`:
`{"sense", "now", "nowplus", "city", "life", "sismoff"}` — un valore fuori catalogo **fa fallire
la migrazione** (`mapping.py:812-817`), quindi la lista è vincolante.

Gli esempi `radoff-sense` / `radoff-life` / `radoff-city` di `/data/devices` sono **il refuso**.
Concordano su `sense` senza prefisso: il catalogo, il codice di normalizzazione, i test unitari,
gli esempi di `/analytics/.../latest` e le partizioni dello storico già migrato.

**(b) `Now+` → `nowplus`, `Now` → `now`.** La normalizzazione legacy→2.0 è *minuscolo +
`+`→`plus`*, identica in `be-core` (`firmware/modules/iot_module.py:170-179`) e nella migrazione
(`mapping.py:117-126`). Test esplicito: `("Now+", "nowplus")`.

**(c) Sì, è la stessa chiave, e le due forme NON sono intercambiabili.** Il `device_type` degli
endpoint analytics indicizza direttamente il dizionario `DEVICE_SCHEMAS`, che ha esattamente
quelle chiavi: `radoff-sense` darebbe 404 (con la lista dei tipi validi in `available`).

**(d) No, non possiamo garantire che sia opaco — ma la strada proposta resta quella giusta, per
un motivo più forte.** `device.type` in RDS è `VARCHAR(50)` libero (`data-manager/schema.sql:178`,
commento *«sense, nowplus, life, etc.»*), **senza vincolo enum**: la chiusura del catalogo è
applicata solo dal validatore di migrazione, non dal database. Quindi nessuno può promettere che
non arrivi un valore nuovo.

Però **basarsi su `field_metadata` invece che su `type` è comunque corretto**, perché l'insieme
delle misure varia su **tre** assi (→ D-12). Con `type` chiuso non sareste comunque al riparo.
**Costruite le entità da `field_metadata` / `measures-ranges`, e usate `type` solo come chiave di
cache dello schema** — mai come filtro che scarta device.

**Se un filtro su `type` serve comunque**, rendetelo tollerante (minuscolo, `+`→`plus`, strip di
un eventuale prefisso `radoff-`) e soprattutto **loggate a WARNING ogni device scartato per tipo
sconosciuto**: è ciò che trasforma «spariscono tutti in silenzio» in una riga di log che lo
spiega.

**Nota sul Now/Now+:** `NO_RADON_DEVICE_TYPES = {"now", "nowplus"}`
(`device_config_resolver.py:59-60`) — **non hanno l'hardware radon**, quindi nessun
`radon_bqm3`. Non è un filtro né un abbonamento. Confermato dalla migrazione dello storico: il
radon copre il 78,8% delle righe, solo `city` e `sense`. Il radon si vede su `sense`, `city`,
`sismoff`.

**Cosa ne facciamo.**
* Enumerazione adottata: `sense`, `now`, `nowplus`, `city`, `life`, `sismoff`, minuscolo.
* **Nessun filtro su `type`**: non scartiamo più niente. `type` serve solo come chiave di cache
  dello schema, con normalizzazione tollerante e un WARNING sui tipi sconosciuti, come ci
  suggerite.
* **Conseguenza sul nostro sviluppo:** il dispositivo su cui stiamo sviluppando è un **Now+**,
  che non ha l'hardware radon. Quindi il radon — la misura per cui gli utenti comprano un Radoff
  — **non è testabile sul nostro device**, e ci serve accesso a un `sense` o a un `city` su int
  per validare quella parte prima del rilascio. È una richiesta operativa, la giriamo a parte.

---

### D-14 · 🟡 · 🟨 · Campi esposti dagli altri tipi di device

**Domanda.** Quali campi espone `telemetry` per **Now**, **Now+**, **City** e **LIFE**?

**[agg. 2026-09-09] In gran parte risolta dal metodo, non dall'elenco:** con
`measures-ranges?device_type=<tipo>` possiamo interrogare l'insieme delle misure di ogni tipo
senza che ci venga elencato. Quello che sappiamo per certo dalle risposte:
* **`now` / `nowplus`:** nessun radon (mancanza di hardware);
* **`sismoff`:** nessun PM1 / PM2.5 / PM10, ma `co` e `ch4`;
* **radon su:** `sense`, `city`, `sismoff`.

**⚠️ Ma qui c'è una contraddizione da segnalare.** La vostra risposta a D-13 dice che *"`sismoff`
non ha PM1/2.5/10"*, mentre l'esempio `sismoff_device` dello swagger analytics **elenca
`pm10`, `pm25` e `pm1`** sia in `field_metadata` (14 campi) sia in `telemetry`
(`pm10: 18.4, pm25: 12.1, pm1: 6.8`). Uno dei due è sbagliato, e non è un dettaglio: è la
differenza fra tre entità che esistono e tre che non esisteranno mai.

**Serve.** (a) Quale delle due è corretta per il `sismoff`. (b) Se `measures-ranges` è
autoritativo su questo, ci basta quello e la contraddizione resta solo un refuso negli esempi da
correggere.

**Risposta backend:**
> _(da compilare)_

---

## Sezione 4 — Freschezza del dato e disponibilità

### D-15 · 🟠 · ✅ · Timestamp per campo *(richiesta, non domanda)*

**Domanda.** Il blocco `telemetry` può portare un timestamp **per campo**, e non solo uno
complessivo?

**Cosa sappiamo già. La risposta è documentata, ed è no.** Lo swagger `/data/devices`: *"Each
sensor field holds its own most recent value, and `timestamp` is the newest of those field times.
The fields therefore do not necessarily all come from a single message ... Read `timestamp` as
'nothing here is newer than this', not as 'all of this was measured at this instant'."*

**[agg. 2026-09-09]** La risposta a D-21 rafforza il problema con dei numeri: la finestra di
ricerca è di **6 ore**, e la cadenza dei sensori è di **1 messaggio al minuto** mentre il radon
aggrega su almeno **5 minuti**. Quindi il divario fra il `timestamp` complessivo e l'età reale
del radon non è un caso limite: è la norma, e può arrivare a ore prima che il campo scompaia.

**Perché ci serve.** Senza timestamp per campo non possiamo distinguere **una singola grandezza
ferma** da **un dispositivo del tutto muto**: dobbiamo marcare tutte le entità come non
disponibili insieme, o nessuna. Nella pratica un utente vedrà un valore di radon vecchio di ore
presentato come fresco, e non avremo modo di dirglielo.

**La richiesta.** Un timestamp per campo — `{"value": 36.3, "timestamp": "..."}` oppure un blocco
parallelo `field_timestamps: { radon_bqm3: "...", tvoc: "..." }`, che è retrocompatibile e non
tocca la forma attuale di `telemetry`. Il dato c'è: la telemetry-cache DynamoDB è scritta a ogni
ingest, e InfluxDB ha il tempo della singola colonna che state già pivotando.

**Ripiego, se non è possibile a breve.** Con le cadenze di D-18 (1 min / 5 min) possiamo stimare
la staleness per campo invece di misurarla. Funziona, ma è un'euristica nostra che sbaglierà nei
casi limite.

**Risposta backend:**
> _(da compilare)_

---

### D-16 · 🟠 · 🟨 · Le finestre di lookback: `telemetry: null` e "widens"

**Domanda.** Quanto valgono, in minuti, la "short window" della lista e la finestra allargata del
dettaglio?

**[agg. 2026-09-09] Metà risposta acquisita.** Dalla risposta a D-21: la finestra di ricerca
della telemetria è di **6 ore** (`influxdb_list_window` / `influxdb_latest_window`, su int e
prod). `GET /data/devices` **non allarga** su miss → `telemetry: null` per un device silente da
più di 6 h; `GET /data/devices/{deviceId}` **allarga**. Due chiamate possono legittimamente dare
risposte diverse sullo stesso device — e ci indicate di usare **`connection_status`** per la
disponibilità delle entità, non l'assenza di telemetria.

**Cosa ne facciamo.** Disponibilità basata su `connection_status`, non su `telemetry: null`. Un
`telemetry: null` diventa "nessun valore da mostrare ora", non "device offline".

**Resta.** (a) **Quanto allarga** `GET /data/devices/{deviceId}`? È il numero che ci dice se
esiste un caso in cui il dettaglio ha dati e la lista no per un device che l'utente considera
funzionante. (b) Le due finestre sono le stesse su tutti gli ambienti (avete citato int e prod,
non stg/dev)? (c) 6 h è configurabile lato vostro: se cambia, ce lo comunicate? Il valore entra
nella nostra logica di disponibilità.

**Risposta backend:**
> _(da compilare)_

---

### D-17 · 🟠 · 🟨 · `connection_status` e `status`: enumerazioni e cadenza

**Domanda.** Quali valori possono assumere `connection_status` e `status`, che relazione hanno, e
dopo quanto silenzio un device diventa "non connesso"?

**Cosa sappiamo già.** Entrambi gli swagger espongono `connection_status: "connected"` e
`connection_status_updated_at` (ISO-8601 UTC), oltre a un campo distinto e coesistente
`status: "active"`. `connection_status` è sincronizzato da DynamoDB.

**[agg. 2026-09-09] Questa domanda è diventata più importante di quanto fosse.** Nella risposta a
D-21 ci dite di usare **`connection_status` per la disponibilità delle entità**, non l'assenza di
telemetria: è quindi il campo su cui poggia tutta la logica di disponibilità del client, e ne
sappiamo ancora solo il nome e un valore d'esempio.

**Serve.** (a) L'**enumerazione completa** di `connection_status` (`connected` e cos'altro:
`disconnected`, `unknown`, `never_connected`?). (b) **Chi lo aggiorna, con quale cadenza, e dopo
quanto silenzio del device passa a non-connesso** — con la cadenza di 1 msg/min di D-18, la
soglia è probabilmente di pochi minuti, ma è un numero che deve venire da voi. (c) L'enumerazione
di `status` e la sua relazione con `connection_status`: `status` è amministrativo
(attivo/dismesso) e `connection_status` operativo? (d) Un device `connected` con
`telemetry: null` è possibile, e come lo interpretiamo? (e) `connection_status_updated_at` è il
momento dell'ultimo cambio di stato o dell'ultimo controllo?

**Risposta backend:**
> _(da compilare)_

---

### D-18 · 🟡 · ✅ · Cadenza di invio dei sensori

**Domanda.** Con quale cadenza i dispositivi inviano i dati al cloud? Servono due numeri: i
sensori "veloci" e il radon.

**Risposta backend (2026-09-09).** Dalla risposta a D-21: **un messaggio al minuto** — bucket
`live-data` documentato come «1-minute raw» (`yama-iot-core-platform/ARCHITECTURE.md` §7.5) — e
il calcolo del radon aggrega per minuto con **`MIN_DATA_MINUTES = 5`**. È questo il pavimento
vero del polling: sotto i 60 s il **device** non ha prodotto nulla di nuovo.

**Cosa ne facciamo.** Minimo configurabile **60 s**, default **5 minuti** con jitter (→ D-21).
Motivazione corretta in documentazione: il limite è la cadenza del dispositivo, non un TTL di
cache.

**Residuo minimo.** `MIN_DATA_MINUTES = 5` è la finestra minima di aggregazione: il valore di
`radon_bqm3` si aggiorna **ogni** 5 minuti, o quello è solo il minimo di dati necessario per
produrne uno? Serve per la stima di staleness del ripiego di D-15.

---

## Sezione 5 — Paginazione, scala, limiti

### D-19 · 🟠 · 🟨 · Paginazione e ordinamento

**Domanda.** L'ordinamento dei risultati di `GET /data/devices` è stabile fra le pagine e fra le
chiamate?

**Cosa sappiamo già.** `page` (1-based, default 1), `page_size` (min 1, **max 200**, default 50),
`pagination` con `page`, `page_size`, `total`, `total_pages`. Ci avete confermato `page_size=200`
come parametro da usare. Nota: *"`pagination.total` and `total_pages` count the items actually
returned"* — gli slave LIFE non sono conteggiati perché annidati nel controller (→ D-33).

**Perché ci serve.** Se l'ordinamento non è stabile, paginare significa perdere o duplicare
device fra le pagine — e in Home Assistant un device perso è un device che sparisce dalla
dashboard dell'utente, con le sue automazioni.

**Serve.** (a) L'ordinamento è deterministico (per `serial_number`? per `created_at`?) o dipende
dal piano di query? (b) Si può filtrare o ordinare (per `type`, `building_slug`)? (c)
`page_size: 200` è un limite duro?

**Risposta backend:**
> _(da compilare)_

---

### D-20 · 🟠 · 🟨 · Quanti device può vedere un utente

**Domanda.** Quanti dispositivi restituisce in pratica `GET /data/devices` per un utente B2C
tipico, per un admin B2B tipico, e per un `superadmin`?

**Cosa sappiamo già.** L'esempio riporta `total: 150` su tre pagine. `superadmin` è un gruppo
Cognito effettivo che bypassa l'appartenenza al dominio.

**[agg. 2026-09-09]** Con `page_size=200` e 50 rps condivisi per stage (→ D-21), il caso da
capire è il `superadmin`: se una lista completa richiede decine di pagine, un solo ciclo di
polling di una sola installazione consuma una frazione significativa del tetto condiviso con
l'app mobile.

**Serve.** (a) Gli ordini di grandezza per i tre profili. (b) La vostra preferenza: un
`superadmin` deve poter usare l'integrazione, o è un profilo che escludiamo esplicitamente con un
messaggio chiaro nel config flow? (c) Se `domain_prefix` è obbligatorio nella pratica (→ D-03),
il problema si riduce al numero di device per dominio: qual è il massimo realistico?

**Risposta backend:**
> _(da compilare)_

---

### D-21 · 🔴 · ✅ · Rate limit

**Domanda.** Quali sono i limiti effettivi (per utente, per IP, per token) e quelli del pool
Cognito? Se non esistono ancora, quale numero possiamo assumere?

**Risposta backend (2026-09-09).**

**(a) I numeri esistono, e sono più bassi del previsto.**

| Ambito | Limite | Fonte |
|---|---|---|
| API Data e Analytics | **50 req/s steady, 100 burst** | `modules/data-analytics/variables.tf:252-263` |
| API device-setup | 50 req/s, 100 burst | `modules/device-setup/main.tf:2336-2337` |
| API firmware (con API key) | 2000 req/s, 5000 burst | `infrastructure/terraform/main.tf:757-770` |
| `page_size` su `/data/devices` | max 200 | swagger |
| `POST /data/devices/status` | max 100 serial per richiesta | swagger |
| Job storici concorrenti | 3 per utente | `max_active_jobs_per_user` |

Nessun ambiente sovrascrive i default nei `.tfvars`: **50/100 vale anche in produzione.**

Tre precisazioni che cambiano il quadro:
* **Il limite è per STAGE, non per utente né per IP.** È un tetto *condiviso* fra app mobile, web
  e ogni integrazione. Non protegge da noi stessi, e il nostro traffico compete con quello
  dell'app.
* **Nessun WAF.** Nessuna risorsa `aws_wafv2_*` in tutta la nuova architettura (né be-core, né
  iot-core, né devops-core-workflows). Quindi nessun rischio che lo User-Agent venga bloccato —
  ma neanche alcuna protezione per origin IP. I limiti WAF citati riguardano il perimetro legacy.
* **50 rps è stretto per la scala ipotizzata.** Anche con il pattern a 1 richiesta, 1.000
  installazioni a 60 s fanno ~17 rps di media, e i picchi si sommano a tutto il resto.

**(b) 60 s è il minimo giusto, ma la motivazione era sbagliata.** Il TTL di 60 s è sulla **riga
device di Aurora**, non sulle letture di telemetria: la telemetria arriva dalla **telemetry-cache
DynamoDB, scritta dal Silver Processor a ogni ingest, senza TTL**
(`data-analytics/handler.py:582-585`). Pollare a 45 s *produrrebbe* dati nuovi. Il pavimento vero
è la **cadenza del dispositivo: un messaggio al minuto** (→ D-18).

**Default a 5 minuti: confermato**, ed è la scelta prudente dati i 50 rps condivisi. **Aggiungete
jitter**: migliaia di installazioni a 300 s esatti si allineano sui minuti tondi e trasformano un
carico medio accettabile in picchi periodici.

**(c) Sì**, `GET /data/devices` con telemetria inline è il pattern da preferire (→ D-04).

**Due dettagli operativi:**
* **Nessun `Retry-After` sui 429**, e il 429 non è dichiarato nello swagger: lo genera API
  Gateway, non l'applicazione, con il corpo standard `{"message": "Too Many Requests"}`.
  Trattatelo come «salta questo ciclo», con backoff esponenziale + jitter da ~5 s — non come
  «riprova subito», che peggiora la saturazione.
* **Finestra di ricerca della telemetria: 6 h** (`influxdb_list_window` / `influxdb_latest_window`
  su int e prod). `GET /data/devices` **non allarga** su miss → `telemetry: null` per un device
  silente da più di 6 h; `GET /data/devices/{deviceId}` **allarga**. Due chiamate possono
  legittimamente dare risposte diverse sullo stesso device — usate `connection_status` per la
  disponibilità delle entità, non l'assenza di telemetria.

**Cosa ne facciamo.**
* Intervallo di polling: **minimo 60 s**, **default 300 s**, con **jitter** all'avvio e su ogni
  ciclo. Lo scriviamo nella documentazione pubblica dell'integrazione.
* Sui 429: salta il ciclo, backoff esponenziale con jitter partendo da ~5 s, nessun retry
  immediato. Nessuna attesa di `Retry-After` (→ D-22, chiusa).
* Disponibilità su `connection_status` (→ D-16, D-17).

**Un punto che vi rimandiamo, perché è vostro e non nostro.** Con **50 rps per stage condivisi
con l'app mobile** e nessun limite per utente o per IP, un'integrazione distribuita
pubblicamente su Home Assistant è un rischio per voi: noi possiamo essere educati (1 richiesta
ogni 5 minuti, con jitter), ma non possiamo impedire a un utente di configurare 60 s né a
un'installazione difettosa di ripetere. **Vi conviene un usage plan dedicato con una API key per
l'integrazione**, o almeno un limite per utente: è la protezione che manca, e la nostra
diffusione pubblica è esattamente l'evento che la rende necessaria. Se decidete di farlo, la
chiave la gestiamo noi lato client — ma è una vostra decisione di infrastruttura e va presa prima
del rilascio, non dopo il primo picco.

---

### D-22 · 🟠 · ✅ · Cosa succede quando si supera un limite

**Domanda.** Superato un limite, la risposta è un 429? Con un header `Retry-After`?

**Risposta backend (2026-09-09).** **429 senza `Retry-After`.** Non è dichiarato nello swagger
perché lo genera **API Gateway**, non l'applicazione, con il corpo standard
`{"message": "Too Many Requests"}`. Va trattato come «salta questo ciclo», con backoff
esponenziale + jitter da ~5 s. Nessun WAF in arch 2.0, quindi nessun blocco per origin IP
(→ D-21, D-31).

**Cosa ne facciamo.** Implementato come indicato: nessuna dipendenza da `Retry-After`, nessun
retry immediato, backoff con jitter, e il ciclo salta senza marcare le entità come non
disponibili (un 429 non è un device offline).

**Residuo minimo.** Il 429 di API Gateway è per **stage**: significa che una saturazione causata
dall'app mobile si manifesta a noi come 429, e viceversa. Confermate? Cambia solo il messaggio
che scriviamo nei log, non il comportamento.

---

## Sezione 6 — Dati storici

### D-23 · 🟠 · 🟨 · Aggregati e serie storiche in arch 2.0

**Domanda.** In arch 2.0 esiste un endpoint per leggere **aggregati** o **serie storiche** per
device?

**Cosa sappiamo già.** I due endpoint passati sono entrambi "latest".
**[agg. 2026-09-09] Ma dalla risposta a D-21 sappiamo che qualcosa esiste:** la tabella dei
limiti include *"Job storici concorrenti — 3 per utente (`max_active_jobs_per_user`)"*. Quindi
c'è un'API di **job asincroni per lo storico**, di cui non abbiamo né path né contratto. Sappiamo
anche che lo storico migrato è partizionato per device type e che il radon copre il 78,8% delle
righe.

**Perché ci serve.** Due motivi concreti:
1. il client arch 1.x creava **entità separate per i valori aggregati**; con la migrazione le
   eliminiamo, e vogliamo conferma che il concetto non torni;
2. Home Assistant ha statistiche a lungo termine: al primo setup potremmo **importare lo storico**
   invece di partire da zero. Per voi è un accesso una volta sola, non un polling — e un job
   asincrono è esattamente la forma giusta per farlo.

**Serve.** (a) Path e contratto dell'API di job storici, e se è utilizzabile da un utente normale.
(b) Granularità disponibili (10 min / orario / giornaliero?) e finestra massima — sappiamo di un
limite di 30 giorni per invocazione in un altro contesto. (c) Il **tipo** di aggregazione (media?
ultimo valore? massimo?): senza, non possiamo etichettare correttamente le statistiche di Home
Assistant, che distingue `mean` da `sum` da `max`. (d) Il limite di 3 job concorrenti per utente
è compatibile con un import iniziale su un account con molti device?

**Risposta backend:**
> _(da compilare)_

---

## Sezione 7 — Autenticazione e sessione

### D-24 · 🟠 · 🟨 · Arch 2.0 usa lo stesso user pool e lo stesso app client?

**Domanda.** L'autenticazione cambia con la migrazione? E qual è la durata reale del token?

**Cosa sappiamo già.** Entrambi gli swagger dichiarano `security: [CognitoAuth]` senza dire quale
pool. Sull'attuale abbiamo misurato `ExpiresIn` = **86400 s (24 h)** in produzione; su INT è stato
riportato "~1 h".

**[agg. 2026-09-09]** Sappiamo che l'API Gateway 2.0 monta un authorizer `COGNITO_USER_POOLS`
(`modules/data-analytics/main.tf:530-534`), ma non quale pool: è esattamente il punto della
domanda.

**Perché ci serve.** Se il pool o l'app client cambiano fra 1.x e 2.0, **le sessioni persistite
non valgono sull'altro host** e ogni installazione richiede di nuovo le credenziali. Dato che 1.x
e 2.0 convivono su host diversi (→ D-01), questo è anche il fattore che decide se possiamo fare
un rilascio graduale o no.

**Serve.** (a) Stesso user pool e stesso app client per `v2.api.iot.radoff.life` e per l'host
attuale? (b) Se no: quale pool e quale client id per 2.0, per ambiente? (c) La divergenza 24 h /
1 h fra produzione e INT è intenzionale? Ci serve il valore di produzione, che scriviamo nel
README.

**Risposta backend:**
> _(da compilare)_

---

### D-25 · 🔴 · ✅ · Rotazione dei refresh token

**Domanda.** La rotazione dei refresh token sull'app client Cognito è una scelta deliberata?

**Cosa sappiamo già.** Ricavato da noi, con due scoperte:
1. la rotazione **è abilitata**, il che rende `InitiateAuth`/`REFRESH_TOKEN_AUTH` inutilizzabile
   (`UnsupportedOperationException: This API does not support refresh token rotation`); abbiamo
   dovuto passare a `GetTokensFromRefreshToken`;
2. un refresh token **appena emesso** viene rifiutato (`NotAuthorizedException: Invalid Refresh
   Token`) circa 60 secondi dopo il login SRP che lo ha emesso — in modo **ripetibile**.
   Coerente con una limitazione AWS nota e non risolta (`aws/aws-sdk-js-v3#7162`).
*Fonte: verifiche su account reale 2026-09-04 e 2026-09-07, card S-12.*

**Perché ci serve.** Con il refresh di fatto inutilizzabile, ogni rinnovo ricade su un handshake
SRP completo: molto più traffico verso Cognito del necessario, proprio sul flusso più soggetto a
throttling. È indipendente dall'architettura dell'API, quindi la migrazione non lo risolve — e
con i 50 rps condivisi di D-21 il traffico in eccesso pesa più di quanto pensassimo.

**Non ci serve un'informazione, ci serve una decisione.** La rotazione è deliberata? Se non lo è,
la disattivate — o ci autorizzate a chiederne la disattivazione su un app client dedicato (→
D-28)?

**Risposta backend:**
> _(da compilare)_

---

### D-26 · 🟠 · ⬜ · Lifetime della catena di refresh token

**Domanda.** Qual è la durata della catena di refresh token per questo app client?

**Cosa sappiamo già.** Non verificabile da noi; il default Cognito è 30 giorni.

**Perché ci serve.** Determina in quanto tempo Home Assistant si accorge che l'utente ha cambiato
password e gli chiede di reinserirla: è un numero che dobbiamo scrivere nella documentazione
dell'integrazione, e oggi lo formuliamo in modo vago.

**Risposta backend:**
> _(da compilare)_

---

### D-27 · 🟠 · 🟨 · Percorso di autenticazione per client di terze parti

**Domanda.** Esiste o è previsto qualcosa di più adatto (OAuth2 device flow, personal access key
1:1 con l'utente) rispetto al riuso delle credenziali dell'app mobile?

**Cosa sappiamo già.** È già stato proposto nella vostra analisi dell'integrazione (*"provides
users with personal access keys ... strict 1:1 binding between the Radoff User ID and the AWS API
Key"*), senza design né data. **E arch 2.0 non lo introduce:** entrambi gli swagger dichiarano
solo `CognitoAuth`.

**[agg. 2026-09-09]** La risposta a D-21 dà a questa domanda un secondo movente, più forte del
primo: senza limiti per utente e con 50 rps condivisi, **una API key per l'integrazione servirebbe
a voi** per attribuire e limitare il nostro traffico, non solo a noi per non chiedere la password
all'utente. Le due esigenze si risolvono con lo stesso meccanismo.

**Perché ci serve.** È il punto che oggi ci obbliga a chiedere email e password Radoff e a
conservare un refresh token nella configurazione di Home Assistant — la prima cosa che verrà
contestata in una review pubblica. Se è pianificato anche solo a medio termine, progettiamo il
config flow per poterlo aggiungere senza rompere le installazioni esistenti: ci basta sapere che
arriverà.

**Risposta backend:**
> _(da compilare)_

---

### D-28 · 🟠 · ✅ · App client Cognito dedicato

**Domanda.** Valutate un app client **dedicato** all'integrazione, ristretto al solo SRP e
separato da quello dell'app mobile?

**Cosa sappiamo già.** App client attuale, ispezionato via AWS CLI: nessun client secret,
self-registration disabilitata (`AllowAdminCreateUserOnly: true`),
`ExplicitAuthFlows = [ALLOW_CUSTOM_AUTH, ALLOW_USER_SRP_AUTH]`. Sullo stesso client è però
abilitato anche il flusso OAuth hosted-UI (`code` + `implicit`, con callback su localhost e su
jwt.ms).
*Fonte: card T-01.*

**Perché ci serve.** Un client separato permetterebbe di ruotare o irrigidire l'uno senza toccare
l'altro, di disattivare la rotazione dei refresh token solo per noi (→ D-25), e di darvi metriche
separate sul nostro traffico (che con i 50 rps condivisi di D-21 vi serve). Se la risposta è sì,
**la migrazione è il momento giusto**: il client id è un valore che stiamo comunque per cambiare
insieme all'host (→ D-24).

**Risposta backend:**
> _(da compilare)_

---

## Sezione 8 — Errori e diagnostica

### D-29 · 🟠 · 🟨 · Tassonomia degli errori e forma di `ErrorResponse`

**Domanda.** Qual è la forma completa e stabile del body d'errore, e la semantica esatta di 401,
403 e 404?

**Cosa sappiamo già.** Dagli swagger:
* **401** e **403** sono response distinte su `/data/devices`, e il 403 ha una causa precisa:
  `domain_prefix` di un dominio a cui il chiamante non appartiene (senza essere `superadmin`).
  403 ⇒ chiedere una riconfigurazione, non ritentare;
* **404** su `/analytics/.../latest` significa *"no InfluxDB data and no `device_type` param"* —
  **non** "device inesistente": un device nuovo che non ha ancora mandato nulla dà 404;
* il body segue `ErrorResponse`, di cui conosciamo **un solo campo**, `statusCode`;
* i fallimenti delle dipendenze soft (DynamoDB, Aurora) **non** producono 5xx: producono campi
  `null`.

**[agg. 2026-09-09] Due tessere in più.** Il **429** ha un corpo diverso da `ErrorResponse`
(`{"message": "Too Many Requests"}`, generato da API Gateway) — quindi il body d'errore **non è
uniforme**: dobbiamo saper leggere entrambe le forme. E `measures-ranges` con un `device_type`
ignoto risponde **404 con un campo `available`** che elenca i tipi validi: un'altra forma ancora.

**Perché ci serve.** Il client deve reagire in modo diverso a "credenziali scadute" (rinnova),
"accesso al dominio revocato" (chiedi riconfigurazione), "device rimosso" (rimuovi le entità),
"limite superato" (salta il ciclo) e "vostro problema transitorio" (ritenta).

**Serve.** (a) La forma completa di `ErrorResponse`: c'è un codice applicativo oltre a
`statusCode`? un `message`? È stabile? (b) Quante forme di body d'errore esistono in tutto — ne
abbiamo già viste tre. (c) 401 e 403 sono usati coerentemente su tutti gli endpoint?
(d) **Il punto più importante:** un `null` da degrado di dipendenza è distinguibile da un `null`
da dato assente? Senza distinzione, un vostro problema transitorio su DynamoDB ci fa marcare
entità come non disponibili — o peggio, se il degrado tocca lo schema, ci fa cancellare e
ricreare entità. Se non è distinguibile nel payload, ci basta un header o un campo
`degraded: true`.

**Risposta backend:**
> _(da compilare)_

---

### D-30 · 🟠 · ⬜ · Header di request id

**Domanda.** Qual è il nome esatto dell'header di request id nelle risposte dell'API
(`x-request-id`, `x-amzn-requestid`, altro)?

**Cosa sappiamo già.** Nulla di confermato, e gli swagger non documentano header di risposta. Il
client prova i nomi più comuni usati dalle API dietro AWS e logga il primo che trova, altrimenti
`n/a`.

**[agg. 2026-09-09]** Sappiamo che davanti a tutto c'è API Gateway, quindi `x-amzn-RequestId` e
`x-amz-apigw-id` sono i candidati probabili — ma ci serve sapere **quale dei due compare nei
vostri log**, perché è quello che ha senso che l'utente vi riporti.

**Perché ci serve.** Lo logghiamo per correlare le segnalazioni degli utenti con i vostri log: è
la differenza fra "non funziona" e una riga di log che potete cercare.

**Risposta backend:**
> _(da compilare)_

---

### D-31 · 🟠 · 🟨 · Riconoscimento dello User-Agent

**Domanda.** Il backend può riconoscere e segmentare il nostro User-Agent
`HomeAssistant-Radoff/<versione>` (metriche, quote dedicate, diagnostica)? E il WAF non lo
blocca?

**[agg. 2026-09-09] Metà risposta acquisita, ed è quella che ci preoccupava.** Dalla risposta a
D-21: **nessun WAF in arch 2.0** — nessuna risorsa `aws_wafv2_*` in nessuno dei repo. Quindi
nessun rischio che il nostro User-Agent venga bloccato. I limiti WAF di cui avevamo notizia
riguardano il perimetro legacy, che non useremo.

**Resta la parte positiva della domanda.** Volete/potete **segmentare** il traffico per
User-Agent (o meglio, per API key dedicata, → D-27)? Vi darebbe gratuitamente il numero di
installazioni Home Assistant attive e il carico che generano — che con i 50 rps condivisi di
D-21 è un dato che vi serve per dimensionare. A noi serve per capire, in caso di problemi, se
sono nostri o di sistema.

**Risposta backend:**
> _(da compilare)_

---

### D-32 · 🟠 · ⬜ · Utente senza device

**Domanda.** Cosa restituisce `GET /data/devices` per un utente autenticato che non ha nessun
dispositivo (o nessun dominio)? Un **200** con `devices: []`, o un **403**?

**Cosa sappiamo già.** Nulla di verificato: non disponiamo di un account in queste condizioni.

**Perché ci serve.** È un caso che il config flow deve intercettare con un messaggio chiaro
("questo account non ha dispositivi Radoff") invece di un errore generico o di un'installazione
vuota che sembra rotta. Con la migrazione lo stiamo riscrivendo: è il momento di farlo bene.

**Risposta backend:**
> _(da compilare)_

---

## Sezione 9 — Modello del dispositivo e scrittura

### D-33 · 🟡 · ⬜ · LIFE come attuatore e la nidificazione `controller_of_device`

**Domanda.** Un LIFE "slave (actuator)" non è restituito come elemento top-level ma annidato nel
blocco `controller_of_device` del suo controller, con `managed_by_device_serial` a puntare
indietro. Quindi:
1. Il LIFE è un **attuatore**: espone comandi (accensione, ventilazione, set-point)? Attraverso
   quale endpoint? Se sì, in Home Assistant diventa un `switch`/`fan`/`climate` e sarebbe il
   primo caso di **scrittura** verso la vostra API — da progettare a parte, con le sue domande su
   idempotenza, conferma dello stato e latenza.
2. La relazione è sempre 1:1 (`controller_of_device_serial` è singolare) o un controller può
   pilotare più slave?
3. Un LIFE annidato ha `telemetry: null` nell'esempio: ha telemetria propria o no? (Nel catalogo
   di D-13 `life` è un device type a tutti gli effetti.)
4. Come lo rappresentiamo? Un solo device Home Assistant con le entità di entrambi, o due device
   collegati da `via_device`? La seconda ci sembra più corretta — confermate?

**Perché ci serve.** Non blocca il primo rilascio (partiamo dai sensori), ma la scelta su come
modellare la coppia controller/slave è difficile da cambiare dopo: cambiarla significa
ricostruire i device nel registro dell'utente.

**Risposta backend:**
> _(da compilare)_

---

### D-34 · 🟡 · ⬜ · Metadati del device: stanza, edificio, posizione, icone

**Domanda.** I metadati del modello device sono stabili e utilizzabili per costruire il device
model di Home Assistant?

**Cosa sappiamo già.** Il modello espone `name`, `room_slug`/`room_name`,
`building_slug`/`building_name`, `domain_prefix`, `latitude`/`longitude`, `firmware_version`,
`created_at`/`updated_at`, `icon_color_variant_id`, `color`, `icon_image`. Il nostro piano:
* `name` → nome del device; `firmware_version` → `sw_version`;
* `room_name` (o `building_name / room_name`) → suggerimento per l'**area** di Home Assistant,
  proposto all'utente al primo setup;
* `latitude`/`longitude` → attributi, non entità;
* `color`/`icon_image`/`icon_color_variant_id` → **ignorati**: Home Assistant usa le proprie
  icone e non possiamo caricare asset da un host esterno.

**Serve.** (a) Conferma che il piano non contraddica come presentate le cose nell'app.
(b) `room_name` e `building_name` possono essere `null`? (Presumiamo sì: il degrado di Aurora
azzera l'intero blocco.) (c) `name` è modificabile dall'utente nell'app? In quel caso è solo
un'etichetta iniziale, non un identificativo (→ D-02). (d) `latitude`/`longitude` sono la
posizione del device o del suo edificio? Se sono precise e riferite a un'abitazione, preferiamo
non esporle affatto.

**Risposta backend:**
> _(da compilare)_

---

### D-35 · 🟠 · ⬜ · `POST /data/devices/status` *(nuova in v5)*

**Domanda.** Nella tabella dei limiti di D-21 compare un endpoint che non conoscevamo:
`POST /data/devices/status`, con un massimo di **100 serial per richiesta**. Cos'è e cosa
restituisce?

**Cosa sappiamo già.** Solo l'esistenza e il limite di 100 serial, dalla vostra risposta a D-21.

**Perché ci serve.** Se restituisce `connection_status` (o lo stato di freschezza) per un batch di
serial, potrebbe essere l'endpoint giusto per la **disponibilità** delle entità: leggero, in
batch, e proprio ciò che ci indicate di usare al posto dell'assenza di telemetria (→ D-16, D-17).
In quel caso il nostro pattern diventerebbe: `GET /data/devices` per i valori a cadenza lenta,
`POST /data/devices/status` per la disponibilità a cadenza più rapida — o l'opposto, se lo
status è più economico.

**Serve.** (a) Cosa restituisce esattamente. (b) È accessibile a un utente normale? (c) Come si
relaziona a `connection_status` del modello device: è la stessa informazione? (d) Ha senso che
lo usiamo nel modo descritto, o ha un'altra finalità (provisioning, diagnostica)?

**Risposta backend:**
> _(da compilare)_

---

## Appendice A — Corrispondenza con le numerazioni precedenti

| v5 | v4 | v3 | Nota |
|---|---|---|---|
| D-01 … D-34 | identici alla v4 | vedi tabella v4 | numerazione **invariata** dalla v4 |
| D-35 | — | — | nuova in v5: `POST /data/devices/status` |

La mappatura v4 → v3 resta valida e non è ripetuta qui: la v4 aveva riordinato e accorpato la v3
(34 domande da 42), la v5 mantiene la stessa numerazione e aggiunge solo D-35.

---

## Appendice B — Domande eliminate con la migrazione

Cadono perché riguardano contratti che non useremo mai in produzione.

| Domanda (v3) | Perché cade |
|---|---|
| Fattore di scala `0.00835` sulla temperatura | I valori arrivano nell'unità dichiarata (D-05 confermato). Fattore **rimosso dal client** — ed era `1/120`, ereditato dal `TEMPERATURE_MOLTIPLICATOR_FACTOR` legacy: origine finalmente nota |
| `aggregatedData` vs `data` | I bucket non esistono in arch 2.0: un solo blocco `telemetry`. Le entità "average" vengono **eliminate**. Il tema storico resta → D-23 |
| Terzo bucket `recalculatedData` | Non esiste in arch 2.0 e non lo consumavamo. Chiusa |
| Formato di `x-domain` | **L'header non esiste in arch 2.0.** Resta come lo scopriamo → D-03 |
| I claim `d_<domain-uuid>` sono un contratto? | Il client **smette di decodificare i claim**: `GET /data/devices` funziona col solo bearer token |
| Endpoint di discovery senza `x-domain` | **Risolta**: `GET /auth/user/me/domains` esiste anche in arch 2.0 e non richiede header di dominio. È il primo passo del config flow → D-03 |
| Un claim `d_*` per ogni dominio | Dipendeva dai claim, che non usiamo più |
| Comportamento multi-dominio di `/auth/user/me/domains` | Endpoint di arch 1.x. Il modello 2.0 è appartenenza + 403 |
| Futuro di `parentDomainId` | Il modello device 2.0 ha solo `domain_prefix`, piatto. Chiusa |
| `x-domain` fra i domini della discovery | Non c'è `x-domain`. Regola equivalente in D-29 |
| Valore autoritativo di `deviceTypeName` (`Now+`) | Il campo è `type`, e `Now+` → **`nowplus`** (D-13 confermato) |
| Accesso a `GET /admin/firmware-type` | Non serve: lo schema arriva da `measures-ranges` (D-04) |
| Limite di `take` su `/data/devices/search` | Endpoint di arch 1.x. In 2.0 `page`/`page_size` (max 200). Resta l'ordinamento → D-19 |
| Header `Retry-After` sui 429 | **Risposto: non c'è** (D-22). Chiusa |
| Il WAF blocca il nostro User-Agent? | **Risposto: non c'è WAF** in arch 2.0 (D-21, D-31). Chiusa la parte di rischio |

---

## Appendice C — Discrepanze fra swagger, codice e risposte

Disallineamenti dentro la fonte di verità, o fra la fonte di verità e il codice. I punti 11–13
sono nuovi in v5 e nascono dal confronto fra le risposte del backend e gli swagger.

| # | Discrepanza | Verdetto | Dove |
|---|---|---|:--:|
| 1 | `type`: `sense`/`sismoff` vs `radoff-sense`/`radoff-life`/`radoff-city` | **risolta**: il prefisso `radoff-` è il refuso, la forma giusta è senza prefisso | D-13 |
| 2 | `pressure`: `unit: "Pa"` ma esempio `1012.8` | **risolta**: `Pa` è giusto, l'esempio è il refuso | D-06 |
| 3 | Identificativo con tre nomi: `deviceSerial`, `deviceId`, `serial_number` | **risolta**: sono la stessa cosa. Resta il dubbio sul prefisso `RADOFF-` negli esempi, come al punto 1 | D-02 |
| 4 | `tvoc`: valore `0.132` con soglie `100/200/300/400` | **aperta**; escluso che sia un fattore di scala (`scale_factors = {}`) | D-07 |
| 5 | `pm10`: `excellent upperBound: 200`, poi `high ≤30` | **aperta**; ora che leggiamo i `ranges` a runtime finisce sotto gli occhi dell'utente | D-09 |
| 6 | `radon_status`: `dataType: "string"` ma valore `2` | **aperta** | D-10 |
| 7 | Livelli `excellent / high / good / poor / terrible`, con `high` secondo | **risolta: è intenzionale**, `high` sta fra `excellent` e `good` | D-09 |
| 8 | `/analytics/.../latest` non elenca 401 né 403 | **risolta, ed era un bug**: nessun controllo di dominio nell'implementazione, in fase di fix | D-04 |
| 9 | Nessuna response 429 documentata | **risolta**: la genera API Gateway, corpo `{"message": "Too Many Requests"}`, senza `Retry-After` | D-22 |
| 10 | `GET /data/devices/{deviceId}` citato ma non specificato | **risolta**: è in `yama-be-core-platform/swagger.yaml` (~riga 1982), ci mancava il file | D-01 |
| 11 | **[NEW]** `sismoff` e i PM | La risposta a D-13 dice che il `sismoff` **non ha PM1/2.5/10**, ma l'esempio `sismoff_device` dello swagger li elenca in `field_metadata` **e** in `telemetry` (`pm10: 18.4, pm25: 12.1, pm1: 6.8`) | D-14 |
| 12 | **[NEW]** AQI e il divisore 120 | `aqi_calculator.py` divide la temperatura per 120 (fedeltà al legacy) mentre `internal_temperature` arriva già in °C: la componente temperatura dell'AQI è sistematicamente sbagliata, e il commento nel codice lo ammette | D-08 |
| 13 | **[NEW]** Cache a 60 s | Lo swagger analytics presenta il TTL di 60 s come se assorbisse le letture di telemetria ("absorbs hot serials"); in realtà è sulla **riga device di Aurora**, e la telemetria viene dalla telemetry-cache DynamoDB **senza TTL**. Ci aveva portato a una conclusione sbagliata sul minimo di polling | D-21 |
| 14 | **[NEW]** `measures-ranges` e `/auth/user/me/domains` non sono negli estratti | I due endpoint su cui si regge il client — lo schema e la discovery del dominio — non compaiono in nessuno dei due swagger che avevamo: li abbiamo scoperti dalle risposte a D-04 e D-03. È la ragione per cui chiediamo lo `swagger.yaml` completo | D-01, D-03, D-11 |
| 15 | **[M-01]** Il path della discovery in D-03 è quello di arch 1.x | La risposta a D-03 indica `GET /auth/user/me/domains`. Su arch 2.0 quel path **non esiste** (403 `MissingAuthenticationTokenException`, nessun preflight `OPTIONS`): la discovery è **`GET /data/user/me/domains`** (swagger `yama-be-core-platform`, `getUserDomains`). Verificato su dev il 2026-09-10, risponde 200 col solo bearer token. La response è **paginata** (`page_size` default 20, max 100) e annida il dominio: `domains[].domain.prefix` | D-03 |
| 16 | **[M-01]** L'app client Cognito di dev non ha il flusso SRP abilitato | Login sul pool dev (`eu-west-1_5SsvW9t6S`, client `2i63gbc9sim3b8paasaga7jb6g`) rifiutato con `InvalidParameterException: USER_SRP_AUTH is not enabled for the client`. Enumerati i flussi: dev ha `USER_PASSWORD_AUTH` + `REFRESH_TOKEN_AUTH` ma **non** SRP; prod ha SRP + refresh ma non password. A dev manca il flusso che l'integrazione usa | D-28 |
| 17 | **[M-01]** `pm10 excellent upperBound`: 200 nello swagger, **20** a runtime | Misurato il 2026-09-10 su `measures-ranges`: la scala reale è 20 / 30 / 40 / 50, coerente con `high ≤ 30`. Il `200` dell'esempio è il refuso. **Chiude la voce 5** | D-09 |
| 18 | **[M-01]** Il `sismoff` **non** espone i PM | `measures-ranges?device_type=sismoff` non contiene `pm1`/`pm25`/`pm10`. Conferma la risposta a D-13 e smentisce l'esempio `sismoff_device` dello swagger. **Chiude la voce 11** | D-14 |
| 19 | **[M-01]** Il tipo `life` esiste nei device ma non nello schema | `measures-ranges?device_type=life` risponde 404, e `available` elenca `city, now, nowplus, sense, sismoff`. Ma device di tipo `life` **esistono** in `/data/devices`. D-13 include `life` nell'enumerazione di `type` | D-13, D-14 |
| 20 | **[M-01]** Il `domain_prefix` non è human-readable | D-03 lo dà per leggibile (`radoff-hq`) e quindi usabile come etichetta nel menu di scelta. Su dev, **14 domini su 15** hanno come `prefix` un frammento di UUID (`1aeb7ad1`, `875fe89b`); uno solo è `radoff-cattolica`. È il problema di arch 1.x che D-03 dava per superato. Il campo `name` è invece leggibile: il config flow mostrerà quello | D-03 |
| 21 | **[M-01]** Campi non documentati nella discovery | Ogni dominio porta un `type` (`customer`/`business`/`unknown`) che lo swagger non dichiara, e `assigned_at` è sempre `null` | D-03 |

**Le voci successive alla 14 arrivano dalla ricognizione su dev.** La card
**M-01** (vedi [`M-01-ricognizione-dev.md`](M-01-ricognizione-dev.md)) confronta
i payload reali di `v2.api.dev.iot.radoff.life` con quanto dichiarato negli
swagger: ogni divergenza trovata si aggiunge qui e all'elenco dei refusi di
T-08 §8. Finché quella ricognizione non è eseguita la tabella resta a 14 voci —
i punti 5, 6 e 11 sono quelli che M-01 chiude per primi (rispettivamente
`pm10 excellent upperBound`, il tipo di `radon_status`, e i PM del `sismoff`).

---

## Appendice D — Cosa cambia nel client con la migrazione

Aggiornata con le decisioni prese grazie alle risposte del 2026-09-09.

| Area | Prima (arch 1.x) | Dopo (arch 2.0) | Stato |
|---|---|---|:--:|
| Host | host attuale, costante nel codice | **`v2.api.dev.iot.radoff.life`** per lo sviluppo; base URL come **parametro di configurazione**, non costante | ✅ deciso |
| Autenticazione | SRP Cognito + header `x-domain` | SRP Cognito, nessun header di dominio | 🟨 pool da confermare (D-24) |
| Config flow | credenziali → claim `d_*` → scelta del dominio | credenziali → `GET /auth/user/me/domains` → scelta solo se >1 dominio | ✅ deciso |
| Discovery | `POST /data/devices/search` con `take: 99` | `GET /data/devices?page_size=200`, ciclo su `total_pages` | ✅ deciso |
| Polling | 1 `search` + 1 `GET` per device, a ogni ciclo | **1 sola** `GET /data/devices` con telemetria inline | ✅ deciso |
| Intervallo | min 30 s, default 60 s | **min 60 s, default 300 s, con jitter** | ✅ deciso |
| Schema (unità/soglie) | tabelle hardcodate in `properties.py`/`sensor.py` | `GET /analytics/measures-ranges?device_type=` al setup, in cache | ✅ deciso |
| Livelli qualitativi | `excellent/good/medium/poor/terrible` | `excellent/high/good/poor/terrible`, ordinati per `pos` (sparso!) | ✅ deciso |
| Temperatura | `valore × 0.00835` | valore così com'è | ✅ deciso |
| Pressione | Pa | Pa (`native_unit_of_measurement`, conversione lasciata alla UI) | ✅ deciso |
| Modello dei dati | tre bucket, namespace piatto, entità "average" separate | un blocco `telemetry`, entità singole | ✅ deciso |
| Filtro sui tipi | solo `Now+`, gli altri scartati **senza log** | **nessun filtro**; `type` solo come chiave di cache, WARNING sui tipi ignoti | ✅ deciso |
| Radon | non disponibile | `radon_bqm3` + `radon_status` su `sense`/`city`/`sismoff` — **non** su `now`/`nowplus` | 🟨 enum di `radon_status` (D-10) |
| Disponibilità | `lastDataReceivedAt` × moltiplicatore arbitrario (3×) | **`connection_status`**, non `telemetry: null` | 🟨 enum e cadenza (D-17) |
| 429 | retry con backoff 0.5, 3 tentativi | salta il ciclo, backoff esponenziale + jitter da ~5 s | ✅ deciso |
| `unique_id` delle entità | id device arch 1.x + bucket + proprietà | **`<serial_number>_<nome_campo>`**, serial usato verbatim | ✅ deciso |
| Device di sviluppo | Now+ | Now+ **non ha l'hardware radon**: serve un `sense` o `city` su **dev** per validare il radon | ⬜ richiesta operativa |

---

## Appendice E — Testo di chiusura per la card T-02

Bozza del commento con cui chiudiamo **T-02**, mappata sulla numerazione originale della card.
Ogni riga è chiusa in uno di tre modi: **risposta** (l'abbiamo), **superata** (la migrazione ad
arch 2.0 la rende priva di oggetto), **trasferita** (resta viva, ma in una card di follow-up).

> **T-02 — chiusura.** Le 13 domande della card sono state tutte lavorate. La migrazione diretta
> del client all'architettura 2.0 ha reso prive di oggetto sette di esse; le altre hanno una
> risposta dal team backend, riportata per esteso in `docs/T-02-domande-backend.md` (v6). I
> residui, tutti non bloccanti, sono elencati in fondo e vanno in una card di follow-up.

| # orig | Domanda originale | Esito | Risposta |
|:--:|---|:--:|---|
| **#1** | `PARENT_DOMAIN` è stabile o va superato? `/auth/user/me/domains` richiede un `x-domain` noto a priori? Esiste una discovery? | **superata + risposta** | `parentDomainId` non esiste nel modello 2.0 (solo `domain_prefix`, piatto) e la costante è già stata rimossa dal client. La discovery si fa con **`GET /auth/user/me/domains`**, che in 2.0 **non richiede header di dominio**: è il primo passo del config flow |
| **#2** | Il payload include un timestamp per campione, e con quale fuso? | **risposta** | **No** per campione. C'è `telemetry.timestamp`, ISO-8601 **UTC**, definito come il *massimo* dei timestamp dei singoli campi: i campi possono provenire da messaggi diversi. Cadenza del device: **1 messaggio al minuto**, radon aggregato su almeno 5 minuti. La richiesta di un timestamp per campo resta aperta (D-15) |
| **#3** | Fattore di scala della temperatura; unità di `pressure` e `relative_humidity` | **risposta** | I valori arrivano **già nell'unità dichiarata**: `scale_factors = {}` per tutti i device type, nessuna conversione in lettura. Il fattore `0.00835` era `1/120` (`TEMPERATURE_MOLTIPLICATOR_FACTOR` legacy) ed è **rimosso**. `pressure` in **Pascal** (il `1012.8` degli esempi è un refuso confermato), `relative_humidity` in **%** |
| **#4** | Finestra e tipo di aggregazione di `aggregatedData` vs `data` | **superata** | I bucket non esistono in arch 2.0: un solo blocco `telemetry` con l'ultimo valore per campo. Le entità "average" del client vengono **eliminate**. Aggregati e storico: esiste un'API a job asincroni (3 job concorrenti per utente), contratto da leggere nello swagger completo → D-23 |
| **#5** | Futuro di `parentDomainId`; paginazione di `/data/devices/search` | **superata + risposta** | `parentDomainId` superato (vedi #1). Paginazione arch 2.0: `page` / `page_size` (**max 200**) con `total` e `total_pages`; il `take: 99` fisso — che troncava in silenzio — è **eliminato** |
| **#6** | Rate limit dell'API e del pool Cognito | **risposta** | **50 req/s steady, 100 burst, per stage** (tetto condiviso con app mobile e web), nessun limite per utente né per IP, **nessun WAF** in arch 2.0. I 429 li genera API Gateway, corpo `{"message": "Too Many Requests"}`, **senza `Retry-After`**. Polling del client: **minimo 60 s** (cadenza del device), **default 300 s con jitter** |
| **#7** | Valore autoritativo di `deviceTypeName` | **risposta** | Il campo è `type`, minuscolo, catalogo chiuso **`{sense, now, nowplus, city, life, sismoff}`**; `Now+` → **`nowplus`**. Il prefisso `radoff-` degli esempi è un refuso. Ma il client **non filtra più su `type`**: costruisce le entità dallo schema (`measures-ranges`) e usa `type` solo come chiave di cache, con un WARNING sui tipi ignoti |
| **#8** | Il backend può riconoscere il nostro User-Agent? Il WAF lo blocca? | **risposta parziale** | **Nessun WAF in arch 2.0**, quindi nessun rischio di blocco: era la metà che ci preoccupava. La segmentazione del traffico per User-Agent o API key resta una proposta → D-27, D-31 |
| **#9** | Percorso di autenticazione per client di terze parti | **risposta** | Nessuno: arch 2.0 usa solo `CognitoAuth`. La *personal access key* con binding 1:1 resta una proposta senza design né data → D-27 |
| **#10** | Formato di `x-domain` dopo la migrazione allo schema 2.0 | **superata** | **L'header `x-domain` non esiste in arch 2.0.** Il dominio è `domain_prefix`, human-readable, query param di `GET /data/devices`, da passare sempre e ottenuto da `/auth/user/me/domains`. Nessuna migrazione della configurazione utente è necessaria, perché non rilasciamo mai su 1.x |
| **#11** | Stabilità dei claim `d_<domain-uuid>` | **superata** | Il client **non decodifica più i claim**: `/auth/user/me/domains` e `/data/devices` funzionano con il solo bearer token |
| **#12** | Cardinalità dei domini per ruolo; `superadmin` | **trasferita** | `superadmin` è un gruppo Cognito effettivo che bypassa l'appartenenza al dominio. Gli ordini di grandezza per admin e superadmin restano da quantificare → D-20 |
| **#13** | Vista multi-dominio | **risposta** | L'API la consentirebbe (`domain_prefix` opzionale, ogni device porta il suo), ma il pattern indicato dal backend è passare **sempre** `domain_prefix`: il client fa quindi scegliere un dominio al setup |

**Domande emerse durante l'implementazione e chiuse insieme alla card** (non erano nell'elenco
originale):

| Tema | Esito |
|---|---|
| **Radon assente dalla response** | Esposto come `radon_bqm3` (Bq/m³) e `radon_status`. **`now` e `nowplus` non hanno l'hardware radon**: non è un filtro né un abbonamento. Il radon si vede su `sense`, `city`, `sismoff` |
| **Soglie qualitative** | Servite dall'API in `ranges` e coincidenti con quelle che il client aveva hardcodate. Non stanno più nel codice: si leggono da `GET /analytics/measures-ranges?device_type=<tipo>`. Livelli ufficiali `excellent → high → good → poor → terrible` (`high` sta **fra** excellent e good), ordinati per `pos`, che è **sparso** |
| **Identificativo del device** | `deviceId` == `serial_number` == `deviceSerial`. È la base dell'`unique_id` delle entità |
| **Endpoint di polling** | `GET /data/devices?domain_prefix=…&page_size=200` con telemetria inline: **una richiesta per ciclo** invece di `1 + N`. `/analytics/devices/{serial}/latest` non entra nel percorso normale |
| **Ambiente** | InfluxDB e `/analytics/*` sono già deployati su **dev**, dove si svolgono sviluppo e verifiche. La pubblicazione in produzione è a valle della migrazione di prod e **fuori dallo scopo di queste card** |
| **`pressure` Pa vs hPa** | Pascal. Gli esempi degli swagger (`1012.8`) sono un refuso, da correggere lato backend |

**Due questioni che T-02 lascia aperte sul lato backend**, segnalate perché non sono nostre e
hanno impatto sui loro utenti:

1. **`/analytics/devices/{deviceSerial}/latest` non applica nessun controllo di dominio**:
   qualsiasi utente autenticato può leggere la telemetria di qualsiasi serial di cui conosca il
   numero. Segnalato, in fase di fix lato backend. Il nostro client non usa quell'endpoint.
2. **L'AQI divide la temperatura per 120** (fedeltà al legacy) mentre `internal_temperature`
   arriva già in °C: la componente temperatura di `aqi_value` è sistematicamente errata, e il
   commento nel codice lo ammette. Da questo dipende se esporre o no `aqi_value` nel primo
   rilascio → D-08.

**Residui, tutti non bloccanti, da portare in una card di follow-up:**
* **una sola richiesta**, che riduce sei domande: lo `swagger.yaml` completo di
  `yama-be-core-platform` (D-03, D-19, D-23, D-29, D-33, D-35);
* **semantica** che solo il backend conosce: `tvoc` / `V - Ix` (D-07), calcolo dell'AQI e divisore
  120 (D-08), enumerazione di `radon_status` (D-10), override `deviceConfig` per singolo device
  (D-12), PM del `sismoff` (D-14), ampiezza della finestra allargata (D-16), enumerazione e
  cadenza di `connection_status` (D-17), versionamento dello schema (D-11);
* **decisioni**: rotazione dei refresh token (D-25), app client dedicato (D-28), usage plan o API
  key per l'integrazione (D-27, D-31), `superadmin` dentro o fuori (D-20);
* **operativo**: accesso su dev a un device `sense` o `city` — il nostro Now+ non ha l'hardware
  radon, quindi la misura principale del prodotto non è testabile — e a un account senza device
  (D-32).
