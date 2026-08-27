# PCC Rules Management API Specification

La suite di API per la gestione delle regole **PCC (Policy and Charging Control)** consente di definire le politiche di Quality of Service (QoS) a livello di singolo flusso per un determinato UE all'interno di una specifica slice. 

Tali regole consentono di allocare banda garantita (**GBR** - *Guaranteed Bit Rate*) e banda massima consentita (**MBR** - *Maximum Bit Rate*) differenziata per direzione (uplink, downlink o entrambe), creando sottoclassi HTB dedicate e filtri `tc u32` per la classificazione del traffico in base all'IP remoto.

> [!NOTE]
> **Proprietà e Vincoli delle Regole PCC**
> 1. La slice specificata (`slice_id`) deve essere già configurata ed associata all'UE indicato.
> 2. Il nome della regola (`rule_name`) deve essere univoco all'interno della medesima slice dell'UE.
> 3. I flussi possono essere filtrati per direzione (`uplink`, `downlink`, `both`) basandosi sull'indirizzo IP remoto definito nel blocco `flow_description`.

---

## 📋 Panoramica Endpoints

| Metodo | Endpoint | Status Code | Descrizione |
| :--- | :--- | :---: | :--- |
| `POST` | `/ues/{ue_id}/slices/{slice_id}/pcc_rules` | `201` | Crea una nuova regola PCC agganciando una sottoclasse HTB alla slice |
| `GET` | `/ues/{ue_id}/slices/{slice_id}/pcc_rules` | `200` | Elenca tutte le regole PCC associate alla specifica slice dell'UE |
| `PUT` | `/ues/{ue_id}/slices/{slice_id}/pcc_rules/{rule_name}` | `200` | Aggiorna i parametri QoS/Flusso di una regola PCC esistente (supporta rinominazione) |
| `DELETE` | `/ues/{ue_id}/slices/{slice_id}/pcc_rules/{rule_name}` | `200` | Rimuove la regola PCC, distruggendo la sottoclasse HTB e i relativi filtri di rete |

---

## 🚀 Dettaglio Endpoints ed Esempi cURL

### 1. Creazione Regola PCC (`POST /ues/{ue_id}/slices/{slice_id}/pcc_rules`)

Crea ed applica una nuova regola PCC. Viene generato un nuovo `class_id` figlio rispetto alla classe HTB dell'UE, con filtri `tc` per intercettare i pacchetti diretti a o provenienti dall'IP remoto specificato.

#### Parametri di Path
* `ue_id` (string, obbligatorio): Identificativo dell'UE (es. `ue1` o `phytwin_ue1`).
* `slice_id` (string, obbligatorio): Identificativo della slice attiva (es. `slice1`).

#### Request Body
```json
{
  "rule_name": "video_stream",
  "direction": "both",
  "flow_description": {
    "remote_ip": "10.45.0.100/32",
    "protocol": "udp"
  },
  "qos": {
    "gbr_ul": "10Mbit",
    "gbr_dl": "20Mbit",
    "mbr_ul": "15Mbit",
    "mbr_dl": "30Mbit",
    "priority": 2,
    "arp": 1
  }
}
```

#### Schema del Payload

| Campo | Tipo | Descrizione |
| :--- | :--- | :--- |
| `rule_name` | `string` | Identificativo unico della regola per la slice corrente |
| `direction` | `string` | Ambito di applicazione: `uplink`, `downlink`, oppure `both` |
| `flow_description.remote_ip` | `string` | Indirizzo IP di destinazione/sorgente remota (CIDR) |
| `flow_description.protocol` | `string` | Protocollo di trasporto (es. `udp`, `tcp`, `ip`) |
| `qos.gbr_ul` / `qos.gbr_dl` | `string` | Banda minima garantita (Guaranteed Bit Rate) per Uplink e Downlink |
| `qos.mbr_ul` / `qos.mbr_dl` | `string` | Banda massima limite (Maximum Bit Rate) per Uplink e Downlink |
| `qos.priority` | `integer` | Livello di priorità per lo scheduler HTB (range 0-7) |
| `qos.arp` | `integer` | Allocation and Retention Priority (usata come fallback se `priority` è omesso) |

#### Response (`201 Created`)
```json
{
  "status": "SUCCESS",
  "ue_id": "phytwin_ue1",
  "slice_id": "slice1",
  "rule_name": "video_stream",
  "class_id": 101
}
```

