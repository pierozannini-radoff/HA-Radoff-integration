# M-05 (RT-2944) — decisioni, scostamenti e verifica dal vivo

Card: **[M-05] Polling a una chiamata: paginazione, intervalli difendibili
e jitter**. Fase 5 di 8 della migrazione ad arch 2.0.
Riferimenti: T-02 (RT-2810) e T-08 (RT-2938). Dipende da M-03 (RT-2941),
parallelizzabile con M-06.

Il perimetro della card è solo lo **scheduling**: il passo 1 (chiamata
unica e paginazione) si è spostato in M-03 il 2026-09-10, secondo
l'ipotesi (a) della nota di quella card. Qui si decide ogni quanto la
richiesta parte, con quale sfasamento, e cosa succede a un 429.

Questo documento tiene le due cose che la card da sola non tiene: **le
decisioni prese durante l'implementazione, e da chi**, e **come si
verifica dal vivo** ciò che una suite mockata non può verificare.

## Decisioni prese in implementazione

### Lo sfasamento è additivo, non simmetrico

La card chiede «uno sfasamento casuale stabile per installazione». La
prima ipotesi discussa era ±10% dell'intervallo. È diventata **[0, +10%)**
— l'offset si somma sempre, non si sottrae mai.

**Perché.** Con ±10%, una entry configurata al minimo (60 s) polla a 54 s,
cioè sotto il pavimento che il passo 2 di questa card ha appena stabilito
e per la ragione — la cadenza del dispositivo — che rende quel pavimento
non negoziabile. Sommando soltanto, la dispersione è la stessa e nessuna
installazione scende mai sotto l'intervallo richiesto: 300 s diventa
300–330 s, 60 s diventa 60–66 s. Scostamento proposto nel piano della card
e approvato il 2026-09-10; l'alternativa scartata era ±10% con clamp a
`MIN_SCAN_INTERVAL`, che avrebbe aggiunto un caso limite senza aggiungere
dispersione.

**Effetto sul carico.** Lo sfasamento è sul *periodo*, non solo sul primo
poll: la deriva fra due installazioni è permanente e non viene riazzerata
da un riavvio condiviso (un aggiornamento di Home Assistant, un blackout
da cui tutti ripartono insieme).

### L'offset è derivato dall'`entry_id`, non estratto a caso

`_stable_fraction(entry_id)` è SHA-256 sui primi 4 byte, scalato in
[0, 1). Non `random`, e non `hash()`.

**Perché.** `random` ridisegna a ogni riavvio, e un riavvio è proprio
l'evento correlato che l'offset esiste per rompere: mille installazioni
che ripartono insieme dopo un aggiornamento tornerebbero allineate. `hash()`
è ri-seedato a ogni processo (`PYTHONHASHSEED`), quindi ha lo stesso
difetto in modo meno evidente. Con SHA-256 sull'`entry_id`, ricostruire il
coordinator è indistinguibile dal non averlo mai fermato.

Nota: Home Assistant sfalsa già i coordinator di qualche *microsecondo*
(`DataUpdateCoordinator._microsecond`). Serve a non far partire tutto
nello stesso tick dello stesso event loop; non dice nulla su migliaia di
installazioni separate che arrivano all'API sullo stesso minuto tondo.

### Il backoff del 429 è un pavimento, non una sostituzione

La card dice «onorare `err.retry_after` prima del ciclo successivo». Preso
alla lettera sarebbe un difetto: il backoff parte da ~5 s
(`RATE_LIMIT_BACKOFF_START`) e raddoppia, quindi per la prima manciata di
429 è **molto più corto** dell'intervallo di poll. Sostituire l'intervallo
con `retry_after` significherebbe interrogare ogni 5 s un backend che ha
appena chiesto di rallentare — l'opposto di ciò che il 429 chiede.

Il prossimo ciclo parte quindi dopo `max(intervallo + offset, retry_after)`.
Sotto l'intervallo il backoff non cambia nulla (il caso comune: un 429
chiede ~5 s e il ciclo successivo era comunque a 5 minuti); sopra —
cioè dopo una serie di 429, quando il raddoppio supera i 300 s — è il
backoff a comandare. Pinnato in
`tests/test_coordinator.py::test_a_short_backoff_does_not_pull_the_next_cycle_forward`.

### Un 429 al primo poll resta un fallimento

