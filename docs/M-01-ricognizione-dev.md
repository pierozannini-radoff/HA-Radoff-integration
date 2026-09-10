# M-01 — Ricognizione dell'API arch 2.0 su dev e fixture reali

### Integrazione Home Assistant · epica RT-116 · prima card della serie **M**
### Rif. T-02 (RT-2810, chiusa) e T-08 (RT-2938) · Documento di riferimento: [`T-02-domande-backend.md`](T-02-domande-backend.md) (v6)

**Stato: ✅ chiusa il 2026-09-10.** Ricognizione completata su dev con
`--auth-flow password`: 36 chiamate, 32 delle quali 200, fixture reali in
`tests/fixtures/dev/`. Sette verifiche su otto hanno un esito; **V5**
(`radon_status`) è fuori scopo per decisione presa il 2026-09-10 (`sense` e
`city` esclusi da questa release). Resta una richiesta al backend —
`ALLOW_USER_SRP_AUTH` sull'app client di dev — che blocca l'integrazione, non
questa card.

> ## ⚠️ Da rimuovere a fine epica RT-116
>
> Niente di quanto segue fa parte dell'integrazione: è strumentazione di
> migrazione, e va cancellata quando la serie M è chiusa e il client 2.0 è in
> piedi. Lasciarla in un repo pubblicato significa distribuire uno script che
> punta a `dev` e fixture di un account reale.
>
> - `scripts/probe_arch2.py`
> - `tests/fixtures/dev/` (fixture, manifest, esiti, README)
> - `tests/test_dev_fixtures.py`
> - l'helper `load_dev_fixture` e `DEV_FIXTURES_DIR` in `tests/conftest.py`
> - la voce `"probe_arch2.py" = ["ALL"]` in `.ruff.toml`
> - questo documento e gli altri in `docs/`, se non se ne decide un'altra sede
>
> Le fixture servono fino a quando le card della serie M le usano: la
> rimozione è l'ultimo passo dell'epica, non uno intermedio.

---

## Risultati delle esecuzioni del 2026-09-09

Due esecuzioni, con un account **di produzione** contro l'host **dev**
`v2.api.dev.iot.radoff.life`. La seconda con anche il pool Cognito di dev
(`eu-west-1_5SsvW9t6S`, app client `2i63gbc9sim3b8paasaga7jb6g`).

**Nessuna chiamata autenticata è passata.** Tre esiti però sono determinati, e
due di questi sono richieste concrete e immediate al backend.

### Esito 1 — 🟥 il flusso SRP non è abilitato sull'app client di dev

```
InvalidParameterException: USER_SRP_AUTH is not enabled for the client.
```

Il login sul pool dev **non fallisce per credenziali**: fallisce perché
quell'app client non ha il flusso SRP abilitato. L'integrazione non ha un piano
B — SRP è l'unico flusso che `pycognito` implementa e l'unico che non manda la
password in chiaro all'API, ed è quello che il client usa oggi in produzione.

Interrogando Cognito con un utente inesistente e una password fittizia — che
non tocca nessun account reale, perché un flusso non abilitato viene rifiutato
*prima* della ricerca dell'utente — risulta questa configurazione:

| App client | `USER_SRP_AUTH` | `USER_PASSWORD_AUTH` | `REFRESH_TOKEN_AUTH` |
|---|:--:|:--:|:--:|
| prod `61ckd0c4…` | ✅ (il login riesce) | ❌ | ✅ (con rotazione, → D-25) |
| **dev `2i63gbc9…`** | **❌** | ✅ | ✅ |

I due app client sono configurati in modo **diverso**, e a quello di dev manca
esattamente il flusso che l'integrazione usa. Non è una scelta di sicurezza
deliberata: dev ha abilitato `USER_PASSWORD_AUTH`, che manda la password in
chiaro, e disabilitato SRP, che non lo fa.

**Richiesta:** abilitare `ALLOW_USER_SRP_AUTH` sull'app client
`2i63gbc9sim3b8paasaga7jb6g`, oppure darci un app client di dev che lo abbia.
Si lega a **D-28** (app client dedicato): se ne create uno per l'integrazione,
nasca già con SRP abilitato.

Finché non è fatto, **la ricognizione non può partire**: è la dipendenza
singola che blocca tutta la serie M.

### Esito 2 — D-24: il token del pool attuale non è accettato dall'host v2 di dev

Su `/analytics/*` e `/data/*` l'authorizer Cognito **valuta il JWT e lo
respinge**: 401 `{"message": "Unauthorized"}`, `x-amzn-errortype:
UnauthorizedException`.

Trattandosi di un token di *produzione* contro l'host di *dev*, è atteso e
**non risponde a D-24(a)** — se `v2.api.iot.radoff.life` accetti il pool
attuale resta una vostra risposta. Conferma però che dev ha un pool proprio,
che è quello fornito, e rende l'Esito 1 l'unico ostacolo residuo su questo
fronte.

### Esito 3 — ✅ risolto: la discovery è sotto `/data/*`, non `/auth/*`

Questo esito è nato come blocco ed è finito in una correzione. Vale la pena
tenere la catena, perché è il tipo di errore in cui si ricasca.

**Il sintomo.** `GET /auth/user/me/domains` — il path indicato dalla risposta a
T-02 D-03 — risponde 403 `IncompleteSignatureException` con un token, e 403
`MissingAuthenticationTokenException` senza. Nessun preflight `OPTIONS`,
nessun header CORS sull'errore. Da qui avevamo dedotto, correttamente, che
**la risorsa non c'è**; e ipotizzato, sbagliando, che il problema fosse come è
esposto il base path `auth`.

