# UE Management API Specification

La suite di API per la gestione degli User Equipment (UE) consente di istanziare, interrogare, eliminare e controllare la generazione di traffico (`iPerf3`) per i nodi emulati nell'architettura **PhyTwin**.

> [!NOTE]
> **Gestione Automatica dell'ID (`phytwin_`)**
> Per tutti gli endpoint che accettano o restituiscono un `ue_id`, il sistema applica automaticamente il prefisso `phytwin_` se non è già presente (es. `ue1` viene convertito internamente in `phytwin_ue1`). È comunque possibile passare un identificativo che contenga già il prefisso.

---

## 📋 Panoramica Endpoints

| Metodo | Endpoint | Status Code | Descrizione |
| :--- | :--- | :---: | :--- |
| `POST` | `/ues` | `201` | Registra e crea un nuovo container UE mettendolo in rete |
| `GET` | `/ues` | `200` | Elenca tutti gli UE e le relative policy (Slice/PCC/AMBR) |
| `GET` | `/ues/{ue_id}` | `200` | Restituisce i dettagli tecnici di un singolo UE |
| `POST` | `/ues/{ue_id}/iperf/start` | `200` | Avvia un agent iPerf3 in background sul container dell'UE |
| `POST` | `/ues/{ue_id}/iperf/stop` | `200` | Invia un segnale `SIGTERM` per stoppare iPerf3 sul container dell'UE |
| `DELETE` | `/ues/{ue_id}` | `200` | Esegue il teardown di rete, stoppa ed elimina il container e i record sul DB |

---

## 🚀 Dettaglio Endpoints ed Esempi cURL

### 1. Creazione UE (`POST /ues`)

Crea la voce nel database per l'UE, esegue il calcolo delle rotte verso i gateway delle slice allocate e, se `spawn_container` è impostato su `true`, avvia il container Docker (`ue-image:latest`) con le capability di rete `NET_ADMIN`.

#### Request Body
```json
{
  "ue_id": "ue1",
  "spawn_container": true,
  "slice_ips": {
    "slice1": "10.45.0.3",
    "slice2": "10.55.0.3"
  }
}
```

#### Response (`201 Created`)
```json
{
  "status": "SUCCESS",
  "ue_id": "phytwin_ue1",
  "container_name": "phytwin_ue1",
  "container_id": "9b1deb4d3b7d4f89a123456789abcdef0123456789abcdef0123456789abcdef",
  "created_at": "2026-08-27T12:00:00Z"
}
```

#### Esempio cURL
```bash
curl -X POST "http://localhost:8000/ues" \
  -H "Content-Type: application/json" \
  -d '{
    "ue_id": "ue1",
    "spawn_container": true,
    "slice_ips": {
      "slice1": "10.45.0.3",
      "slice2": "10.55.0.3"
    }
  }'
```

---

### 2. Elenco Completo UE (`GET /ues`)

Recupera la lista di tutti gli UE censiti nel database, includendo per ciascuno le slice connesse, gli IP assegnati, le policy AMBR (*Aggregated Maximum Bit Rate*) e le regole PCC (*Policy and Charging Control*) attive.

#### Response (`200 OK`)
```json
[
  {
    "ue_id": "phytwin_ue1",
    "container_id": "9b1deb4d3b7d...",
    "created_at": "2026-08-27T12:00:00Z",
    "slices": [
      {
        "slice_id": "slice1",
        "dnn": "internet",
        "ue_ip": "10.45.0.3",
        "ambr": {
          "ul_br": "50Mbit",
          "dl_br": "100Mbit",
          "priority": 1
        },
        "pcc_rules": [
          {
            "rule_name": "video_stream",
            "direction": "dl",
            "remote_ip": "10.45.0.1",
            "gbr_ul": "10Mbit",
            "gbr_dl": "20Mbit",
            "mbr_ul": "15Mbit",
            "mbr_dl": "30Mbit",
            "priority": 10
          }
        ]
      }
    ]
  }
]
```

#### Esempio cURL
```bash
curl -X GET "http://localhost:8000/ues"
```

---

### 3. Dettaglio Singolo UE (`GET /ues/{ue_id}`)

Restituisce le informazioni dettagliate di un singolo UE dato il suo identificativo.

#### Parametri di Path
* `ue_id` (string, obbligatorio): L'ID dell'UE (es. `ue1` o `phytwin_ue1`).

#### Response (`200 OK`)
```json
{
  "ue_id": "phytwin_ue1",
  "container_id": "9b1deb4d3b7d...",
  "created_at": "2026-08-27T12:00:00Z",
  "slices": [
    {
      "slice_id": "slice1",
      "dnn": "internet",
      "ue_ip": "10.45.0.3",
      "ambr": {
        "ul_br": "50Mbit",
        "dl_br": "100Mbit",
        "priority": 1
      },
      "pcc_rules": []
    }
  ]
}
```

#### Esempio cURL
```bash
curl -X GET "http://localhost:8000/ues/ue1"
```

---

### 4. Avvio Test iPerf3 (`POST /ues/{ue_id}/iperf/start`)

Esegue in background l'agente Python (`ue_agent.py`) all'interno del container UE per generare traffico verso un server di destinazione target.

#### Request Body
```json
{
  "target_ip": "10.45.0.1",
  "target_port": 5201,
  "protocol": "udp",
  "bitrate": "20M",
  "direction": "ul"
}
```

#### Schema dei Parametri

| Campo | Tipo | Valori Validi / Descrizione |
| :--- | :--- | :--- |
| `target_ip` | `string` | IP del server di destinazione (es. DNN Server) |
| `target_port` | `integer` | Porta iPerf3 di ascolto remote (default `5201`) |
| `protocol` | `string` | `udp` oppure `tcp` |
| `bitrate` | `string` | Bandwidth target espressa con unità (es. `10M`, `500k`) |
| `direction` | `string` | Direzione del flusso: `ul` (Uplink) oppure `dl` (Downlink) |

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "message": "Test iperf3 avviato su UE 'phytwin_ue1'",
  "pid": "142"
}
```

#### Esempio cURL
```bash
curl -X POST "http://localhost:8000/ues/ue1/iperf/start" \
  -H "Content-Type: application/json" \
  -d '{
    "target_ip": "10.45.0.1",
    "target_port": 5201,
    "protocol": "udp",
    "bitrate": "20M",
    "direction": "ul"
  }'
```

---

### 5. Arresto Test iPerf3 (`POST /ues/{ue_id}/iperf/stop`)

Termina i processi `ue_agent.py` e `iperf3` attivi sul container UE inviando un segnale `SIGTERM` (15) per permettere la chiusura pulita dei socket di rete.

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "message": "Test iperf3 fermato su UE 'phytwin_ue1'."
}
```

#### Esempio cURL
```bash
curl -X POST "http://localhost:8000/ues/ue1/iperf/stop"
```

---

### 6. Eliminazione UE (`DELETE /ues/{ue_id}`)

Esegue il teardown completo delle risorse dell'UE:
1. Elimina le regole `tc` (Traffic Control) attive a livello di kernel sul Middlebox.
2. Rimuove le configurazioni e le regole PCC associate dal database.
3. Arresta e rimuove il container Docker dell'UE.

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "message": "UE 'phytwin_ue1' e tutte le sue risorse sono state rimosse."
}
```

#### Esempio cURL
```bash
curl -X DELETE "http://localhost:8000/ues/ue1"
```