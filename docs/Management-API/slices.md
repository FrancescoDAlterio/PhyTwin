# Slice Management API Specification

La suite di API per la gestione delle Slice consente di associare, aggiornare, consultare e rimuovere configurazioni di rete per uno specifico **User Equipment (UE)** nell'architettura **PhyTwin**. 

La creazione di una slice applica direttamente a livello di kernel Linux le regole di Traffic Control (`tc`) basate su classi HTB (*Hierarchical Token Bucket*) per il rispetto delle policy AMBR (*Aggregate Maximum Bit Rate*), oltre a configurare le catene di inoltro `iptables` (`MB_FORWARD`).

---

## 📋 Panoramica Endpoints

| Metodo | Endpoint | Status Code | Descrizione |
| :--- | :--- | :---: | :--- |
| `POST` | `/ues/{ue_id}/slices` | `201` | Associa e configura una nuova slice ad un UE esistente |
| `GET` | `/ues/{ue_id}/slices` | `200` | Elenca tutte le slice allocate per uno specifico UE |
| `PUT` | `/ues/{ue_id}/slices/{slice_id}` | `200` | Aggiorna i parametri AMBR/IP di una slice e ri-applica le regole PCC collegate |
| `DELETE` | `/ues/{ue_id}/slices/{slice_id}` | `200` | Esegue il teardown di rete (TC/iptables) e rimuove la slice e le sue regole PCC dal DB |

---

## ⚙️ Validazioni e Comportamento di Rete

> [!IMPORTANT]
> - **Appartenenza Subnet**: L'indirizzo `ue_ip` fornito deve necessariamente appartenere al blocco CIDR della slice specificata (es. `10.45.0.0/24`).
> - **Compatibilità DNN**: La `dnn` passata nella richiesta deve corrispondere alla Data Network Name legata alla slice definita nella mappa di sistema.
> - **Traffic Control (TC)**: In fase di inserimento o aggiornamento, il Middlebox istanzia le classi HTB parent per la gestione della banda limite in Uplink e Downlink, impostando i filtri `u32` per l'indirizzamento del traffico.
> - **Persistenza PCC (PUT)**: L'operazione di `PUT` su una slice esegue prima il teardown dell'interfaccia, aggiorna la classe HTB genitore dell'AMBR e infine **ricrea automaticamente tutte le regole PCC precedentemente collegate**, garantendo la continuità operativa.

---

## 🚀 Dettaglio Endpoints ed Esempi cURL

### 1. Creazione Configurazione Slice (`POST /ues/{ue_id}/slices`)

Crea la configurazione di slice per un UE, valida gli IP e le DNN, crea le classi HTB (`tc class add`) e i filtri di rete su entrambe le interfacce (interfaccia di slice e interfaccia DNN target) ed inserisce la regola in `iptables`.

#### Parametri di Path
* `ue_id` (`string`, obbligatorio): ID dell'UE a cui agganciare la slice.

#### Request Body
```json
{
  "slice_id": "slice1",
  "dnn": "internet",
  "ue_ip": "10.45.0.3",
  "ambr": {
    "ul_br": "50Mbit",
    "dl_br": "100Mbit",
    "priority": 1
  }
}
```

#### Schema dei Parametri

| Campo | Tipo | Descrizione |
| :--- | :--- | :--- |
| `slice_id` | `string` | Identificativo della slice (deve coincidere con una chiave valida in `SLICES_MAP`) |
| `dnn` | `string` | Nome della Data Network di destinazione legata alla slice |
| `ue_ip` | `string` | Indirizzo IP statico assegnato all'UE all'interno della subnet della slice |
| `ambr.ul_br` | `string` | Aggregate Maximum Bit Rate per l'Uplink (es. `50Mbit`, `1000kbit`) |
| `ambr.dl_br` | `string` | Aggregate Maximum Bit Rate per il Downlink (es. `100Mbit`) |
| `ambr.priority` | `integer` | Priorità della classe HTB (valore compreso tra `0` e `7`, default `7`) |