#### Esempio cURL
```bash
curl -X POST "http://localhost:8000/ues/ue1/slices/slice1/pcc_rules" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_name": "video_stream",
    "direction": "both",
    "flow_description": {
      "remote_ip": "10.45.0.100/32",
      "protocol": "udp"
    },
    "qos": {
      "gbr_ul": "10Mbit",
      "gbr_dl": "20Mbit",
      "mbr_ul": "15Mbit",
      "mbr_dl": "30Mbit",
      "priority": 2,
      "arp": 1
    }
  }'
```

---

### 2. Elenco Regole PCC (`GET /ues/{ue_id}/slices/{slice_id}/pcc_rules`)

Recupera tutte le regole PCC associate alla slice dell'UE specificato.

#### Parametri di Path
* `ue_id` (string, obbligatorio): Identificativo dell'UE.
* `slice_id` (string, obbligatorio): Identificativo della slice.

#### Response (`200 OK`)
```json
[
  {
    "rule_name": "video_stream",
    "direction": "both",
    "flow_description": {
      "remote_ip": "10.45.0.100/32",
      "protocol": "udp"
    },
    "qos": {
      "gbr_ul": "10Mbit",
      "gbr_dl": "20Mbit",
      "mbr_ul": "15Mbit",
      "mbr_dl": "30Mbit",
      "priority": 2
    }
  }
]
```

#### Esempio cURL
```bash
curl -X GET "http://localhost:8000/ues/ue1/slices/slice1/pcc_rules"
```

---

### 3. Modifica Regola PCC (`PUT /ues/{ue_id}/slices/{slice_id}/pcc_rules/{rule_name}`)

Aggiorna i parametri di QoS o la descrizione del flusso per una regola esistente. Esegue la pulizia preventiva delle vecchie regole di rete a livello kernel (`teardown`) e ricrea le classi e i filtri TC aggiornati mantenendo il `class_id` assegnato.

> [!TIP]
> **Rinominazione Regole**: Se il campo `rule_name` inviato nel payload è diverso dall'URL, il sistema verificherà che il nuovo nome non sia già occupato da un'altra regola ed aggiornerà il nome nel DB.

#### Parametri di Path
* `ue_id` (string, obbligatorio): Identificativo dell'UE.
* `slice_id` (string, obbligatorio): Identificativo della slice.
* `rule_name` (string, obbligatorio): Nome corrente della regola da modificare.

#### Request Body
```json
{
  "rule_name": "video_stream_hd",
  "direction": "both",
  "flow_description": {
    "remote_ip": "10.45.0.100/32",
    "protocol": "udp"
  },
  "qos": {
    "gbr_ul": "15Mbit",
    "gbr_dl": "40Mbit",
    "mbr_ul": "20Mbit",
    "mbr_dl": "50Mbit",
    "priority": 1,
    "arp": 1
  }
}
```

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "ue_id": "phytwin_ue1",
  "slice_id": "slice1",
  "rule_name": "video_stream_hd",
  "class_id": 101
}
```

#### Esempio cURL
```bash
curl -X PUT "http://localhost:8000/ues/ue1/slices/slice1/pcc_rules/video_stream" \
  -H "Content-Type: application/json" \
  -d '{
    "rule_name": "video_stream_hd",
    "direction": "both",
    "flow_description": {
      "remote_ip": "10.45.0.100/32",
      "protocol": "udp"
    },
    "qos": {
      "gbr_ul": "15Mbit",
      "gbr_dl": "40Mbit",
      "mbr_ul": "20Mbit",
      "mbr_dl": "50Mbit",
      "priority": 1,
      "arp": 1
    }
  }'
```

---

### 4. Eliminazione Regola PCC (`DELETE /ues/{ue_id}/slices/{slice_id}/pcc_rules/{rule_name}`)

Rimuove la regola PCC specificata. L'operazione elimina i filtri `tc` associati all'IP remoto, distrugge la sottoclasse HTB creata per la regola ed elimina il record dal database.

#### Parametri di Path
* `ue_id` (string, obbligatorio): Identificativo dell'UE.
* `slice_id` (string, obbligatorio): Identificativo della slice.
* `rule_name` (string, obbligatorio): Nome della regola da rimuovere.

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "message": "PCC Rule 'video_stream_hd' rimossa per l'UE 'phytwin_ue1'."
}
```

#### Esempio cURL
```bash
curl -X DELETE "http://localhost:8000/ues/ue1/slices/slice1/pcc_rules/video_stream_hd"
```