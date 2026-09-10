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

**Passata non ancora eseguita.** Lo script è stato provato solo nella sua
metà offline — i controlli che non toccano la rete, eseguiti contro le
fixture di M-01: **12 PASS, 0 FAIL, 1 skip** (lo skip è il controllo sui
device, che senza dominio non ha nulla da guardare). Restano da eseguire
contro dev i tre che richiedono la rete: lettura del catalogo, 404 sul tipo
ignoto, e i device del dominio.

Quando la passata verrà fatta, il verdetto va incollato qui sotto, come
hanno fatto M-01 e M-03.

```
(da compilare)
```