#### Response (`201 Created`)
```json
{
  "status": "SUCCESS",
  "ue_id": "phytwin_ue1",
  "slice_id": "slice1",
  "ue_ip": "10.45.0.3",
  "ambr_htb_prio": 1
}
```

#### Esempio cURL
```bash
curl -X POST "http://localhost:8000/ues/ue1/slices" \
  -H "Content-Type: application/json" \
  -d '{
    "slice_id": "slice1",
    "dnn": "internet",
    "ue_ip": "10.45.0.3",
    "ambr": {
      "ul_br": "50Mbit",
      "dl_br": "100Mbit",
      "priority": 1
    }
  }'
```

---

### 2. Elenco Slice dell'UE (`GET /ues/{ue_id}/slices`)

Restituisce tutte le configurazioni di slice attive e registrate sul database per l'UE specificato.

#### Parametri di Path
* `ue_id` (`string`, obbligatorio): ID dell'UE.

#### Response (`200 OK`)
```json
[
  {
    "slice_id": "slice1",
    "dnn": "internet",
    "ue_ip": "10.45.0.3",
    "ambr": {
      "ul_br": "50Mbit",
      "dl_br": "100Mbit",
      "priority": 1
    }
  },
  {
    "slice_id": "slice2",
    "dnn": "ims",
    "ue_ip": "10.55.0.3",
    "ambr": {
      "ul_br": "10Mbit",
      "dl_br": "20Mbit",
      "priority": 2
    }
  }
]
```

#### Esempio cURL
```bash
curl -X GET "http://localhost:8000/ues/ue1/slices"
```

---

### 3. Aggiornamento Configurazione Slice (`PUT /ues/{ue_id}/slices/{slice_id}`)

Aggiorna i parametri di banda AMBR o di rete per una slice già associata. Il Middlebox rimuove temporaneamente la struttura di rete esistente, ricalcola i parametri HTB, reinstalla i filtri `tc` e ripristina automaticamente eventuali sotto-classi/regole PCC preesistenti.

#### Parametri di Path
* `ue_id` (`string`, obbligatorio): ID dell'UE.
* `slice_id` (`string`, obbligatorio): ID della slice da modificare.

#### Request Body
```json
{
  "slice_id": "slice1",
  "dnn": "internet",
  "ue_ip": "10.45.0.3",
  "ambr": {
    "ul_br": "80Mbit",
    "dl_br": "150Mbit",
    "priority": 2
  }
}
```

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "message": "Slice 'slice1' aggiornata con successo per UE 'phytwin_ue1'."
}
```

#### Esempio cURL
```bash
curl -X PUT "http://localhost:8000/ues/ue1/slices/slice1" \
  -H "Content-Type: application/json" \
  -d '{
    "slice_id": "slice1",
    "dnn": "internet",
    "ue_ip": "10.45.0.3",
    "ambr": {
      "ul_br": "80Mbit",
      "dl_br": "150Mbit",
      "priority": 2
    }
  }'
```

---

### 4. Eliminazione Slice (`DELETE /ues/{ue_id}/slices/{slice_id}`)

Rimuove completamente la slice dall'UE. Questa operazione esegue il teardown di tutte le classi `tc` e regole `iptables` collegate, cancellando contemporaneamente dal database la configurazione di slice e tutte le regole PCC ad essa associate.

#### Parametri di Path
* `ue_id` (`string`, obbligatorio): ID dell'UE.
* `slice_id` (`string`, obbligatorio): ID della slice da rimuovere.

#### Response (`200 OK`)
```json
{
  "status": "SUCCESS",
  "message": "Slice 'slice1' e relative regole PCC rimosse per UE 'phytwin_ue1'."
}
```

#### Esempio cURL
```bash
curl -X DELETE "http://localhost:8000/ues/ue1/slices/slice1"
```