**La causa vera.** Lo swagger di `yama-be-core-platform` (operationId
`getUserDomains`) mette la discovery su **`GET /data/user/me/domains`**, sotto
il base path `data`. È esattamente il path che avevamo sondato ottenendo **401
`UnauthorizedException`**: la risorsa esisteva ed era dietro l'authorizer
Cognito: mancava solo un token valido. Avevamo la risposta sotto gli occhi e
l'avevamo letta come rumore di fondo dell'authorizer.

| Path | `GET` senza token | `OPTIONS` | Verdetto |
|---|---|:--:|---|
| **`/data/user/me/domains`** | **401 `UnauthorizedException`** | — | **esiste, dietro authorizer Cognito** |
| `/analytics/measures-ranges` | 401 `UnauthorizedException` | 200 | esiste |
| `/data/devices` | 401 `UnauthorizedException` | 200 | esiste |
| `/auth/user/me/domains` | 403 `MissingAuthenticationTokenException` | 403 | **non esiste** |

Il 403 `IncompleteSignatureException` resta comunque una lezione da tenere:
davanti a una route inesistente **con** un header `Authorization`, API Gateway
prova a leggerlo come firma SigV4 e maschera il "non trovato" dietro un errore
di firma. Non è autorizzazione IAM.

**Cosa cambia.** Il config flow di D-03 non è bloccato: cambia solo il path. La
divergenza è nella risposta a D-03, che indica `/auth/user/me/domains` — forma
di arch 1.x — e va corretta a `/data/user/me/domains`.

**Cosa ci dice il contratto, oltre al path.** La response è più ricca di quanto
D-03 lasciasse intendere, e tre dettagli toccano il disegno del client:

```
{ email, domains: [ { domain: {prefix, name, parent_domain_prefix, created_at},
                      role: {code, name, description}, assigned_at } ],
  pagination: {page, page_size, total, total_pages} }
```

- **`prefix` e `name` esistono entrambi e sono distinti** (`radoff-hq` /
  "Radoff Headquarters"): è la risposta a **V6**, per contratto — resta da
  osservare a runtime, ma il menu di scelta del dominio può mostrare un nome
  leggibile invece del prefisso.
- **La discovery è paginata**, `page_size` default 20 e massimo 100. Il client
  deve **ciclare le pagine**, non fidarsi della prima: un utente con più di 20
  domini altrimenti ne vedrebbe una parte. C'è anche un filtro `search`.
- **`prefix` è annidato dentro `domain`**, non in cima alla voce, e ogni voce
  porta anche il `role` dell'utente su quel dominio. La `parent_domain_prefix`
  rivela una **gerarchia** di domini di cui non sapevamo nulla.

Lo script è stato aggiornato di conseguenza: usa il path giusto, estrae il
`prefix` dalla forma annidata, e tiene le varianti vecchie come sonde di
fallback.

### Esito 4 — la ricognizione è passata, e ha prodotto sette scoperte

Con `--auth-flow password` sul pool dev il login riesce e **tutte le chiamate
autenticate rispondono 200**. Le fixture di quella prima passata sono state
scartate (contenevano nomi di persone: vedi «Un incidente di redazione» sotto)
e vanno rigenerate, ma le osservazioni reggono.

