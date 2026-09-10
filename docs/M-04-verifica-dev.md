# M-04 (RT-2942) — verifica dal vivo e scostamenti

Card: **[M-04] Entità schema-driven da measures-ranges: via MAPPING e
INDEX_MAPPING**. Fase 4 di 8 della migrazione ad arch 2.0.
Riferimenti: T-02 (RT-2810), T-08 (RT-2938). Dipende da M-03 (RT-2941).

Questo documento tiene due cose che la card da sola non tiene: **cosa è
stato escluso dallo scopo, e da chi**, e **come si verifica dal vivo** ciò
che una suite mockata non può verificare.

## Scostamenti dagli AC della card

### `sismoff` fuori scopo — decisione del 2026-09-10

L'AC *"con le fixture reali di M-01, le entità generate per un sense e per
un sismoff corrispondono ai campi dichiarati da measures-ranges per quei
tipi"* è verificato **solo per `sense`** (e per `nowplus`, che la card non
chiedeva ma che è il tipo supportato). Decisione presa da Piero il
2026-09-10, tracciata qui perché la card non venga chiusa lasciando
credere che il sismoff sia stato guardato.

**Perché.** Le fixture di M-01 contengono lo *schema* del sismoff
(`tests/fixtures/dev/measures_ranges__sismoff.json`, 9 misure fra cui `co`
e `ch4`) ma **nessun device sismoff**: la pagina catturata su dev
(`devices__full.json`) ha un `nowplus` e un `sense`. Verificare quell'AC
avrebbe richiesto o un payload device sintetico costruito a mano dallo
schema — che avrebbe verificato il nostro stesso codice contro una nostra
invenzione — o una nuova passata su dev su un dominio che un sismoff ce
l'abbia.

**Cosa è comunque coperto**, senza che nessuno debba rifare il lavoro:

- lo schema `sismoff` è letto, ordinato e confrontato con la fixture da
  `scripts/verify_m04_live.py` insieme agli altri quattro tipi;
- `co` e `ch4` — le due misure che solo il sismoff ha — hanno device_class
  (`co`) e traduzioni in italiano e inglese (entrambe, più i loro
  `_index`), e `test_translations.py` le tiene allineate perché legge
  l'unione del catalogo, non un tipo solo;
- il codice non filtra per tipo (M-04 ha tolto quel filtro), quindi un
  sismoff che comparisse su un account otterrebbe le entità del suo schema
  senza alcuna modifica.

**Cosa manca, per chi riprenderà il filo.** Solo la prova sul campo: un
payload `GET /data/devices` che contenga un sismoff, e l'asserzione che le
entità generate coincidano con le 9 misure del suo schema. È una
parametrizzazione in più in
`tests/test_sensor.py::test_entities_match_the_measures_the_type_declares`
— la funzione è già parametrica sui tipi — più la fixture del device. Va
aperta come card a sé o riportata sulla card che estenderà il supporto
oltre Now+.

### Ampiezza del rilascio

