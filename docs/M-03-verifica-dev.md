# M-03 — verifica dal vivo su dev

Esito dell'esecuzione di [`scripts/verify_m03_live.py`](../scripts/verify_m03_live.py)
contro `https://v2.api.dev.iot.radoff.life` il **2026-09-10**, prima di
chiudere la card RT-2941.

La suite automatica di M-03 gira col trasporto mockato: prova che il client
concorda con le fixture reali di M-01, non che concordi con l'API di oggi.
M-03 è anche il punto della serie in cui il pattern di chiamata cambia — da
`1 + N` a una sola `GET /data/devices` paginata — e quel cambio si guarda
una volta contro il backend vero.

## Esito 1 — l'integrazione, così com'è, non entra su dev

Con il flusso che l'integrazione usa davvero (SRP, pool di `const.py`):

```
log | INFO  api.auth: Token will expire in 86400 seconds
log | ERROR api.client: Failed to get domains: status=401,
      url=https://v2.api.dev.iot.radoff.life/data/user/me/domains,
      request_id=639d2818-885e-492d-98a8-08568f5917a6
```

Il login SRP **riesce** — il pool corrente ha `ALLOW_USER_SRP_AUTH` — ma
v2 dev **rifiuta quel token con 401**. È la conferma a runtime di D-24: su
dev valgono solo i token del pool dev, e l'app client di quel pool non ha
`ALLOW_USER_SRP_AUTH`, che è l'unica richiesta bloccante aperta da M-01.

**Conseguenza, e non riguarda solo M-03:** finché quel flag non viene
abilitato, nessuna versione dell'integrazione può autenticarsi su dev senza
modifiche. Non blocca questa card (il modello dati e la chiamata si
verificano lo stesso, vedi sotto) ma blocca qualunque prova end-to-end con
Home Assistant vivo, e va risolto prima di M-08.

Le verifiche qui sotto usano perciò `--auth-flow password`, che ottiene il
token con `USER_PASSWORD_AUTH` sul pool dev e lo inietta nella sessione.
**Scavalca l'handshake dell'integrazione**: quello che segue dice che la
chiamata dati e il modello sono giusti, non che l'autenticazione lo sia.

## Esito 2 — il censimento dei domini, e perché serve

Lo script censisce i domini invece di prendere il primo: M-01 aveva scelto
un dominio i cui device erano tutti muti e c'era rimasta con due verifiche
non osservate. Sui 15 domini dell'account dev:

| dominio | device supportati (`nowplus`) | di cui senza telemetria |
|---|---:|---:|
| `5d2d8275` | 10 | 10 |
| `5debef25` | 2 | 2 |
| `875fe89b` | 1 | 0 |
| `88c72bef` | 1 | 1 |
| `9a8e7728` | 1 | 1 |
| altri 10 | 0 | 0 |

15 device supportati in tutto, **14 dei quali muti**: la conferma su un
campione più largo di D-16 (`telemetry: null` è la condizione della
maggioranza). E nessun dominio ha insieme un device che trasmette e uno
muto, quindi la copertura completa richiede due passate — lo script lo dice
in output invece di lasciarlo dedurre dagli `skip`.

## Esito 3 — gli AC della card, verificati sul payload vero

**Passata su `875fe89b`** (il dominio con il device che trasmette): 11 PASS,
0 FAIL, 1 skip.

```
[ ok ] get_devices() completa senza sollevare - 1 device in 1400 ms
[ ok ] Un ciclo fa 1 richiesta a /data/devices, nessuna per device
[ ok ] Nessuna chiamata agli endpoint di arch 1.x
[ ok ] Ogni device ha un serial_number come identita'          3D90E0
[ ok ] Ogni device modellato e' di un tipo supportato          nowplus
[ ok ] I campi che il payload 2.0 ha aggiunto arrivano nel modello
       connection_status, firmware_version, room_name, room_slug,
       building_name, building_slug, domain_prefix = 1/1
[ ok ] Ogni lettura e' archiviata sotto il proprio nome di campo
[ ok ] `timestamp` e `device_type` non diventano letture
[ ok ] measured_at e' il timestamp del blocco, uguale per ogni campo
       eta' della telemetria: 3D90E0 0 min
[ ok ] Valori gia' nell'unita' dichiarata (nessuna scala applicata)
       3D90E0 internal_temperature=25.9583 degC
       3D90E0 pressure=100507.0 Pa
[ ok ] unique_id e' radoff-{serial_number}-{campo}
       radoff-3D90E0-aqi_value
[skip] Un device con telemetry: null produce stale=True
```