Restituire i dati del ciclo precedente richiede che un ciclo precedente
esista. Al primo refresh non c'è, e non ci sono nemmeno entità da
proteggere: quel caso resta `UpdateFailed` → `ConfigEntryNotReady`, con il
retry di Home Assistant.

### Il 429 sulla chiamata dello schema

La card lascia aperto se onorare `retry_after` anche in
`async_setup_entry`, attorno a `async_load_schemas()`. **Deciso di no**
(con Piero, 2026-09-10): l'esito resta `ConfigEntryNotReady` e il retry
resta quello di Home Assistant, con il suo backoff. Il difetto che il
passo 3 vuole evitare non si presenta — al setup non ci sono entità da
marcare non disponibili — e un secondo percorso di retry accanto a quello
del framework aggiungerebbe codice senza aggiungere garanzie.

Quello che la clausola dedicata aggiunge è **leggibilità del log**: un 429
si riconosce come 429, con il ritardo che il client ha calcolato, invece
di sembrare uno schema endpoint giù.

### `UPDATE_TIMEOUT_FACTOR` resta 0.8

Rimane un rapporto, quindi ha seguito l'intervallo da solo: il budget di
un ciclo passa da 48 s a 240 s. È generoso per una o poche richieste, ed è
voluto — è una rete contro un ciclo appeso che si sovrappone al successivo,
non un timeout per richiesta (quello è `API.DEFAULT_TIMEOUT`). Il commento
in `const.py` è stato riscritto: descriveva ancora il pattern N+1, che
M-03 ha eliminato. Misurato dal vivo: **2.80 s, l'1.17% del budget**.

## Rilanciare tutte le verifiche

```bash
./scripts/verify_m05            # lint, suite, AC uno per riga, passata su dev
./scripts/verify_m05 --offline  # tutto tranne la passata su dev
```

Esce 1 al primo passo fallito e stampa l'elenco alla fine. Le credenziali
vengono dal proprio `.env`; gli override di pool sono già dentro lo script
e si cambiano con `M05_POOL_ID` / `M05_CLIENT_ID` / `M05_DOMAIN_PREFIX`.

L'unica verifica che nessuno script può fare è guardare il form:
`./scripts/develop`, poi **Impostazioni → Dispositivi e servizi → Radoff →
Configura**, e controllare che il campo proponga 300 e rifiuti 59.

## La verifica dal vivo

```bash
# credenziali nel proprio .env (gitignored), come RADOFF_USERNAME/RADOFF_PASSWORD.
# I due override di pool servono perché const.py punta ancora all'altro pool:
# vedi «Un residuo che blocca il collaudo end-to-end» in docs/M-04-verifica-dev.md.
python3 scripts/verify_m05_live.py \
    --pool-id eu-west-1_5SsvW9t6S \
    --client-id 2i63gbc9sim3b8paasaga7jb6g \
    --domain-prefix 875fe89b
```

### Cosa verifica, e perché serve una passata vera

Quasi tutto ciò che M-05 decide è locale e vive nella suite mockata, che lo
verifica meglio e senza toccare l'API. Quello che una suite mockata **non**
può verificare è il numero su cui la card poggia: che un ciclo, sull'API
vera e sul dominio vero, costi *una richiesta*. È l'affermazione che il
README fa in pubblico ed è la ragione per cui 300 s è difendibile su una
quota condivisa. Se il backend abbassasse il `page_size` massimo o
smettesse di servire la telemetria inline, la suite resterebbe verde e
quel numero diventerebbe falso.

| Controllo | Perché non basta un test mockato |
|---|---|
| Un ciclo costa una richiesta a `/data/devices` | è il presupposto di ogni numero della card |
| Il backend applica il `page_size` che chiediamo (200) | il tetto è del backend; se scendesse, il costo per ciclo cambierebbe in silenzio |
| Il setup costa una richiesta per **tipo**, non per device | la nota di M-04 sul budget, misurata invece che assunta |
| Il ciclo sta dentro il budget di `UPDATE_TIMEOUT_FACTOR` | il tempo di risposta è del backend |

### Cosa non verifica, e perché