**1. Il `prefix` NON è human-readable, il `name` sì.** La risposta a D-03 dice
che `domain_prefix` è leggibile (`radoff-hq`) e quindi usabile come etichetta
nel menu di scelta. Su dev, 14 domini su 15 hanno un `prefix` che è un
frammento di UUID — `1aeb7ad1`, `fdffb4d9`, `875fe89b` — e uno solo è
`radoff-cattolica`. **È esattamente il problema di arch 1.x che D-03 dava per
superato.** Il `name` però esiste ed è leggibile ("Quality Domain", "Bologna
RTH"): il config flow deve mostrare `name` e passare `prefix`, mai mostrare il
prefisso.

**2. Il device type `life` non ha uno schema.** `measures-ranges?device_type=life`
risponde **404**, e il campo `available` elenca `city, now, nowplus, sense,
sismoff`. D-13 dà l'enumerazione come `{sense, now, nowplus, city, life,
sismoff}`. O `life` non è ancora su dev, o non è un tipo con misure — va
chiesto, perché decide se il client deve gestirlo.

**3. Terza forma di body d'errore, confermata.** Il 404 di `measures-ranges` è
`{error, message, available}` — né `ErrorResponse` né il
`{"message": "Too Many Requests"}` di API Gateway. Il client deve saper leggere
almeno tre forme (D-29).

**4. L'account è `superadmin` su 15 domini.** Il ruolo è `superadmin` su
*tutti*, e i domini sono in gerarchia sotto `fdffb4d9` ("Bologna RTH"). Due
conseguenze: **non è un account rappresentativo** del config flow di un utente
normale, e **la sonda del 403 non è valida** — `domain_prefix=radoff-hq`, un
dominio non suo, ha risposto **200 con lista vuota** invece che 403, che per un
superadmin è coerente (→ D-20). Per verificare il 403 serve un account *senza*
quel ruolo.

**5. `domain_prefix` è davvero opzionale, e omesso restituisce tutto.**
`/data/devices` senza `domain_prefix` risponde 200 con device di domini
diversi, ciascuno col proprio `domain_prefix` nel payload. D-04 dice di
passarlo sempre, e va continuato a fare — ma il comportamento senza è un dato
che serve alla tassonomia.

**6. La discovery espone campi non documentati.** Oltre a quanto c'è nello
swagger, ogni dominio porta un `type` (`customer`, `business`, `unknown`) che
lo swagger non dichiara, e `assigned_at` è sempre `null`.

**7. Il primo dominio della lista può essere vuoto.** Il dominio scelto
inizialmente (`1aeb7ad1`) ha zero device, quindi la prima passata ha prodotto
liste vuote e ogni verifica sui payload è uscita "non osservato" — che ha la
stessa faccia di un problema vero. Lo script ora scandisce i domini con
`page_size=1` finché ne trova uno con device.

### Rettifiche del 2026-09-10 (pomeriggio)

Due correzioni da Piero, una confermata e una precisata.

**1. I 15 domini sono un artefatto del ruolo — confermato.** L'account usato è
`superadmin` su tutti, cioè con accesso platform-wide: un utente normale vede
il proprio dominio e basta. Il censimento (15 domini, 120 device, 83/18/11/2/2/2)
**non è la risposta a D-20** e non descrive cosa vedrà un utente
dell'integrazione. Descrive un caso limite utile — il config flow deve reggere
15 domini senza rompersi — ma il caso normale resta non osservato, insieme al
403 su dominio altrui (D-29), che per un superadmin non si verifica.

**2. "SRP andava già bene, era l'utenza a non essere adatta" — no, e si
dimostra.** L'affermazione è verificabile e va verificata, perché decide se
serve una richiesta al backend. Il test: tentare l'handshake SRP con un utente
**che non esiste**. Se l'errore dipendesse dall'utenza, un utente inesistente
darebbe un errore diverso.

| App client | `USER_SRP_AUTH` con utente inesistente | Lettura |
|---|---|---|
| prod `61ckd0c4…` | `NotAuthorizedException` | Cognito è **arrivato a cercare l'utente**: SRP abilitato |
| dev `2i63gbc9…` | `InvalidParameterException: USER_SRP_AUTH is not enabled for the client` | rifiutato **prima** di guardare l'utente: SRP non abilitato |

Con un utente che non esiste la variabile "utenza" è eliminata per
costruzione, e i due app client rispondono in modo diverso. **La causa è
l'app client `2i63gbc9sim3b8paasaga7jb6g`, non l'utenza.**

Resta una spiegazione compatibile con entrambe le cose, ed è probabilmente
quella giusta: se su dev qualcosa autentica già via SRP — l'app mobile, il
frontend — allora esiste **un altro app client sullo stesso pool** che ha SRP
abilitato, e quello che ci è stato passato non è quello. In quel caso non
serve nessuna modifica al backend: serve il client id giusto. La richiesta
diventa quindi *o* `ALLOW_USER_SRP_AUTH` su `2i63gbc9…`, *o* il client id di un
app client di dev che ce l'abbia già.

Lo script ora enumera anche `USER_SRP_AUTH` con l'utente fittizio, insieme
agli altri due flussi, così il dato è nel report invece di dipendere
dall'esito di un login.

### Esecuzione finale — account non-`superadmin`

Rimosso il ruolo `superadmin` dall'utenza, la ricognizione è stata rilanciata e
ha chiuso le due voci che il ruolo teneva aperte.

- **D-20 ha una risposta.** La discovery restituisce **un solo dominio**
  (`875fe89b`) con 2 device. Il caso a 15 domini resta come limite superiore
  che il config flow deve reggere, non come caso normale.
- **Il 403 si provoca davvero.** `domain_prefix` di un dominio non proprio
  risponde **403** con `{"error": "Forbidden: you do not belong to domain
  'radoff-hq'"}` — stessa forma del 404 su serial inesistente. Con il
  `superadmin` rispondeva 200, perché per quel ruolo l'accesso era legittimo.
- **Telemetria osservata su un device reale**: `3D90E0` (`nowplus`,
  `connected`) porta `aqi_value`, `eco2`, `internal_temperature`, `pm1`,
  `pm10`, `pm25`, `pressure`, `relative_humidity`, `timestamp`, `tvoc`. Il
  secondo device è un `sense` disconnesso con `telemetry: null` — fuori scopo
  per questa release, e comunque muto.
- **`pressure` 100488.0 Pa** e **`internal_temperature` 26.0583 °C**
  confermano V7 e V8 anche su questo account.

**Una riserva sull'evidenza di V1.** Le fixture ora in repo vengono da un
dominio con 2 device, quindi la seconda pagina è vuota e da sole non
dimostrano la stabilità dell'ordinamento in paginazione. La misura seria —
83 device, due pagine piene, nessuna sovrapposizione — è stata fatta nella
passata precedente. È una proprietà dell'API e non del ruolo, quindi resta
valida, ma va detto che non è riproducibile dalle fixture correnti.

### Decisioni di scopo prese il 2026-09-10

Prese da Piero, e restringono la card:

- **`sense` e `city` fuori da questa release.** Di conseguenza **V5
  (`radon_status`) esce dallo scopo**: era l'unica verifica che richiedeva
  hardware radon, e la richiesta operativa di T-08 §7 non serve più per M-01.
- **`life` fuori da questa release.** Il 404 di `measures-ranges?device_type=life`
  resta registrato come divergenza rispetto a D-13, ma non blocca nulla.
- **La release è interna, su dev. La produzione è esclusa.** Quindi **D-24(a)**
  — se `v2.api.iot.radoff.life` accetti i token del pool attuale — non serve
  per questa release: resta una domanda aperta per il rilascio pubblico, che ha
  una card propria.

Restano nello scopo i tipi `now`, `nowplus` e `sismoff`, tutti con schema
disponibile su `measures-ranges`.

### Esito 5 — i device ci sono, ma quasi tutti sono muti

La seconda passata ha trovato il dominio giusto e ha risposto 200 a tutto, ma
**V7 e V8 sono rimaste non osservate lo stesso**: i due device del dominio
scelto (`2ad5b08a`) hanno `telemetry: null`, `connection_status:
disconnected`, `config_status: awaiting_setup`. Non hanno mai trasmesso.

La chiamata senza `domain_prefix` mostra il quadro vero: **120 device in
totale**, e almeno uno — `3D90E0`, `nowplus`, `connection_status: connected` —
porta telemetria completa (`aqi_value`, `eco2`, `internal_temperature`, `pm1`,
`pm10`, …). Il criterio di scelta del dominio era troppo debole: "ha device"
non implica "ha dati". Ora lo script scarta i domini in cui tutti i device
hanno `telemetry: null` e lo dice quando non ne trova nessuno con dati.

**Due scoperte che vengono da qui**, entrambe utili alle card successive:

- **`telemetry: null` è la forma reale del device che non ha mai trasmesso**
  (→ D-16). Il client deve trattarlo come "nessun dato", non come errore: su
  120 device è la condizione della maggioranza.
- **La nidificazione `controller_of_device` è reale** (→ D-33). Un `city`
  (`E754F0`) porta inline l'intero oggetto del `life` che controlla
  (`855894`), con `managed_by_device_serial` che punta all'indietro. Il device
  annidato **appartiene a un altro dominio** (`9a8e7728`) rispetto al padre
  (`2ad5b08a`): un client che appiattisse la lista si troverebbe device di
  domini che non ha chiesto. Con `city` e `life` fuori scopo non ci tocca in
  questa release, ma va saputo prima di riaprirla.

### Un incidente di redazione, e cosa ne è seguito

Le fixture della prima passata contenevano **nomi e cognomi di persone reali**:
il campo `name` dei domini su dev è valorizzato con i nomi dei referenti, e
quello dei device col nome che il proprietario gli ha dato. La redazione
copriva UUID, mail, JWT e coordinate, ma non le etichette libere.

I file sono stati rimossi e la regola aggiunta. È **contestuale**: `name`
diventa `<label-N>` solo se l'oggetto che lo contiene ha anche un `prefix` o un
`serial_number` — un dominio o un device — mentre `role.name` ("Super Admin") e
le etichette di `measures-ranges` ("PM10") restano intatte, perché redigerle
avrebbe svuotato proprio le fixture dello schema. `tests/test_dev_fixtures.py`
non lo avrebbe intercettato: cercava JWT, mail, UUID e coordinate, non nomi
propri, che nessuna espressione regolare riconosce in modo affidabile.

### Esito accessorio — D-30 chiusa

L'header di request id è **`x-amzn-requestid`**, affiancato da
`x-amz-apigw-id`. Era ⬜ da sempre; questa è la conferma osservata.

### Cosa serve per sbloccare

**Una cosa sola, dal backend:** `ALLOW_USER_SRP_AUTH` sull'app client
`2i63gbc9sim3b8paasaga7jb6g` di dev. È il flusso che l'integrazione usa e
l'unico che non trasmette la password.

**Nel frattempo, da noi:** l'app client di dev ha `USER_PASSWORD_AUTH`
abilitato, quindi con un account di prova su dev si può ottenere un token e
completare la ricognizione senza aspettare. Lo script lo supporta con
`--auth-flow password`, che stampa un avviso e va usato **solo** con
credenziali di prova, mai di produzione: quel flusso manda la password a
Cognito dentro la richiesta (protetta dal TLS, ma il server la riceve), mentre
SRP dimostra di conoscerla senza trasmetterla.

```bash
.venv/bin/python scripts/probe_arch2.py \
  --username '<utente dev>' --ask-password \
  --auth-flow password
```

Il `domain_prefix` non va indovinato: lo ricava la discovery.

**Resta aperta, ma non blocca:** D-24(a) — se `v2.api.iot.radoff.life` (prod)
accetti i token del pool attuale. Decide se il rilascio può essere graduale, e
solo il backend può rispondere.


---

## Perché questa card viene prima

Senza fixture reali ogni fase successiva della serie M si scriverebbe su payload
immaginati, ed è esattamente così che sono nati i difetti che stiamo
correggendo: il fattore `0.00835`, le soglie hardcodate, l'AQI nel bucket
sbagliato. Qui si legge e si registra, non si migra: `custom_components/radoff/`
non viene toccata.

L'ambiente è **dev** (`https://v2.api.dev.iot.radoff.life`), dove InfluxDB e
l'intera API `/analytics/*` sono già deployati. La produzione non ha ancora il
piano dati 2.0 ed è fuori scopo (T-02 D-01).

## Come si esegue

```bash
export RADOFF_DEV_USERNAME='...'          # le proprie credenziali su dev
export RADOFF_DEV_PASSWORD='...'
export RADOFF_DEV_POOL_ID='eu-west-1_XXXXXXXX'   # facoltativi, → D-24
export RADOFF_DEV_CLIENT_ID='...'

python3 scripts/probe_arch2.py
```

Lo script non contiene credenziali: è ripetibile da chiunque nel team con le
proprie. Output in `tests/fixtures/dev/` — fixture redatte, `_manifest.json` con
gli **header di risposta completi** di ogni chiamata, `_findings.json` e
`_findings.md` con gli esiti.

**Dove trovare i parametri.** L'host è nella risposta a
[D-01](T-02-domande-backend.md) (da `infrastructure/terraform/environments/*.tfvars`)
ed è già il default dello script. Il pool Cognito **attuale** è in
`custom_components/radoff/const.py:90-92` e lo script lo legge da lì. Il pool
**dev** di arch 2.0 non è noto — è la metà aperta di D-24(b): sta nell'ARN
dell'authorizer `COGNITO_USER_POOLS` (`modules/data-analytics/main.tf:530-534`),
nei tfvars di dev, o si ricava con
`aws cognito-idp list-user-pools --region eu-west-1` sull'account dev. Se manca,
lo script gira lo stesso e registra D-24 come parziale.

---

## Diagnostica: provare un account dev mentre SRP è disabilitato

Sull'app client di dev `USER_PASSWORD_AUTH` **è** abilitato (vedi Esito 1),
quindi si può verificare se un account esiste in quel pool senza aspettare che
abilitino SRP:

```bash
.venv/bin/python scripts/probe_arch2.py \
    --auth-flow password --ask-password \
    --username 'utente@dev' \
    --domain-prefix '<prefisso del dominio>' \
    --out /tmp/probe-dev
```

`--ask-password` chiede la password in modo interattivo: non finisce nella
history della shell né nella tabella dei processi, a differenza di una env var
scritta sulla riga di comando. `RADOFF_DEV_POOL_ID` e `RADOFF_DEV_CLIENT_ID`
vanno comunque esportati, altrimenti prova solo il pool di produzione.

`--domain-prefix` serve perché la discovery non è raggiungibile (Esito 3): senza,
le chiamate a `/data/devices` vengono saltate. Con un prefisso plausibile a mano,
invece, **la ricognizione va avanti fino in fondo** — fixture comprese.

### Cosa aspettarsi

| Risposta | Significato |
|---|---|
| `NotAuthorizedException` | password errata **o** utente inesistente: con `PreventUserExistenceErrors` (default Cognito) i due casi non si distinguono |
| `UserNotConfirmedException` | l'utente esiste ma non ha completato la conferma |
| login riuscito | **l'account c'è**, e hai un token valido del pool dev: da lì lo script prosegue e produce le fixture |

### Perché non è il flusso dell'integrazione, e non lo diventerà

`USER_PASSWORD_AUTH` manda la password a Cognito **in chiaro** dentro la
richiesta — protetta dal TLS in transito, ma il server la riceve così. SRP
dimostra di conoscerla senza trasmetterla, ed è quello che il client usa oggi in
produzione. Questa strada esiste solo perché l'app client di dev ha SRP
disabilitato, serve a non restare fermi nell'attesa, e va usata **solo con un
account di prova su dev** — mai con credenziali di produzione.

Che su dev sia abilitato il flusso meno sicuro e disabilitato il più sicuro è
di per sé un argomento da mettere nel ticket.

---

## Verifiche da riportare (T-08 §6)

Ogni riga ha un esito scritto, calcolato dalle response reali. `ID` è la chiave
con cui l'esito compare in `_findings.json`.

| ID | Verifica | Esito |
|:--:|---|---|
| **V1** | Ordinamento di `GET /data/devices` stabile; filtri o ordinamenti | ✅ **stabile.** L'evidenza forte viene dalla passata `superadmin` su 83 device: pagine 1 e 2 senza sovrapposizioni, prima pagina identica alla ripetizione, coerente con la passata `page_size=200`. Sull'account normale (2 device) la ripetizione è identica ma la seconda pagina è vuota, quindi le fixture attuali da sole non lo dimostrano. `sort`, `order_by` e `order` rispondono **200 ma non cambiano nulla**: parametri ignorati, nessun ordinamento esposto |
| **V2** | Enumerazione completa di `unit` | ✅ **8 unità**: `V - Ix` (VOC), `ppm` (CO₂, CO, metano), `°C`, `%`, `Pa`, **`""`** (AQI, stringa vuota), `Bq/m³` (radon), `µg/m³` (particolato) |
| **V3** | `pm10 excellent upperBound: 200` a runtime? | ✅ **no: è 20.** A runtime la scala è 20 / 30 / 40 / 50. Il `200` è un refuso dello swagger |
| **V4** | Il `sismoff` espone `pm1`/`pm25`/`pm10`? | ✅ **non li espone.** Conferma la risposta a D-13, smentisce l'esempio dello swagger |
| **V5** | Tipo e valori di `radon_status` | ⬜ **fuori scopo**: `sense` e `city` esclusi da questa release |
| **V6** | `prefix` e nome visualizzabile distinto? | ✅ **entrambi presenti, ma il `prefix` NON è leggibile**: 14 domini su 15 hanno un frammento di UUID (`1aeb7ad1`). Il `name` è leggibile. Il config flow deve **mostrare `name` e passare `prefix`** |
| **V7** | `pressure` in Pa (~101.300)? | ✅ **Pa**, osservato `101083.0` |
| **V8** | `internal_temperature` in °C o centesimi? | ✅ **°C**, osservato `26.8583`. Nessun fattore di scala |


### Come ciascuna viene misurata

**V1** — tre chiamate a `/data/devices` con `page_size` piccolo (pagina 1,
pagina 2, e di nuovo pagina 1) più una passata con `page_size=200`. Lo script
confronta le sequenze di serial: fra chiamate ripetute, fra paginazione e
passata piena, e cerca serial duplicati fra le due pagine — un ordinamento
instabile si manifesta come un device che compare due volte o non compare
affatto. I filtri si sondano passando `sort`, `order_by` e `order` e guardando
status e forma della risposta: gli swagger non li documentano, l'unico modo di
saperlo è provarli.

**V2** — `measures-ranges` senza `device_type` unisce tutti i tipi; lo script
raccoglie ogni `unit` dichiarata e le misure che la usano.

**V3, V4** — lettura diretta dei blocchi `ranges` nel payload reale, non
nell'esempio dello swagger.

**V5, V7, V8** — valori realmente presenti nei payload di `/data/devices` e
`/data/devices/{serial}`. Lo script distingue tre casi per `radon_status`: campo
assente, campo presente ma sempre `null`, valori osservati.

**V6** — chiavi effettivamente presenti per ogni dominio nella response di
discovery.

---

## Verifiche aggiuntive della card

| ID | Domanda | Esito |
|:--:|---|---|
| **D-03** | La discovery è chiamabile col solo bearer token? | ✅ **sì**, su `GET /data/user/me/domains`. Il path in D-03 (`/auth/...`) è di arch 1.x |
| **D-24** | Il token del pool attuale è accettato dall'host v2? | ⬜ **non determinato**, e fuori scopo: la release è interna su dev |
| **D-28** | L'app client permette il flusso SRP? | 🟥 **no sull'app client `2i63gbc9…`.** Verificato con un utente **inesistente**: la causa è l'app client, non l'utenza. Su prod lo stesso test dà `NotAuthorizedException`, cioè SRP abilitato |
| **D-29** | Tassonomia degli errori | ✅ **cinque casi, tre forme di body**: `{error, message, available}` (404 tipo ignoto, con i tipi validi), `{error}` (404 serial inesistente **e** 403 dominio altrui), `{message}` (401 senza token). Il 403 è `{"error": "Forbidden: you do not belong to domain 'radoff-hq'"}` |
| **D-30** | Header di request id | ✅ **`x-amzn-requestid`**, con `x-amz-apigw-id` e `x-amzn-trace-id` |
| **D-20** | Quanti domini e device vede un utente | ✅ **un utente normale vede un solo dominio.** Rimosso il ruolo `superadmin` dall'account, la discovery restituisce 1 dominio (`875fe89b`) con 2 device. Il caso `superadmin` — 15 domini, 120 device — resta come limite superiore che il config flow deve reggere |


**D-24** si misura nel modo più diretto possibile: lo stesso account, la stessa
`GET /auth/user/me/domains` sull'host v2, con il token del pool attuale e con
quello del pool dev. Quale dei due l'API accetta è la risposta. Se il pool
attuale **non** è accettato, la migrazione ha una dipendenza in più (le sessioni
persistite non valgono sull'altro host, e non si può fare un rilascio graduale):
va segnalato subito, e lo script si ferma dicendolo.

**D-29** — quattro errori provocati deliberatamente più uno senza token:
`domain_prefix` di un dominio non proprio (403 atteso), serial inesistente,
`device_type` ignoto su `measures-ranges` (404 con `available` atteso),
`/data/devices` senza `domain_prefix`, e una chiamata non autenticata. Di
ciascuno si registrano status, chiavi del body e body intero: sappiamo già che
le forme non sono uniformi (`ErrorResponse`, `{"message": "Too Many Requests"}`
di API Gateway, il 404 con `available`), e questa è la base della tassonomia di
M-02.

**D-30** — lo script scorre gli header di risposta di **tutte** le chiamate e
riporta quelli che somigliano a un request id (`x-amzn-requestid`,
`x-amz-apigw-id`, …), più l'elenco completo degli header visti. Quello che
conta è quale dei candidati compare davvero: è il valore che ha senso far
riportare all'utente in una segnalazione.

---

## Divergenze rispetto agli swagger

⬜ *Da compilare dopo l'esecuzione.* Ogni anomalia osservata va confrontata con
quanto dichiarato negli swagger; le divergenze si aggiungono all'elenco dei
refusi di **T-08 §8** e, in parallelo, all'[Appendice C di
T-02](T-02-domande-backend.md) (che oggi si ferma a 14 voci).

Le tre già attese, perché nascono da una contraddizione nota:

- **`sismoff` e i PM** (Appendice C #11): la risposta a D-13 dice che non li ha,
  l'esempio `sismoff_device` dello swagger li elenca sia in `field_metadata` sia
  in `telemetry`. V4 dice quale delle due è vera.
- **`pm10 excellent upperBound: 200`** (Appendice C #5): incoerente con
  `high ≤ 30`. V3 dice se il refuso è nell'esempio o nel dato.
- **`radon_status`** (Appendice C #6): `dataType: "string"` ma valore `2`. V5
  dice qual è il tipo reale.

---

## Se la discovery viene rifiutata (exit 2)

Il login riesce, ma `GET /auth/user/me/domains` sull'host v2 risponde 401 o
403. **Non è automaticamente l'esito di D-24**: dipende da *chi* ha rifiutato.
Lo `x-amzn-errortype` della risposta, registrato in `_manifest.json`, lo dice.

| `x-amzn-errortype` | Chi ha rifiutato | Cosa significa per D-24 |
|---|---|---|
| assente, con body `ErrorResponse` | l'**applicazione** | il token è stato validato: il rifiuto è una decisione applicativa (es. l'utente non ha domini su quell'ambiente). D-24 tende verso "accettato" |
| `UnauthorizedException` / 401 `{"message":"Unauthorized"}` | l'**authorizer Cognito** | il JWT è stato valutato e respinto: pool o app client diversi. **Questa** è la risposta negativa a D-24 |
| `IncompleteSignatureException`, `InvalidSignatureException`, `UnrecognizedClientException` | **API Gateway**, prima dell'authorizer | il gateway ha provato a leggere `Authorization` come una **firma SigV4**, quindi il JWT non è stato nemmeno guardato. Il metodo è autorizzato con **IAM (`AWS_IAM`)**, non con l'authorizer Cognito. **D-24 resta non determinato** |
| `MissingAuthenticationTokenException` | **API Gateway** | il path non esiste su quel base path mapping — è il messaggio fuorviante che API Gateway dà per una route inesistente, non un problema di autenticazione |

### Perché la ricognizione prosegue lo stesso

`/auth/*`, `/analytics/*` e `/data/*` sono **tre base path mapping distinti**
sullo stesso host (T-02 D-01) e possono avere autorizzazioni diverse. Un
rifiuto su uno non implica gli altri, quindi lo script non si ferma più: prova
comunque gli altri base path e produce la verifica **`D-24-surface`**, che
riporta per ciascuno gli status ottenuti e gli `errortype` visti. È quella a
dire se il problema è di tutto l'host o di un solo pezzo.

Lo script prova anche **due formati dell'header** `Authorization` — `Bearer
<token>` (quello del client 1.x) e il JWT nudo — e registra quale passa. Sotto
autorizzazione IAM non passa nessuno dei due, il che è di per sé una conferma.

### Cosa chiedere al backend in quel caso

Se l'errore è di livello gateway, la domanda per T-08 non è più «quale pool»,
ma: **come è autorizzato `/auth/*` sull'host v2 di dev?** Se è `AWS_IAM`, un
client di terze parti con le sole credenziali dell'utente non può chiamarlo, e
la discovery del dominio — su cui si regge tutto il config flow deciso in D-03
— va ripensata o esposta dietro l'authorizer Cognito come gli altri. È un
blocco di progettazione, non un dettaglio di configurazione, e va sollevato
subito.

---

## Diagnosi del login (se lo script si ferma con exit 4)

Se il login SRP fallisce, la ricognizione non parte e **D-24 resta non
determinato**: senza token l'host v2 non viene mai interrogato, quindi il
fallimento non dice nulla su se l'API 2.0 accetti i token del pool attuale.
Lo script lo scrive esplicitamente invece di far coincidere le due cose.

Il codice Cognito grezzo, che `api/auth.py` normalmente assorbe dentro
`AuthInvalidError`, viene stampato: è quello che discrimina.

| Codice | Cosa significa qui |
|---|---|
| `UserNotFoundException` | l'utente **non esiste in questo pool**. Se le stesse credenziali funzionano nell'app dev, il pool di arch 2.0 è un altro: D-24(b) diventa bloccante |
| `NotAuthorizedException` | password errata, utente disabilitato, **oppure** utente inesistente. Con `PreventUserExistenceErrors` attivo (default Cognito) i due casi sono indistinguibili dall'esterno: serve il passo di isolamento qui sotto |
| `UserNotConfirmedException` | l'utente esiste ma non ha completato la conferma: va confermato dall'app o dalla console |
| `ResourceNotFoundException` | pool id o client id inesistenti: è la configurazione del pool a essere sbagliata, non le credenziali |

### Isolare credenziali da pool

Il pool attuale in `const.py` è quello di **produzione**. Un account creato su
dev con ogni probabilità vive in un pool diverso, e in quel caso il fallimento
è atteso, non un sintomo.

Due prove, nell'ordine:

**1. Il percorso SRP è sano?** Stesso pool, un account che sappiamo valido lì
(quello di produzione):

```bash
RADOFF_DEV_USERNAME='<account prod>' RADOFF_DEV_PASSWORD='<password>' \
  .venv/bin/python scripts/probe_arch2.py --out /tmp/probe-isolamento
```

Se questo login riesce, l'handshake SRP e il pool funzionano, e il problema è
che **l'account dev non appartiene a quel pool** — cioè arch 2.0 su dev ha un
proprio user pool. Se fallisce anche questo, il problema è a monte e non
riguarda dev.

**2. Le credenziali dev sono valide da qualche parte?** Provale nell'app o nel
frontend puntati su dev. Se lì entrano, la conclusione del punto 1 è confermata
e serve il pool dev.

### Trovare il pool dev

A quel punto `RADOFF_DEV_POOL_ID` e `RADOFF_DEV_CLIENT_ID` non sono più
facoltativi. In ordine di costo:

1. **Terraform**, `modules/data-analytics/main.tf:530-534`: l'authorizer
   `COGNITO_USER_POOLS` ha `provider_arns`, e l'ARN contiene il pool id
   (`arn:aws:cognito-idp:eu-west-1:<account>:userpool/eu-west-1_XXXXXXXX`). Il
   client id sta nel modulo che definisce l'app client, o nei tfvars.
2. **`infrastructure/terraform/environments/dev.tfvars`** — stesso file da cui
   sono usciti gli host di D-01.
3. **AWS sull'account dev**:
   `aws cognito-idp list-user-pools --max-results 60 --region eu-west-1`, poi
   `aws cognito-idp list-user-pool-clients --user-pool-id <id>`.
4. **Build dev dell'app mobile o web** — il client id è pubblico per
   definizione, come il nostro in `const.py`.
5. **Backend**, ed è comunque da chiedere: è la domanda D-24(b).

**Questo esito va riportato su RT-2938 subito**, senza aspettare il resto della
ricognizione: se arch 2.0 usa un pool diverso, le sessioni persistite non
valgono sull'altro host, ogni installazione richiede di nuovo le credenziali, e
un rilascio graduale 1.x → 2.0 non è possibile. È il fattore che la risposta a
D-24 doveva chiarire, e il primo indizio concreto che abbiamo.

---

## Cosa resta scoperto, e perché

L'account usato per la ricognizione è un **Now+**, che non ha l'hardware radon.
`radon` e `radon_status` restano quindi non osservati o osservati solo come
`null`: V5 non si chiude davvero senza un **`sense` o un `city` su dev**, che è
la richiesta operativa di **T-08 §7**. Non blocca questa card — la copertura è
minore, e va detto nel commento invece di lasciarlo dedurre.

Stessa cosa per **D-32** (cosa risponde `/data/devices` a un utente senza
device): serve un account in quelle condizioni, che non abbiamo.

---

## Commento per RT-2938 (T-08)

Postato il 2026-09-10. Il testo è riprodotto qui per averlo nel repo insieme
alle fixture che lo sostengono.

---

**M-01 — ricognizione arch 2.0 su `v2.api.dev.iot.radoff.life`, chiusa il
2026-09-10.** 36 chiamate, 32 con esito 200. Fixture reali e redatte in
`tests/fixtures/dev/`, script ripetibile in `scripts/probe_arch2.py`.

### Verifiche di §6

Sette su otto hanno un esito. **V5** (`radon_status`) è fuori scopo: `sense` e
`city` sono stati esclusi da questa release, che è interna e su dev.

- **V1 — ordinamento stabile.** Su 83 device: pagine 1 e 2 senza
  sovrapposizioni, prima pagina identica se ripetuta, coerente con
  `page_size=200`. `sort`, `order_by`, `order` rispondono 200 ma non cambiano
  il risultato: **parametri ignorati, nessun ordinamento esposto**.
- **V2 — 8 unità**: `V - Ix` (VOC), `ppm`, `°C`, `%`, `Pa`, **`""`** (AQI,
  stringa vuota), `Bq/m³`, `µg/m³`.
- **V3 — `pm10 excellent upperBound` è 20, non 200.** A runtime la scala è
  20/30/40/50. Il 200 è un refuso dello swagger: chiude la discrepanza.
- **V4 — il `sismoff` non espone i PM.** Conferma D-13, smentisce l'esempio
  dello swagger.
- **V6 — `prefix` e `name` ci sono entrambi, ma il `prefix` NON è leggibile.**
  14 domini su 15 hanno un frammento di UUID (`1aeb7ad1`, `875fe89b`); uno
  solo è `radoff-cattolica`. La risposta a D-03 lo dava per human-readable e
  quindi usabile come etichetta: non lo è. Il `name` sì, quindi il config flow
  mostrerà `name` e passerà `prefix`.
- **V7 — `pressure` in Pascal**, osservato `101083.0`.
- **V8 — `internal_temperature` in °C**, osservato `26.8583`. Nessun fattore
  di scala.

### Altre risposte ottenute

- **D-03 — la discovery è `GET /data/user/me/domains`**, non
  `/auth/user/me/domains` come indica la risposta a D-03: quel path su arch
  2.0 non esiste (403 `MissingAuthenticationTokenException`, nessun preflight
  `OPTIONS`). Funziona col solo bearer token. La response è **paginata**
  (`page_size` default 20, max 100) e annida il dominio in
  `domains[].domain.prefix`; espone anche un campo `type`
  (`customer`/`business`/`unknown`) non documentato, e `assigned_at` sempre
  `null`.
- **D-20 — censiti 15 domini e 120 device** per questo account. Distribuzione
  83 / 18 / 11 / 2 / 2 / 2, gli altri nove vuoti. L'account è `superadmin` su
  tutti.
- **D-29 — tre forme di body d'errore**: `{error, message, available}` (404
  per `device_type` ignoto, con l'elenco dei tipi validi), `{error}` (404 per
  serial inesistente), `{message}` (401 senza token). **I due casi attesi come
  403 hanno risposto 200**, coerente col ruolo `superadmin`: per verificare il
  403 su dominio altrui serve un account senza quel ruolo.
- **D-30 — l'header di request id è `x-amzn-requestid`**, affiancato da
  `x-amz-apigw-id` e `x-amzn-trace-id`. Era una domanda aperta.
- **D-16 — `telemetry: null` è la forma reale del device che non ha mai
  trasmesso**, ed è la condizione della maggioranza dei 120 device. Il client
  deve trattarla come "nessun dato", non come errore.
- **D-33 — la nidificazione `controller_of_device` è reale**, e attraversa i
  domini: un `city` porta inline l'intero oggetto del `life` che controlla, e
  quel `life` appartiene a un dominio diverso dal padre. Con `city` e `life`
  fuori scopo non ci tocca ora, ma va saputo prima di riaprirli.

### Una richiesta, ed è l'unica che blocca

**Abilitare `ALLOW_USER_SRP_AUTH` sull'app client `2i63gbc9sim3b8paasaga7jb6g`
del pool dev `eu-west-1_5SsvW9t6S`.** Oggi non è abilitato, e l'integrazione
autentica solo via SRP: è l'unico flusso che `pycognito` implementa e l'unico
che non trasmette la password. Sondando Cognito con un utente inesistente
(nessun account reale toccato) risulta:

| App client | `USER_SRP_AUTH` | `USER_PASSWORD_AUTH` | `REFRESH_TOKEN_AUTH` |
|---|:--:|:--:|:--:|
| prod `61ckd0c4…` | ✅ | ❌ | ✅ (con rotazione, → D-25) |
| dev `2i63gbc9…` | **❌** | ✅ | ✅ |

I due app client divergono, e a dev manca proprio il flusso che serve, mentre è
abilitato quello che trasmette la password. Questa ricognizione è passata da
`USER_PASSWORD_AUTH` come diagnostica una tantum: non è e non diventerà il
flusso dell'integrazione.

### Per §8 — divergenze rispetto agli swagger

1. **Il path della discovery**: D-03 dice `/auth/user/me/domains`, la realtà è
   `/data/user/me/domains`.
2. **`pm10 excellent upperBound: 200`**: a runtime è 20.
3. **I PM del `sismoff`**: l'esempio `sismoff_device` li elenca, lo schema no.
4. **Il tipo `life`**: `measures-ranges?device_type=life` risponde 404 e
   `available` elenca `city, now, nowplus, sense, sismoff` — ma i device di
   tipo `life` **esistono** in `/data/devices`. Il tipo c'è nel modello device
   e non nello schema delle misure. D-13 lo includeva nell'enumerazione.
5. **Il `domain_prefix` non è human-readable**, contrariamente a D-03.
6. **Campi non documentati** nella discovery: `domain.type`.

### Fuori scopo per questa release

`sense`, `city` e `life` esclusi; restano `now`, `nowplus`, `sismoff`, tutti
con schema disponibile. La release è **interna, su dev**: la produzione è
esclusa, quindi **D-24(a)** — se `v2.api.iot.radoff.life` accetti i token del
pool attuale — resta aperta ma non blocca, e andrà chiusa prima del rilascio
pubblico.