**Passata su `9a8e7728`** (il dominio con il device muto), che chiude
l'unico controllo rimasto:

```
[ ok ] I 1 device senza telemetria sono stale, con letture vuote   23FDA0
```

Le due passate insieme coprono ogni criterio di accettazione della card.

Due cose che vale la pena leggere due volte:

- **`internal_temperature = 25.9583 °C` e `pressure = 100507.0 Pa`** sul
  device vero, un minuto dopo la misura. È la prova che togliere il fattore
  `0.00835` era giusto: con quel fattore la stessa lettura sarebbe stata
  0.22 °C. V7 e V8 di M-01 confermate a runtime, non solo da fixture.
- **`radoff-3D90E0-aqi_value`** è un `unique_id` costruito dal codice vero
  (`RadoffEntity.unique_id`) su un serial vero.

## Esito 4 — lo slave LIFE esiste davvero, e attraversa i domini

Durante il censimento, senza che fosse cercato:

```
log | INFO api.client: Device B12428 controls a nested life device
      (207698, domain 9a8e7728) that this version does not model: ignored.
      Modelling the controller/slave pair is tracked as T-08 D-33
log | INFO api.client: Device F9C400 controls a nested life device
      (ECFB4C, domain ef1d598b) that this version does not model: ignored.
      Modelling the controller/slave pair is tracked as T-08 D-33
```

Due controller con un `life` annidato, e **il secondo caso attraversa i
domini**: `F9C400` è stato censito in un dominio diverso da `ef1d598b`, che
è il dominio dello slave che porta inline. È D-33 osservato dal vivo, non
più solo dalla passata `superadmin` di M-01 le cui fixture erano state
scartate.

L'AC «un LIFE annidato produce una riga di log e nessuna entità, senza
eccezioni» è quindi verificato su dati reali: due righe, nessun device
aggiunto alla lista, nessuna eccezione.

## Cosa resta non verificato

- **L'autenticazione dell'integrazione su dev** (Esito 1). Bloccata da
  `ALLOW_USER_SRP_AUTH`, richiesta aperta di M-01.
- **Il wiring lato Home Assistant** — entità nel registry, availability,
  `sw_version` sulla pagina device. Serve un HA vivo, che oggi non riesce ad
  autenticarsi: lo coprono i test in `tests/test_sensor.py` e
  `tests/test_coordinator.py`, che girano dentro `hass` col trasporto
  mockato.
- **La paginazione su più di una pagina.** Nessun dominio di dev ha più di
  200 device, quindi `total_pages` è sempre 1 nel mondo reale: il loop è
  esercitato solo dai test. La passata `superadmin` di M-01 su 83 device è
  l'evidenza più vicina che abbiamo, e non è riproducibile con le fixture
  correnti.

## Come si rigenera

```bash
# le TUE credenziali, in .env (gitignored) come RADOFF_USERNAME/RADOFF_PASSWORD
python3 scripts/verify_m03_live.py \
    --auth-flow password \
    --pool-id eu-west-1_5SsvW9t6S \
    --client-id 2i63gbc9sim3b8paasaga7jb6g

# la meta' che il dominio scelto non copre
python3 scripts/verify_m03_live.py ... --domain-prefix 9a8e7728
```

Quando `ALLOW_USER_SRP_AUTH` sarà abilitato sul pool dev, `--auth-flow` e i
due override di pool spariscono e la passata verifica anche l'autenticazione:

```bash
python3 scripts/verify_m03_live.py
```