Tolto il filtro per `type` (AC *"un device_type sconosciuto non fa
scomparire il device"*), l'integrazione crea entità anche per `sense`,
`city`, `now` e `sismoff`. **Now+ resta l'unico tipo supportato e
verificato**, ed è ciò che il README promette: gli altri ottengono le
entità che il loro schema dichiara, senza garanzia di copertura. Il README
e `docs/M-03-verifica-dev.md` sono stati aggiornati di conseguenza.

### TVOC: unità e statistiche

`tvoc` perde unità (`µg/m³`) e device_class (`volatile_organic_compounds`),
perché l'API dichiara `V - Ix`, che non è una concentrazione (T-02 D-07).
Su un'installazione che aggiorna, Home Assistant tratta il cambio di unità
su un'entità esistente come una rottura delle statistiche a lungo termine.
Documentato in `schema.py` e nel README, e **segnalato a M-07**, che decide
il re-keying degli `unique_id` esistenti e quindi eredita il caso.

## La verifica dal vivo

```bash
# credenziali nel proprio .env (gitignored), come RADOFF_USERNAME/RADOFF_PASSWORD
python3 scripts/verify_m04_live.py

# finché ALLOW_USER_SRP_AUTH non è abilitato sul pool dev (richiesta di M-01)
python3 scripts/verify_m04_live.py \
    --auth-flow password \
    --pool-id eu-west-1_5SsvW9t6S \
    --client-id 2i63gbc9sim3b8paasaga7jb6g

# su un dominio specifico, per i due controlli che guardano device veri
python3 scripts/verify_m04_live.py ... --domain-prefix 875fe89b
```

### Cosa verifica, e perché serve una passata vera

La suite automatica gira contro le fixture di M-01: prova che il codice
concorda con la response del 10 settembre 2026, non con quella di oggi. Ed
è il punto della card — unità, etichette e soglie non sono più nostre. Se
il backend le cambia, **la suite resta verde e l'integrazione cambia
comportamento**: è il pregio di questa architettura, ma la card va chiusa
sapendo se è già successo.

| Controllo | Perché non basta un test mockato |
|---|---|
| Lo schema risponde per tutti e 5 i tipi del catalogo | il catalogo è del backend |
| Misure ordinate per `pos`, e `pos` **ancora sparso** | se diventasse contiguo il nostro test non dimostrerebbe più nulla, e va saputo |
| Fasce nell'ordine servito (`high` in seconda posizione) | è la scelta del backend, non nostra |
| Nessuno `scaleFactor` servito | il giorno che comparisse, ogni valore di quella misura sarebbe sbagliato |
| Ogni unità servita è mappata; `pressure` in Pa | l'enumerazione delle unità è del backend |
| `radon`/`aqi`/`ch4`/`tvoc` senza device_class, `aqi_value` disabilitata | invarianti nostre, ricontrollate contro il catalogo vero |
| **Deriva rispetto alle fixture di M-01** | il controllo che nessun test mockato può fare |
| Ogni misura del catalogo ha un nome in it/en | una misura aggiunta dal backend produce un'entità senza nome |
| Tipo ignoto → 404 con `available` | il corpo dell'errore è del backend |
| Nessun device sparisce; letture dentro lo schema | serve un dominio con device veri |

Non verifica il wiring di Home Assistant (quali entità nascono, i nomi,
l'AQI disabilitata): serve un HA vivo, e lo coprono `tests/test_sensor.py`
e `tests/test_init.py`, che girano dentro `hass`.

### Esito

Eseguita il **2026-09-10** contro dev (`https://v2.api.dev.iot.radoff.life`),
pool `eu-west-1_5SsvW9t6S`, con `--auth-flow password`.

**17 PASS, 0 FAIL, 0 skip.**

Due passate, e la prima serve a spiegare la seconda. Senza
`--domain-prefix` lo script prende il primo dominio della discovery, che su
questo account è `1aeb7ad1` e non ha device: 15 PASS e uno `skip` sui due
controlli che hanno bisogno di device veri. Ripetuta su `875fe89b` — lo
stesso dominio con cui M-03 aveva chiuso, l'unico con un device che
trasmette — copre tutto.

```
[  ok  ] Lo schema risponde per tutti e 5 i tipi del catalogo
          nowplus=9 misure, sense=10 misure, city=10 misure, now=6 misure, sismoff=9 misure
          5 tipi letti in 592 ms
[  ok  ] Le 5 richieste della passata sono tutte allo schema
[  ok  ] Le misure escono ordinate per `pos`
          nowplus: pos=[1, 3, 6, 7, 8, 9, 10, 11, 12]
          sense:   pos=[1, 2, 3, 6, 7, 8, 9, 10, 11, 12]
          city:    pos=[1, 2, 3, 6, 7, 8, 9, 10, 11, 12]
          now:     pos=[1, 3, 6, 10, 11, 12]
          sismoff: pos=[1, 2, 3, 4, 5, 6, 10, 11, 12]
[  ok  ] `pos` e' ancora sparso su almeno un tipo (la trappola e' viva)
          tipi con buchi: nowplus, sense, city, now, sismoff
[  ok  ] Le fasce arrivano nel vocabolario e nell'ordine attesi
          excellent -> high -> good -> poor -> terrible
          low -> good -> high
[  ok  ] Nessuna misura serve uno `scaleFactor` (i valori sono gia' scalati)
[  ok  ] Tutte le 8 unita' servite sono mappate su Home Assistant
          '%', '', 'Bq/m³', 'Pa', 'V - Ix', 'ppm', '°C', 'µg/m³'
[  ok  ] AC: `pressure` e' servita in Pa (la conversione la fa la UI di HA)
[  ok  ] radon/aqi/ch4/tvoc restano senza device_class
[  ok  ] Ogni device_class mappata corrisponde a una misura che il catalogo serve
[  ok  ] AC: `aqi_value` nasce disabilitata (T-08 D-08, divisore 120)
[  ok  ] Lo schema servito coincide con le fixture di M-01 (5 tipi)
          nessuna deriva: soglie, unita' ed etichette sono quelle della card
[  ok  ] Le 23 entita' del catalogo hanno un nome in ogni lingua
[  ok  ] Tipo 'life': 404 con la lista dei tipi validi
          available=['city', 'now', 'nowplus', 'sense', 'sismoff']
[  ok  ] Tipo 'not-a-real-device-type': 404 con la lista dei tipi validi
          available=['city', 'now', 'nowplus', 'sense', 'sismoff']
[  ok  ] I 2 device del dominio sono tutti modellati
          tipi presenti: ['nowplus', 'sense']; fuori dal tipo supportato (nowplus): ['sense']
[  ok  ] Ogni lettura dei device e' dichiarata dallo schema del suo tipo

  17 PASS, 0 FAIL, 0 skip
```

### Cosa dice questa passata, oltre al conteggio

**Nessuna deriva.** Lo schema che dev serve oggi è identico, campo per
campo, alle fixture catturate da M-01: le soglie, le unità e le etichette
su cui questa card è stata scritta sono ancora quelle. È il controllo che
nessun test mockato può fare, ed è la ragione principale per cui questa
passata esisteva.

**La trappola di `pos` è viva su tutti e cinque i tipi.** I buchi ci sono
davvero (`now`: 1, 3, 6, 10, 11, 12), quindi il test che li esercita
continua a dimostrare qualcosa.

**L'AC sul filtro è confermato dal vivo.** Il dominio ha due device, un
`nowplus` e un `sense`, ed entrambi sono modellati. Prima di M-04 il
`sense` sarebbe semplicemente sparito: è lo stesso device che
`docs/M-03-verifica-dev.md` contava fuori dai "device supportati".

**Nessuna lettura fuori schema.** Su questi due device ogni campo della
telemetria è dichiarato dallo schema del suo tipo, quindi il ramo che
espone un campo non dichiarato come valore grezzo — quello che esiste per
`radon_status` — su dev non si attiva. Resta coperto dai test.

### Cosa questa passata NON verifica

- **L'autenticazione.** Con `--auth-flow password` l'handshake SRP
  dell'integrazione è scavalcato e il token iniettato, perché l'app client
  del pool dev non ha ancora `ALLOW_USER_SRP_AUTH` (richiesta aperta di
  M-01). Un PASS qui non è un PASS sull'autenticazione, e lo script lo
  dichiara a schermo.
- **Il wiring di Home Assistant**: quali entità nascono, come si chiamano,
  quali sono disabilitate. Serve un HA vivo; lo coprono
  `tests/test_sensor.py` e `tests/test_init.py`, che girano dentro `hass`.
- **`sismoff`**: vedi "Scostamenti" più sopra. Il suo schema è stato letto
  e confrontato, i suoi device no — su dev non ce ne sono.