- **Il 429.** Non è provocabile in modo onesto: il tetto è 50 req/s steady
  per *stage*, condiviso con l'app mobile e con il web (T-02), e saturarlo
  per vedere l'errore degraderebbe il servizio di chiunque altro stia
  usando dev. Compare come `skip` esplicito, mai come PASS. Il
  comportamento dopo un 429 è verificato in `tests/test_coordinator.py` con
  una response montata; la forma del corpo è quella documentata in T-02
  D-22 (`{"message": "Too Many Requests"}`, nessun `Retry-After`).
- **La cadenza reale nel tempo.** Osservare che due cicli distino davvero
  300 s vorrebbe dire tenere lo script acceso un quarto d'ora per guardare
  un timer di Home Assistant. È il framework a garantirlo; che il
  coordinator gli chieda l'intervallo giusto è verificato dentro `hass`.
- **L'offset fra installazioni.** È aritmetica locale sull'`entry_id`. Lo
  script la calcola su 1000 entry_id sintetici e la stampa, perché si legge
  meglio con i numeri davanti, ma è dichiarata come calcolo locale.

### Esito

Eseguita il **2026-09-10** contro dev (`https://v2.api.dev.iot.radoff.life`),
pool `eu-west-1_5SsvW9t6S`, dominio `875fe89b` — lo stesso con cui hanno
chiuso M-03 e M-04.

**5 PASS, 0 FAIL, 1 skip**, con l'handshake SRP vero (`--auth-flow password`
non serve più da RT-2952).

```
[  ok  ] L'offset disperde 1000 installazioni su [0, 30s)
          1000 valori distinti; min 0.01s, mediana 15.16s, max 29.99s
[ skip ] Un 429 salta il ciclo e non marca le entita' non disponibili
[  ok  ] Un ciclo di poll costa una richiesta (piu' una per pagina oltre la prima)
          2 device serviti in 1 richiesta/e a /data/devices (page_size=200), 2802 ms
[  ok  ] Il ciclo sta dentro il budget di 240s (S-13, riletto da M-05)
          misurato 2.80s, cioe' il 1.17% del budget
[  ok  ] Il backend applica il page_size richiesto (200)
          pagination servita: page_size=200, total=2, total_pages=1
[  ok  ] Il setup costa una richiesta per tipo di device, non per device
          2 device, 2 tipo/i (nowplus, sense), 2 richiesta/e; tetto del catalogo: 5
```

Il costo di questa installazione, nella forma in cui il backend ragiona
sulla quota condivisa: **una richiesta ogni 300 s** (più una per pagina
oltre la prima, cioè nessuna qui), **più due a ogni riavvio** — una per
tipo di device presente, con un tetto di cinque, quanti sono i tipi del
catalogo.

## Suite automatica

`355 passed` (`python3 -m pytest`), coverage 90%. I test aggiunti da questa
card:

| Test | AC coperto |
|---|---|
| `test_two_coordinators_created_together_do_not_poll_together` | «due coordinator creati nello stesso istante non pollano nello stesso istante» |
| `test_the_offset_never_shortens_the_interval_below_what_was_asked` | decisione sull'offset additivo (sopra) |
| `test_the_offset_of_an_entry_survives_a_restart` | l'offset non si ridisegna a ogni riavvio |
| `test_a_long_backoff_pushes_only_the_next_cycle_out` | il 429 sposta un ciclo, e `update_interval` non si muove |
| `test_a_short_backoff_does_not_pull_the_next_cycle_forward` | il backoff è un pavimento, non una sostituzione |
| `test_a_429_skips_the_cycle_without_making_entities_unavailable` | «un 429 salta il ciclo, non marca le entità non disponibili, e il ciclo successivo parte regolarmente» |
| `test_a_429_on_the_very_first_poll_retries_the_setup` | il caso in cui un 429 deve ancora fallire |
| `test_the_form_refuses_an_interval_below_the_floor` | «l'OptionsFlow non accetta valori sotto 60 s» |
| `test_an_entry_that_never_set_an_interval_polls_at_the_default` | «il default di una nuova entry è 300 s» |
| `test_a_429_on_the_schema_call_names_itself_and_retries` | il 429 al setup (decisione sopra) |

## Fuori scopo, confermato

- `POST /data/devices/status` come canale separato per la disponibilità
  (T-08 · D-35): contratto non ancora noto. Se la risposta arriva, si
  valuta in M-06.
- Import dello storico nelle statistiche long-term (T-08 · D-23).
- Passaggio ad aiohttp (voce "L" di `architettura-target-sprint-m.md` §11).
