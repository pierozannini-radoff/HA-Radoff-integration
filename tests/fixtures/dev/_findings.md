# M-01 - esiti della ricognizione

Host: `https://v2.api.dev.iot.radoff.life` - generato da `scripts/probe_arch2.py` il 2026-09-10 09:21 UTC.

## D-24 - Il token del pool Cognito attuale e' accettato dall'host v2?

**Esito:** NON DETERMINATO: il login SRP sul pool attuale non ha prodotto un token (InvalidParameterException), quindi l'host v2 non e' mai stato interrogato

<details><summary>Evidenza</summary>

```json
{
  "pools": [
    {
      "label": "pool_current",
      "pool_id": "eu-west-1_zD4CSIZ6i",
      "client_id": "61ckd0c4qoq0ov7mmphrj7kstj",
      "region": "eu-west-1",
      "login_ok": false,
      "login_error": "InvalidParameterException: An error occurred (InvalidParameterException) when calling the InitiateAuth operation: USER_PASSWORD_AUTH flow not enabled for this client",
      "cognito_error_code": "InvalidParameterException",
      "flusso_usato": null,
      "token_expires_in_seconds": null,
      "v2_domains_status": null,
      "auth_header_format_accettato": null
    },
    {
      "label": "pool_dev",
      "pool_id": "eu-west-1_5SsvW9t6S",
      "client_id": "2i63gbc9sim3b8paasaga7jb6g",
      "region": "eu-west-1",
      "login_ok": true,
      "login_error": null,
      "cognito_error_code": null,
      "flusso_usato": "password",
      "token_expires_in_seconds": 3600,
      "v2_domains_status": 200,
      "auth_header_format_accettato": "bearer"
    }
  ],
  "accettati": [
    "pool_dev"
  ],
  "rifiutati": [
    "pool_current=login fallito"
  ]
}
```

</details>

## D-28 - L'app client Cognito permette il flusso SRP che l'integrazione usa?

**Esito:** SRP NON abilitato sull'app client di pool_dev. Verificato con un utente INESISTENTE, quindi la causa e' l'app client e non l'utenza: serve `ALLOW_USER_SRP_AUTH` su quel client, oppure il client id di un altro app client dello stesso pool che lo abbia gia'

<details><summary>Evidenza</summary>

```json
{
  "flussi_per_app_client": [
    {
      "pool": "pool_current",
      "client_id": "61ckd0c4qoq0ov7mmphrj7kstj",
      "flussi": {
        "USER_SRP_AUTH": "abilitato (risposta: NotAuthorizedException)",
        "USER_PASSWORD_AUTH": "NON abilitato",
        "REFRESH_TOKEN_AUTH": "abilitato (risposta: UnsupportedOperationException)"
      }
    },
    {
      "pool": "pool_dev",
      "client_id": "2i63gbc9sim3b8paasaga7jb6g",
      "flussi": {
        "USER_SRP_AUTH": "NON abilitato",
        "USER_PASSWORD_AUTH": "abilitato (risposta: NotAuthorizedException)",
        "REFRESH_TOKEN_AUTH": "abilitato (risposta: NotAuthorizedException)"
      }
    }
  ],
  "per_pool": [
    {
      "label": "pool_current",
      "pool_id": "eu-west-1_zD4CSIZ6i",
      "client_id": "61ckd0c4qoq0ov7mmphrj7kstj",
      "login_ok": false,
      "cognito_error_code": "InvalidParameterException",
      "errore": "InvalidParameterException: An error occurred (InvalidParameterException) when calling the InitiateAuth operation: USER_PASSWORD_AUTH flow not enabled for this client",
      "token_expires_in_seconds": null
    },
    {
      "label": "pool_dev",
      "pool_id": "eu-west-1_5SsvW9t6S",
      "client_id": "2i63gbc9sim3b8paasaga7jb6g",
      "login_ok": true,
      "cognito_error_code": null,
      "errore": null,
      "token_expires_in_seconds": 3600
    }
  ]
}
```

</details>

## D-24-surface - Quali base path dell'host v2 accettano il token, e quali no?

