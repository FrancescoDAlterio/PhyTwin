# Telemetry Manager Pub/Sub API Specification

Il **Telemetry Manager** espone uno stream continuo di metriche in tempo reale utilizzando il pattern **Publish/Subscribe (PUB/SUB)** basato sul protocollo **ZeroMQ (ZMQ)**. 

Le metriche vengono estratte dal database centrale SQLite (`/data/metrics.db`), pubblicate sotto forma di stringhe formattate con prefisso di argomento (topic) e payload JSON, e successivamente contrassegnate come pubblicate.

> [!NOTE]
> **Dettagli di Connessione**
> * **Protocollo:** ZeroMQ PUB
> * **Porta predefinita:** `5555` (configurabile tramite variabile d'ambiente `ZMQ_PORT`)
> * **Formato Messaggio:** `<TOPIC_NAME> <JSON_PAYLOAD>`
> * **Frequenza Polling DB:** ~500ms (fetch a blocchi di massimo 50 record per topic)

---

## 📋 Panoramica Topic

| Topic | Origine Dati | Descrizione |
| :--- | :--- | :--- |
| `metrics.server.iperf` | `server_iperf_stats` | Metriche di performance iPerf misurate lato Server |
| `metrics.ue.iperf` | `ue_iperf_stats` | Metriche di performance iPerf misurate lato Agent UE |
| `metrics.middlebox.global` | `mb_global_stats` | Statistiche globali aggregate del Middlebox (traffico non autorizzato, UE/regole attive) |
| `metrics.middlebox.ue` | `mb_ue_stats` | Statistiche di traffico per singolo UE/Slice gestito dal Middlebox |
| `metrics.middlebox.flow` | `mb_flow_stats` | Statistiche di traffico granularizzato per singolo flusso/regola PCC |

---

## 💻 Esempio di Client Subscriber (Python)

È possibile sottoscriversi a tutti i topic inviando una stringa vuota `""` al socket, oppure filtrare per specifici topic d'interesse.

```python
import zmq
import json

context = zmq.Context()
socket = context.socket(zmq.SUB)
socket.connect("tcp://<TELEMETRY_MANAGER_IP>:5555")

# Sottoscrizione a tutti i topic (oppure es. socket.setsockopt_string(zmq.SUBSCRIBE, "metrics.middlebox"))
socket.setsockopt_string(zmq.SUBSCRIBE, "")

print("In ascolto sul canale Pub/Sub delle metriche...")

while True:
    message = socket.recv_string()
    topic, payload_str = message.split(" ", 1)
    payload = json.loads(payload_str)
    
    print(f"[{topic}] Received timestamp: {payload.get('timestamp')}")
    print(json.dumps(payload, indent=2))
```

---

## 📄 Schemi dei Payload per Topic

### 1. `metrics.server.iperf`
Raccoglie le metriche delle sessioni iPerf registrate dai server applicativi/estemi.

#### Payload Schema Example
```json
{
  "topic": "metrics.server.iperf",
  "timestamp": "2026-08-27T13:00:00Z",
  "server": {
    "ip": "10.45.0.1",
    "port": 5001
  },
  "client": {
    "ip": "10.45.0.3",
    "port": 45122
  },
  "session": {
    "connection_id": 12,
    "protocol": "UDP",
    "direction": "UPLINK",
    "interval_seconds": 1.0
  },
  "performance": {
    "bytes_transferred": 1250000,
    "bandwidth_mbps": 10.0,
    "jitter_ms": 1.25,
    "lost_packets": 2,
    "total_packets": 1000,
    "packet_loss_pct": 0.2
  }
}
```

---

### 2. `metrics.ue.iperf`
Raccoglie le metriche misurate dagli agent iPerf in esecuzione direttamente sugli UE emulati.

#### Payload Schema Example
```json
{
  "topic": "metrics.ue.iperf",
  "timestamp": "2026-08-27T13:00:00Z",
  "ue_id": "phytwin_ue1",
  "server": {
    "ip": "10.45.0.1",
    "port": 5001
  },
  "client": {
    "ip": "10.45.0.3",
    "port": 45122
  },
  "session": {
    "connection_id": 12,
    "protocol": "TCP",
    "direction": "DOWNLINK",
    "interval_seconds": 1.0
  },
  "performance": {
    "bytes_transferred": 6250000,
    "bandwidth_mbps": 50.0,
    "jitter_ms": 0.0,
    "lost_packets": 0,
    "total_packets": 4200,
    "packet_loss_pct": 0.0
  }
}
```

---

### 3. `metrics.middlebox.global`
Fornisce una vista ad alto livello sullo stato del Middlebox, inclusi contatori di pacchetti/byte scartati perché non autorizzati e numero di risorse attualmente attive.

#### Payload Schema Example
```json
{
  "topic": "metrics.middlebox.global",
  "timestamp": "2026-08-27T13:00:00Z",
  "metrics": {
    "aggregated_tot": {
      "unauthorized_packets_dropped_tot": 1450,
      "unauthorized_bytes_dropped_tot": 185600
    },
    "per_second": {
      "unauthorized_packets_dropped_per_second": 12,
      "unauthorized_bytes_dropped_per_second": 1536
    }
  },
  "summary": {
    "active_ues": 3,
    "active_pcc_rules": 5
  }
}
```

---

### 4. `metrics.middlebox.ue`
Monitora il volume di traffico, i drop, gli eventi di overlimit di banda e il throughput istantaneo per ogni UE associato ad una slice.

#### Payload Schema Example
```json
{
  "topic": "metrics.middlebox.ue",
  "timestamp": "2026-08-27T13:00:00Z",
  "ue_id": "phytwin_ue1",
  "details": {
    "ue_ip": "10.45.0.3",
    "slice_id": "slice1",
    "dnn": "internet"
  },
  "metrics": {
    "aggregated_tot": {
      "bytes_sent_tot_ul": 52428800,
      "bytes_sent_tot_dl": 104857600,
      "packets_sent_tot_ul": 35000,
      "packets_sent_tot_dl": 70000,
      "packets_dropped_tot_ul": 120,
      "packets_dropped_tot_dl": 0,
      "overlimits_tot_ul": 450,
      "overlimits_tot_dl": 12
    },
    "per_second": {
      "bytes_sent_per_second_ul": 1250000,
      "bytes_sent_per_second_dl": 6250000,
      "packets_sent_per_second_ul": 830,
      "packets_sent_per_second_dl": 4160,
      "packets_dropped_per_second_ul": 5,
      "packets_dropped_per_second_dl": 0,
      "overlimits_per_second_ul": 18,
      "overlimits_per_second_dl": 0,
      "current_throughput_mbps_ul_per_second": 10.0,
      "current_throughput_mbps_dl_per_second": 50.0
    }
  },
  "status": {
    "ul_status": "OK",
    "dl_status": "OK"
  }
}
```

---

### 5. `metrics.middlebox.flow`
Dettaglio specifico per singola regola PCC. Traccia il comportamento del flusso rispetto ai limiti di banda GBR/MBR imposti e segnala eventuali interventi di throttling o drop da parte dei filtri HTB.

#### Payload Schema Example
```json
{
  "topic": "metrics.middlebox.flow",
  "timestamp": "2026-08-27T13:00:00Z",
  "flow_id": 101,
  "details": {
    "ue_ip": "10.45.0.3",
    "rule_name": "video_stream",
    "direction": "both",
    "remote_ip": "10.45.0.100/32",
    "qos_profile": {
      "gbr": "20Mbit",
      "mbr": "30Mbit",
      "priority": 2
    }
  },
  "metrics": {
    "aggregated_tot": {
      "bytes_sent_tot": 26214400,
      "packets_sent_tot": 17500,
      "packets_dropped_tot": 45,
      "overlimits_tot": 120
    },
    "per_second": {
      "bytes_sent_per_second": 2500000,
      "packets_sent_per_second": 1660,
      "packets_dropped_per_second": 2,
      "overlimits_per_second": 10,
      "current_throughput_mbps_per_second": 20.0
    }
  },
  "status": {
    "operation": "ACTIVE",
    "is_throttled": false,
    "is_dropping": false
  }
}
```