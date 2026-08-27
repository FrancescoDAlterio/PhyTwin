import ipaddress
import subprocess
from fastapi import APIRouter, HTTPException, status
from core.config import SLICES_MAP, DNNS_MAP
from schemas.slice import SliceConfigReq
from services.db_service import get_config_db
from services.tc_service import (
    format_rate, clamp_prio, teardown_slice_config_network, get_next_class_id
)

router = APIRouter(prefix="/ues/{ue_id}/slices", tags=["Slices"])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_slice_config(ue_id: str, req: SliceConfigReq):
    conn = get_config_db()
    c = conn.cursor()

    c.execute("SELECT ue_id FROM ues WHERE ue_id = ?", (ue_id,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail=f"UE '{ue_id}' non registrato. Creare prima l'UE.")

    if req.slice_id not in SLICES_MAP:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Slice ID '{req.slice_id}' non valida.")

    matched_slice = SLICES_MAP[req.slice_id]
    if ipaddress.ip_address(req.ue_ip) not in matched_slice["network_obj"]:
        conn.close()
        raise HTTPException(status_code=400, detail=f"UE IP {req.ue_ip} non appartiene alla subnet di {req.slice_id}.")

    # matched_dnn = next((d for k, d in DNNS_MAP.items() if req.dnn in (k, d["network_name"])), None)
    matched_dnn = validate_dnn_for_slice(req.slice_id, req.dnn, matched_slice)
    if not matched_dnn:
        conn.close()
        raise HTTPException(status_code=400, detail=f"DNN '{req.dnn}' non valida.")

    c.execute("SELECT id FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, req.slice_id))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"Slice '{req.slice_id}' già configurata per UE '{ue_id}'. Usare PUT per aggiornare.")

    ue_class_id = get_next_class_id()

    if_slice = matched_slice["interface"]
    if_dnn = matched_dnn["interface"]
    dnn_subnet = matched_dnn["subnet"]

    ambr_ul = format_rate(req.ambr.ul_br)
    ambr_dl = format_rate(req.ambr.dl_br)
    ambr_prio = clamp_prio(req.ambr.priority if req.ambr.priority is not None else 7)

    subprocess.run(f"tc class add dev {if_slice} parent 1: classid 1:{ue_class_id} htb rate {ambr_dl} ceil {ambr_dl} prio {ambr_prio}", shell=True, check=True)
    subprocess.run(f"tc class add dev {if_dnn} parent 1: classid 1:{ue_class_id} htb rate {ambr_ul} ceil {ambr_ul} prio {ambr_prio}", shell=True, check=True)

    subprocess.run(f"tc filter add dev {if_slice} protocol ip parent 1:0 prio 10 u32 match ip dst {req.ue_ip}/32 match ip src {dnn_subnet} flowid 1:{ue_class_id}", shell=True, check=True)
    subprocess.run(f"tc filter add dev {if_dnn} protocol ip parent 1:0 prio 10 u32 match ip src {req.ue_ip}/32 match ip dst {dnn_subnet} flowid 1:{ue_class_id}", shell=True, check=True)

    subprocess.run(f"iptables -I MB_FORWARD 2 -i {if_slice} -o {if_dnn} -s {req.ue_ip}/32 -d {dnn_subnet} -m conntrack --ctstate NEW -j ACCEPT", shell=True, check=True)

    c.execute("""
        INSERT INTO ue_slice_configs (ue_id, slice_id, dnn, ue_ip, ambr_ul, ambr_dl, ambr_prio, ue_class_id, if_slice, if_dnn)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ue_id, req.slice_id, req.dnn, req.ue_ip, req.ambr.ul_br, req.ambr.dl_br, ambr_prio, ue_class_id, if_slice, if_dnn))

    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "ue_id": ue_id, "slice_id": req.slice_id, "ue_ip": req.ue_ip, "ambr_htb_prio": ambr_prio}


@router.get("")
def get_ue_slices(ue_id: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id, slice_id, dnn, ue_ip, ambr_ul, ambr_dl, ambr_prio FROM ue_slice_configs WHERE ue_id = ?", (ue_id,))
    slices = c.fetchall()
    conn.close()

    return [{
        "slice_id": s[1], "dnn": s[2], "ue_ip": s[3],
        "ambr": {"ul_br": s[4], "dl_br": s[5], "priority": s[6]}
    } for s in slices]


@router.put("/{slice_id}")
def update_slice_config(ue_id: str, slice_id: str, req: SliceConfigReq):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id, ue_class_id FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, slice_id))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' non trovata per UE '{ue_id}'.")

    sc_id, old_ue_class_id = row

    c.execute("SELECT rule_name, direction, remote_ip, protocol, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority FROM pcc_rules WHERE slice_config_id = ?", (sc_id,))
    saved_rules = c.fetchall()

    teardown_slice_config_network(conn, sc_id)

    matched_slice = SLICES_MAP[req.slice_id]
    #matched_dnn = next((d for k, d in DNNS_MAP.items() if req.dnn in (k, d["network_name"])), None)
    matched_dnn = validate_dnn_for_slice(req.slice_id, req.dnn, matched_slice)

    if_slice = matched_slice["interface"]
    if_dnn = matched_dnn["interface"]
    dnn_subnet = matched_dnn["subnet"]

    ambr_ul = format_rate(req.ambr.ul_br)
    ambr_dl = format_rate(req.ambr.dl_br)
    ambr_prio = clamp_prio(req.ambr.priority if req.ambr.priority is not None else 7)

    # Uso di 'replace' per sovrascrivere o ricreare la classe HTB senza errori
    subprocess.run(f"tc class replace dev {if_slice} parent 1: classid 1:{old_ue_class_id} htb rate {ambr_dl} ceil {ambr_dl} prio {ambr_prio}", shell=True, check=True)
    subprocess.run(f"tc class replace dev {if_dnn} parent 1: classid 1:{old_ue_class_id} htb rate {ambr_ul} ceil {ambr_ul} prio {ambr_prio}", shell=True, check=True)

    subprocess.run(f"tc filter add dev {if_slice} protocol ip parent 1:0 prio 10 u32 match ip dst {req.ue_ip}/32 match ip src {dnn_subnet} flowid 1:{old_ue_class_id}", shell=True, check=True)
    subprocess.run(f"tc filter add dev {if_dnn} protocol ip parent 1:0 prio 10 u32 match ip src {req.ue_ip}/32 match ip dst {dnn_subnet} flowid 1:{old_ue_class_id}", shell=True, check=True)
    subprocess.run(f"iptables -I MB_FORWARD 2 -i {if_slice} -o {if_dnn} -s {req.ue_ip}/32 -d {dnn_subnet} -m conntrack --ctstate NEW -j ACCEPT", shell=True, check=True)

    c.execute("""
        UPDATE ue_slice_configs
        SET dnn = ?, ue_ip = ?, ambr_ul = ?, ambr_dl = ?, ambr_prio = ?, if_slice = ?, if_dnn = ?
        WHERE id = ?
    """, (req.dnn, req.ue_ip, req.ambr.ul_br, req.ambr.dl_br, ambr_prio, if_slice, if_dnn, sc_id))

    subclass_idx = 1
    c.execute("DELETE FROM pcc_rules WHERE slice_config_id = ?", (sc_id,))
    for r_name, r_dir, rem_ip, r_proto, g_ul, g_dl, m_ul, m_dl, r_prio in saved_rules:
        pcc_class_id = old_ue_class_id + subclass_idx
        subclass_idx += 1
        rem_clean = rem_ip.split("/")[0]

        if r_dir in ["downlink", "both"]:
            subprocess.run(f"tc class add dev {if_slice} parent 1:{old_ue_class_id} classid 1:{pcc_class_id} htb rate {format_rate(g_dl)} ceil {format_rate(m_dl)} prio {r_prio}", shell=True, check=True)
            subprocess.run(f"tc filter add dev {if_slice} protocol ip parent 1:0 prio 1 u32 match ip dst {req.ue_ip}/32 match ip src {rem_clean}/32 flowid 1:{pcc_class_id}", shell=True, check=True)
        if r_dir in ["uplink", "both"]:
            subprocess.run(f"tc class add dev {if_dnn} parent 1:{old_ue_class_id} classid 1:{pcc_class_id} htb rate {format_rate(g_ul)} ceil {format_rate(m_ul)} prio {r_prio}", shell=True, check=True)
            subprocess.run(f"tc filter add dev {if_dnn} protocol ip parent 1:0 prio 1 u32 match ip src {req.ue_ip}/32 match ip dst {rem_clean}/32 flowid 1:{pcc_class_id}", shell=True, check=True)

        c.execute("""
            INSERT INTO pcc_rules (slice_config_id, rule_name, direction, remote_ip, protocol, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority, class_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sc_id, r_name, r_dir, rem_ip, r_proto, g_ul, g_dl, m_ul, m_dl, r_prio, pcc_class_id))

    c.execute("UPDATE ue_slice_configs SET subclass_counter = ? WHERE id = ?", (subclass_idx, sc_id))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "message": f"Slice '{slice_id}' aggiornata con successo per UE '{ue_id}'."}


@router.delete("/{slice_id}")
def delete_slice_config(ue_id: str, slice_id: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, slice_id))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' non trovata per UE '{ue_id}'.")

    sc_id = row[0]
    teardown_slice_config_network(conn, sc_id)

    c.execute("DELETE FROM ue_slice_configs WHERE id = ?", (sc_id,))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "message": f"Slice '{slice_id}' e relative regole PCC rimosse per UE '{ue_id}'."}


#### HELPERS ####
# TODO Da spostare

def validate_dnn_for_slice(slice_id: str, req_dnn: str, matched_slice: dict):
    if not req_dnn or not req_dnn.strip():
        raise HTTPException(status_code=400, detail="Il campo 'dnn' è obbligatorio e non può essere vuoto.")

    expected_dnn = matched_slice.get("dnn")

    # Verifica che la DNN inviata corrisponda alla chiave o al network_name associato alla slice
    matched_dnn_obj = DNNS_MAP.get(req_dnn) or next((d for d in DNNS_MAP.values() if d["network_name"] == req_dnn),
                                                    None)

    if not matched_dnn_obj or (req_dnn != expected_dnn and matched_dnn_obj.get("network_name") != expected_dnn):
        raise HTTPException(
            status_code=400,
            detail=f"DNN '{req_dnn}' non valida per la slice '{slice_id}'. La DNN configurata per questa slice è '{expected_dnn}'."
        )
    return matched_dnn_obj