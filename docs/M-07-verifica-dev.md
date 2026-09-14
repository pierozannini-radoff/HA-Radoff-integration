# M-07 (RT-2946) — decisioni, scostamenti e verifica dal vivo

Card: **[M-07] Config flow su `domain_prefix` e migrazione delle config
entry a VERSION 3**. Fase 7 di 8 della migrazione ad arch 2.0.
Riferimenti: T-02 (RT-2810), T-08 (RT-2938), S-01 (RT-2803), S-02 (RT-2804)
e RT-2926/RT-2927 (difetti T-06). Dipende da M-04 (RT-2942), va prima di
M-08 (RT-2947).

È la card irreversibile della serie: tutto il resto si può rifare, una
migrazione del registry sbagliata lascia gli utenti senza storico e senza
automazioni. Questo documento tiene le due cose che la card da sola non
tiene: **le decisioni prese durante l'implementazione, e da chi**, e **cosa
si è misurato dal vivo** di ciò che una suite mockata può solo assumere.

## Decisioni prese in implementazione

Le quattro decisioni sotto sono state poste prima di scrivere una riga, il
2026-09-14, e decise con Piero.

### 1. Il tvoc: pulire il registry, annunciare, e pubblicare `V - Ix`

M-04 aveva lasciato il caso a questa card (`schema.py`, commento su
`HA_UNITS`): nella versione rilasciata il tvoc era µg/m³ con device class
`volatile_organic_compounds`, in arch 2.0 l'API dichiara `V - Ix`, che non
è una concentrazione (T-02 D-07), e M-04 aveva scelto di pubblicarlo **senza
unità**.

**Decisione: tre cose insieme.** Al momento del re-keying si azzera
l'override di unità di visualizzazione che l'utente può aver impostato
(`options["sensor"]["unit_of_measurement"]`), la rottura delle statistiche
si annuncia nel README, e l'unità pubblicata diventa la stringa `V - Ix`
che l'API dichiara, ribaltando la scelta di M-04.

Il motivo del ribaltamento: un numero nudo senza unità *sembra* una
concentrazione a cui è sparita l'unità, mentre `V - Ix` dice a chi guarda
che quella grandezza non è un µg/m³. Il device class resta assente, ed è
proprio ciò che rende legale una stringa arbitraria come unità — la stessa
latitudine che usa `Bq/m³`.

**Ciò che questa card non fa, e va detto:** non ripara le statistiche a
lungo termine. Vivono nel recorder (`statistics_meta`), non nell'entity
registry, e Home Assistant tratta per progetto un cambio di unità come una
rottura della serie. Riscrivere lo storico registrato di un utente non è
qualcosa in cui una migrazione di config entry debba mettere le mani: il
buco si annuncia, come lo scalino di ~4 °C sulla temperatura.

### 2. Rimozioni dal registry: solo una lista esplicita

La card chiede che «le entità del bucket aggregato che non hanno
corrispondente in 2.0 vanno rimosse dal registry, non lasciate unavailable
per sempre», e in un AC lo dice più largo: «le entità `*_average` […]
risultano rimosse».

**Decisione: si rimuove solo da una lista scritta a mano, e solo nel caso di
collisione.** Nessuna euristica del tipo «tutto ciò che non corrisponde al
nuovo schema», che rischierebbe di cancellare le entità di un device assente
dal poll in quel momento.

Leggendo le risposte di arch 1.x invece di assumerle, la lista ha **un solo
elemento**: il bucket `aggregatedData` ha sempre portato solo
`airqualityindex` (30e0cde, `api.py::MAPPING`), quindi
`airqualityindex_average` è l'unico identificatore `*_average` che
un'installazione possa avere — e ha un corrispondente, `aqi_value`, quindi
l'esito normale è che venga **ri-chiavato e conservi il suo storico**.

La rimozione scatta in un caso solo: un'istanza che ha girato una build
intermedia di questo sprint ha *entrambe* le entità AQI, e entrambe puntano
a `aqi_value`. L'ordine di migrazione fa vincere la preesistente (quella
con lo storico) e la perdente è ciò che l'AC descrive: un'entità che non
riceverà mai più un valore. Nessuna versione rilasciata può produrre quello
stato.

### 3. L'entry va a VERSION 3 anche senza dominio

Una entry 1→3 non ha dominio; una 2.x→3 ha un UUID che arch 2.0 non
accetta e che nessuno può tradurre offline. In entrambi i casi la entry
arriva a versione 3 **senza dominio**, il setup si ferma con un
`ConfigEntryError` tradotto e la Repairs issue porta l'utente alla scelta.

**Decisione: si bumpa comunque.** Le due metà sono indipendenti — il
re-keying non ha bisogno del dominio, solo del device registry — e una
entry lasciata alla versione vecchia rifarebbe l'intera migrazione a ogni
riavvio, ri-loggando ogni warning, con Home Assistant che registra una
migrazione fallita ogni volta, mentre il lavoro sulle entità è già fatto.
Un dominio mancante non è una migrazione fallita: è una domanda in attesa
del suo utente.

Una conseguenza la card non la nominava e vale la pena scriverla: **una
destinazione occupata non fa più ritentare la migrazione.** RT-2927
lasciava la entry sotto la minor version apposta, perché il conflitto
poteva sciogliersi da solo. Qui no: la entry è a 3 e il conflitto, se
c'è, resta loggato come WARNING su un'entità che è ancora lì da guardare.

### 4. Il test «su un backup reale»: ricostruzione fedele + passata a mano