**Esito:** accettano: /analytics/*, /data/*

<details><summary>Evidenza</summary>

```json
{
  "/data/*": {
    "status": {
      "200": 11,
      "404": 1,
      "403": 1
    },
    "errortype": [],
    "auth_style_provati": [
      "bearer"
    ]
  },
  "/analytics/*": {
    "status": {
      "200": 6,
      "404": 2
    },
    "errortype": [],
    "auth_style_provati": [
      "bearer"
    ]
  }
}
```

</details>

## D-03 - `GET /auth/user/me/domains` e' chiamabile con il solo bearer token, come previsto dal config flow?

**Esito:** chiamabile con il solo bearer token

<details><summary>Evidenza</summary>

```json
{
  "tentativi": [
    {
      "auth_style": "bearer",
      "status": 200,
      "errortype": null,
      "body": {
        "email": "**REDACTED**",
        "domains": [
          {
            "domain": {
              "prefix": "875fe89b",
              "name": "<label-1>",
              "type": "customer",
              "parent_domain_prefix": "fdffb4d9",
              "created_at": "2026-04-09T12:50:06"
            },
            "role": {
              "code": "user",
              "name": "User",
              "description": "Standard user access"
            },
            "assigned_at": "2026-04-09T12:51:36"
          }
        ],
        "pagination": {
          "page": 1,
          "page_size": 20,
          "total": 1,
          "total_pages": 1
        }
      }
    }
  ],
  "path_alternativi": [],
  "errortype_per_base_path": {
    "/data/*": [
      "applicativo/200"
    ]
  },
  "base_path_dietro_authorizer": [],
  "base_path_solo_gateway": []
}
```

</details>

## D-30 - Qual e' il nome dell'header di request id nelle risposte?

**Esito:** x-amz-apigw-id, x-amzn-requestid, x-amzn-trace-id

<details><summary>Evidenza</summary>

```json
{
  "header_candidati": {
    "x-amzn-requestid": "a4cf89d5-a24d-4cd5-9e33-31a0ffe4c583",
    "x-amz-apigw-id": "Ded4xEP2joEEdBw=",
    "x-amzn-trace-id": "Root=1-6aa2769e-0fcf52053a5d520048d3c094;Parent=7f7df18d886e8c71;Sampled=0;Lineage=1:43968834:0"
  },
  "header_visti_su_una_risposta": [
    "access-control-allow-headers",
    "access-control-allow-methods",
    "access-control-allow-origin",
    "connection",
    "content-length",
    "content-type",
    "date",
    "x-amz-apigw-id",
    "x-amzn-errortype",
    "x-amzn-requestid",
    "x-amzn-trace-id"
  ]
}
```

</details>

## D-29 - Tassonomia degli errori: status e forma del body per ogni caso.

**Esito:** 5 casi provocati

<details><summary>Evidenza</summary>

```json
[
  {
    "caso": "error__measures_ranges_unknown_type",
    "status": 404,
    "chiavi_body": [
      "available",
      "error",
      "message"
    ],
    "body": {
      "error": "Device type not found",
      "message": "No schema found for device_type 'not-a-real-device-type'",
      "available": [
        "city",
        "now",
        "nowplus",
        "sense",
        "sismoff"
      ]
    }
  },
  {
    "caso": "error__device_detail_unknown_serial",
    "status": 404,
    "chiavi_body": [
      "error"
    ],
    "body": {
      "error": "Device 'RADOFF-NOT-A-REAL-SERIAL-0000' not found"
    }
  },
  {
    "caso": "error__devices_foreign_domain",
    "status": 403,
    "chiavi_body": [
      "error"
    ],
    "body": {
      "error": "Forbidden: you do not belong to domain 'radoff-hq'"
    }
  },
  {
    "caso": "error__devices_no_domain_prefix",
    "status": 200,
    "chiavi_body": [
      "devices",
      "pagination"
    ],
    "body": {
      "devices": [
        {
          "serial_number": "3D90E0",
          "type": "nowplus",
          "name": "<label-2>",
          "status": "active",
          "room_slug": "default-room",
          "domain_prefix": "875fe89b",
          "created_at": "2026-09-09T10:07:42",
          "updated_at": "2026-09-09T22:30:42",
          "room_name": "<label-3>",
          "building_slug": "default-building",
          "building_name": "<label-3>",
          "connection_status": "connected",
          "connection_status_updated_at": "2026-09-09T10:06:41",
          "latitude": 44.5,
          "longitude": 11.3,
          "notes": null,
          "firmware_version": "0.2.8",
          "icon_color_variant_id": null,
          "config_status": "confirmed",
          "managed_by_device_serial": null,
          "controller_of_device_serial": null,
          "color": null,
          "icon_image": null,
          "telemetry": {
            "aqi_value": 1.15,
            "device_type": "nowplus",
            "eco2": 587.0,
            "internal_temperature": 26.0583,
            "pm1": 1.0,
            "pm10": 7.0,
            "pm25": 5.0,
            "pressure": 100488.0,
            "relative_humidity": 54.0,
            "tvoc": 58.0,
            "timestamp": "2026-09-10T09:21:25.023Z"
          },
          "controller_of_device": null
        },
        {
          "serial_number": "E768A8",
          "type": "sense",
          "name": "<label-6>",
          "status": "active",
          "room_slug": "5d2d8275-test-life-x-ogs_test-life-x-ogs",
          "domain_prefix": "5d2d8275",
          "created_at": "2026-08-20T10:39:24",
          "updated_at": "2026-08-25T09:28:32",
          "room_name": "<label-7>",
          "building_slug": "5d2d8275-test-life-x-ogs",
          "building_name": "<label-7>",
          "connection_status": "disconnected",
          "connection_status_updated_at": "2026-05-14T08:47:27",
          "latitude": 44.5,
          "longitude": 11.3,
          "notes": null,
          "firmware_version": null,
          "icon_color_variant_id": null,
          "config_status": "confirmed",
          "managed_by_device_serial": null,
          "controller_of_device_serial": null,
          "color": null,
          "icon_image": null,
          "telemetry": null,
          "controller_of_device": null
        }
      ],
      "pagination": {
        "page": 1,
        "page_size": 2,
        "total": 120,
        "total_pages": 60
      }
    }
  },
  {
    "caso": "error__unauthenticated",
    "status": 401,
    "chiavi_body": [
      "message"
    ],
    "body": {
      "message": "Unauthorized"
    }
  }
]
```

</details>

## V1 - L'ordinamento di GET /data/devices e' stabile fra pagine e fra chiamate ripetute? Esistono filtri o ordinamenti?

**Esito:** stabile

<details><summary>Evidenza</summary>

```json
{
  "pagina_1": [
    "3D90E0",
    "57FA28"
  ],
  "pagina_2": [],
  "pagina_1_ripetuta": [
    "3D90E0",
    "57FA28"
  ],
  "stabile_fra_chiamate_ripetute": true,
  "stabile_fra_paginazione_e_page_size_grande": true,
  "serial_duplicati_fra_pagina_1_e_2": [],
  "totale_con_page_size_grande": 2,
  "sonde_di_ordinamento_status": {
    "sort": 200,
    "order_by": 200,
    "order": 200
  }
}
```

</details>

## V2 - Enumerazione completa dei valori di `unit` (measures-ranges senza device_type).

**Esito:** 8 unita' distinte

<details><summary>Evidenza</summary>

```json
{
  "V - Ix": [
    "Volatile organic compounds"
  ],
  "ppm": [
    "Carbon dioxide",
    "Carbon monoxide",
    "Methane"
  ],
  "°C": [
    "Temperature"
  ],
  "%": [
    "Relative humidity"
  ],
  "Pa": [
    "Atmospheric pressure"
  ],
  "": [
    "Air Quality Index"
  ],
  "Bq/m³": [
    "Radon"
  ],
  "µg/m³": [
    "Particulate matter"
  ]
}
```

</details>

## V3 - pm10 ha davvero `excellent upperBound: 200` a runtime, o solo negli esempi?

**Esito:** vedi evidenza

<details><summary>Evidenza</summary>

```json
{
  "label": "Particulate matter",
  "unit": "µg/m³",
  "dataType": "float",
  "ranges": [
    {
      "status": "excellent",
      "upperBound": 20
    },
    {
      "status": "high",
      "upperBound": 30
    },
    {
      "status": "good",
      "upperBound": 40
    },
    {
      "status": "poor",
      "upperBound": 50
    },
    {
      "status": "terrible"
    }
  ],
  "acronym": "PM10",
  "pos": 9
}
```

</details>

## V4 - Il sismoff espone pm1/pm25/pm10? (T-02 dice no, lo swagger dice si')

**Esito:** non li espone

<details><summary>Evidenza</summary>

```json
{
  "presenza": {
    "pm1": false,
    "pm25": false,
    "pm10": false
  },
  "status_chiamata": 200
}
```

</details>

## V5 - Tipo reale e valori osservati di `radon_status`.

**Esito:** campo NON presente nei payload di questo account (nessun sense/city: richiesta operativa T-08 7)

<details><summary>Evidenza</summary>

```json
{
  "valori": {},
  "tipi_python": {}
}
```

</details>

## V6 - La response di /data/user/me/domains contiene `prefix` e un nome visualizzabile distinto dal prefisso?

**Esito:** prefix + nome presenti

<details><summary>Evidenza</summary>

```json
{
  "chiavi_per_dominio": [
    "created_at",
    "name",
    "parent_domain_prefix",
    "prefix",
    "type"
  ],
  "numero_domini": 1,
  "esempio": {
    "domain": {
      "prefix": "875fe89b",
      "name": "<label-1>",
      "type": "customer",
      "parent_domain_prefix": "fdffb4d9",
      "created_at": "2026-04-09T12:50:06"
    },
    "role": {
      "code": "user",
      "name": "User",
      "description": "Standard user access"
    },
    "assigned_at": "2026-04-09T12:51:36"
  },
  "chiavi_di_primo_livello": [
    "assigned_at",
    "domain",
    "role"
  ]
}
```

</details>

## V7 - Ordine di grandezza di `pressure`: Pa (~101300) come atteso?

**Esito:** ordine di grandezza 101300 (es. 100488.0)

<details><summary>Evidenza</summary>

```json
{
  "valori_osservati": [
    100488.0,
    100488.0,
    100488.0,
    100488.0,
    100488.0,
    100488.0,
    100488.0
  ]
}
```

</details>

## V8 - `internal_temperature` arriva in C (~21) o in centesimi (~2100)?

**Esito:** ordine di grandezza 21 (es. 26.0583)

<details><summary>Evidenza</summary>

```json
{
  "valori_osservati": [
    26.0583,
    26.0583,
    26.0583,
    26.0583,
    26.0583,
    26.0583,
    26.0583
  ]
}
```

</details>