**Decisione: una fixture che ricostruisce i 32 `unique_id` misurati da
RT-2827** (`docs/T-06-evidenze.md`, «AC1 — i 32 `unique_id` confrontati») —
due device, sedici entità ciascuno, gli `entity_id` italiani che i nomi
della versione rilasciata producevano, il suffisso `-index` sui sette
fratelli qualitativi e l'AQI senza fratello — **più la passata a mano su
un'istanza vera**, che chiude M-08.

Gli identificatori nella fixture sono sintetici (gli UUID di device di un
account reale non si committano); la **forma** è quella misurata, ed è la
forma ciò che la migrazione deve azzeccare.

## Scostamenti dalla card

**Una chiamata in più nel config flow.** La card chiede un abort dedicato
per «zero domini o zero device» (T-08 D-32). Zero domini si vede dalla
discovery; zero device no, il flow non guardava i device. Ora, scelto il
dominio, il flow fa una `GET /data/devices` sulla sessione che
`validate_input` ha già aperto. **Solo una risposta vuota è una risposta**:
qualunque fallimento di quella chiamata (429, timeout, 500) vale «nessuna
opinione» e il setup prosegue, perché rifiutare di creare la entry per un
429 transitorio sarebbe un guasto peggiore di quello che si vuole evitare —
e il primo refresh del coordinator gestisce lo stesso errore molto meglio,
con retry e spiegazione.

**Un modulo nuovo, `issues.py`.** Le due Repairs issue servono a
`__init__.py` *e* a `coordinator.py`, e `__init__.py` importa
`coordinator.py`: gli helper non potevano restare dove RT-2926 li aveva
messi.

**Due issue, non una.** «Questa entry non ha mai avuto un dominio» e
«l'account ha perso l'accesso al dominio che aveva» aprono lo stesso flusso
e sono la stessa riparazione, ma non la stessa frase: chi aveva
un'installazione funzionante ieri merita di leggere la seconda.

**Il 403 diventa interattivo.** È la voce che la nota di M-02 chiedeva di
aggiungere al passo 3. L'errore tradotto resta (è ciò che si vede sulla
scheda dell'integrazione, dove una Repairs issue non è visibile) e accanto
nasce la issue riparabile. Prima l'unico rimedio era rimuovere e
riaggiungere l'integrazione, cioè buttare lo storico di ogni entità: il
contrario di ciò per cui esiste questa card.

**L'AC «nessuna occorrenza» verificato con l'AST, non con un grep.**
`tests/test_init.py::test_the_arch_1x_domain_names_are_gone_from_the_code`
cammina l'albero sintattico dei moduli dell'integrazione e cerca *usi* —
import, letture, accessi ad attributo. Diversi commenti nominano ancora
`CONF_DOMAIN_ID`, ed è giusto così: una migrazione è l'unico posto che deve
spiegare cosa ha sostituito, e il commento in `const.py` è ciò che impedisce
al prossimo lettore di reintrodurlo.

## Verifica dal vivo su dev — 2026-09-14

`scripts/verify_m07_live.py`, host `v2.api.dev.iot.radoff.life`, pool
`eu-west-1_5SsvW9t6S`, dominio `875fe89b`.

**5 PASS, 0 FAIL, 0 skip.**

| # | Controllo | Esito |
|---|-----------|-------|
| 1 | Discovery col solo bearer su `/data/user/me/domains` | ok — 1 dominio, nessun header di dominio inviato |
| 2 | Forma annidata `{"domain": {...}, "role": {...}}` | ok — 1/1 elementi con `domain.prefix` |
| 3 | L'etichetta `name` è diversa dal prefisso | ok — 1/1 con etichetta propria, 1/1 prefissi sono frammenti di UUID |
| 4 | Il dominio scelto ha dei device | ok — 2 device su `875fe89b` |
| 5 | Un `domain_prefix` altrui risponde 403 | ok — `Forbidden: you do not belong to domain 'zzzzzzzz'` |

Tre cose che questa passata dice e che vale la pena tenere:

- **il rapporto della riserva 20 regge.** M-01 aveva contato 14 prefissi su
  15 che sono frammenti di UUID; questo account ne vede uno solo, ed è un
  frammento di UUID anche lui, con un `name` leggibile accanto. La scelta di
  etichettare con `name` non è un'ipotesi;
- **l'account di dev vede un dominio, non quindici.** M-01 sondava con
  un'utenza che ne raggiungeva quindici: la differenza è di permessi, non di
  API. Conseguenza pratica per il collaudo di M-08: con questo account **lo
  step di scelta del dominio non compare**, quindi l'AC1 si vede dal vivo e
  l'AC sul multi-dominio no — quello resta coperto dalla suite;
- **il 403 arriva con un messaggio esplicito del backend** (`Forbidden: you
  do not belong to domain '…'`), non con un 401 né con un 200 filtrato male.
  È il presupposto dell'intero flusso Repairs sul 403, ed è verificato
  contro l'API vera, non contro una fixture.

## Cosa resta fuori, e dove

- **La migrazione su un backup vero.** È il punto 3 degli AC e la ragione
  per cui questa card è irreversibile. La suite la esercita sulla
  ricostruzione dei 32 identificatori; la passata su un ripristino reale è
  M-08. La checklist è in coda a `./scripts/verify_m07`.
- **Il flusso Repairs end to end nell'interfaccia.** Verificato in
  `tests/test_repairs.py` attraverso il vero flow manager; a mano si guarda
  con `./scripts/develop`.
- **Il multi-dominio dal vivo.** Vedi sopra: serve un'utenza che raggiunga
  più di un dominio su dev.
