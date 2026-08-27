import subprocess
from fastapi import APIRouter, HTTPException, status
from schemas.pcc import PCCRuleReq
from services.db_service import get_config_db
from services.tc_service import (
    format_rate, clamp_prio, teardown_pcc_rule_network
)

router = APIRouter(prefix="/ues/{ue_id}/slices/{slice_id}/pcc_rules", tags=["PCC Rules"])


@router.post("", status_code=status.HTTP_201_CREATED)
def add_pcc_rule(ue_id: str, slice_id: str, req: PCCRuleReq):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id, ue_ip, ue_class_id, subclass_counter, if_slice, if_dnn FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, slice_id))
    sc_row = c.fetchone()
    if not sc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' per UE '{ue_id}' non trovata.")

    sc_id, ue_ip, ue_class_id, subclass_counter, if_slice, if_dnn = sc_row

    c.execute("SELECT id FROM pcc_rules WHERE slice_config_id = ? AND rule_name = ?", (sc_id, req.rule_name))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"PCC Rule '{req.rule_name}' già esistente per questa slice.")

    pcc_class_id = ue_class_id + subclass_counter
    gbr_ul = format_rate(req.qos.gbr_ul)
    mbr_ul = format_rate(req.qos.mbr_ul)
    gbr_dl = format_rate(req.qos.gbr_dl)
    mbr_dl = format_rate(req.qos.mbr_dl)
    remote_ip_clean = req.flow_description.remote_ip.split("/")[0]
    pcc_prio = clamp_prio(req.qos.priority if req.qos.priority is not None else req.qos.arp)

    if req.direction in ["downlink", "both"]:
        subprocess.run(
            f"tc class replace dev {if_slice} parent 1:{ue_class_id} classid 1:{pcc_class_id} htb rate {gbr_dl} ceil {mbr_dl} prio {pcc_prio}",
            shell=True, check=True
        )
        # Omettiamo completamente 'handle' per evitare conflitti con il kernel
        subprocess.run(
            f"tc filter add dev {if_slice} protocol ip parent 1:0 prio {pcc_prio} u32 match ip dst {ue_ip}/32 match ip src {remote_ip_clean}/32 flowid 1:{pcc_class_id}",
            shell=True, check=True
        )

    if req.direction in ["uplink", "both"]:
        subprocess.run(
            f"tc class replace dev {if_dnn} parent 1:{ue_class_id} classid 1:{pcc_class_id} htb rate {gbr_ul} ceil {mbr_ul} prio {pcc_prio}",
            shell=True, check=True
        )
        subprocess.run(
            f"tc filter add dev {if_dnn} protocol ip parent 1:0 prio {pcc_prio} u32 match ip src {ue_ip}/32 match ip dst {remote_ip_clean}/32 flowid 1:{pcc_class_id}",
            shell=True, check=True
        )

    c.execute("""
        INSERT INTO pcc_rules (slice_config_id, rule_name, direction, remote_ip, protocol, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority, class_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (sc_id, req.rule_name, req.direction, req.flow_description.remote_ip, req.flow_description.protocol,
          req.qos.gbr_ul, req.qos.gbr_dl, req.qos.mbr_ul, req.qos.mbr_dl, pcc_prio, pcc_class_id))

    c.execute("UPDATE ue_slice_configs SET subclass_counter = ? WHERE id = ?", (subclass_counter + 1, sc_id))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "ue_id": ue_id, "slice_id": slice_id, "rule_name": req.rule_name, "class_id": pcc_class_id}


@router.get("")
def get_pcc_rules(ue_id: str, slice_id: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, slice_id))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' per UE '{ue_id}' non trovata.")

    c.execute("SELECT rule_name, direction, remote_ip, protocol, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority FROM pcc_rules WHERE slice_config_id = ?", (row[0],))
    rules = c.fetchall()
    conn.close()

    return [{
        "rule_name": r[0], "direction": r[1],
        "flow_description": {"remote_ip": r[2], "protocol": r[3]},
        "qos": {"gbr_ul": r[4], "gbr_dl": r[5], "mbr_ul": r[6], "mbr_dl": r[7], "priority": r[8]}
    } for r in rules]

@router.put("/{rule_name}")
def update_pcc_rule(ue_id: str, slice_id: str, rule_name: str, req: PCCRuleReq):
    conn = get_config_db()
    c = conn.cursor()

    # 1. Verifica esistenza dell'UE e della Slice associata
    c.execute(
        "SELECT id, ue_ip, ue_class_id, if_slice, if_dnn FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?",
        (ue_id, slice_id)
    )
    sc_row = c.fetchone()
    if not sc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' per UE '{ue_id}' non trovata.")

    sc_id, ue_ip, ue_class_id, if_slice, if_dnn = sc_row

    # 2. Verifica esistenza della PCC Rule da modificare
    c.execute("SELECT id, remote_ip, class_id, priority, direction FROM pcc_rules WHERE slice_config_id = ? AND rule_name = ?",
              (sc_id, rule_name))
    pcc_row = c.fetchone()
    if not pcc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"PCC Rule '{rule_name}' non trovata.")

    pcc_id, old_remote_ip, pcc_class_id, old_prio, old_direction = pcc_row

    # 3. Controllo preventivo: se il nome cambia, verifica che il nuovo nome non appartenga già a un'altra regola della stessa slice
    if req.rule_name != rule_name:
        c.execute("SELECT id FROM pcc_rules WHERE slice_config_id = ? AND rule_name = ?", (sc_id, req.rule_name))
        if c.fetchone():
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"Impossibile rinominare: una PCC Rule con nome '{req.rule_name}' esiste già per questa slice."
            )

    # 4. Teardown della configurazione di rete precedente
    old_effective_prio = old_prio if old_prio is not None else 1
    teardown_pcc_rule_network(if_slice, if_dnn, ue_ip, old_remote_ip, pcc_class_id,
                              pcc_prio=old_effective_prio, direction=old_direction)

    # Formattazione parametri QoS e IP
    gbr_ul = format_rate(req.qos.gbr_ul)
    mbr_ul = format_rate(req.qos.mbr_ul)
    gbr_dl = format_rate(req.qos.gbr_dl)
    mbr_dl = format_rate(req.qos.mbr_dl)
    remote_ip_clean = req.flow_description.remote_ip.split("/")[0]
    pcc_prio = clamp_prio(req.qos.priority if req.qos.priority is not None else req.qos.arp)

    # 5. Applicazione dei nuovi filtri di rete
    if req.direction in ["downlink", "both"]:
        subprocess.run(
            f"tc class replace dev {if_slice} parent 1:{ue_class_id} classid 1:{pcc_class_id} htb rate {gbr_dl} ceil {mbr_dl} prio {pcc_prio}",
            shell=True, check=True
        )
        subprocess.run(
            f"tc filter add dev {if_slice} protocol ip parent 1:0 prio {pcc_prio} u32 match ip dst {ue_ip}/32 match ip src {remote_ip_clean}/32 flowid 1:{pcc_class_id}",
            shell=True, check=True
        )

    if req.direction in ["uplink", "both"]:
        subprocess.run(
            f"tc class replace dev {if_dnn} parent 1:{ue_class_id} classid 1:{pcc_class_id} htb rate {gbr_ul} ceil {mbr_ul} prio {pcc_prio}",
            shell=True, check=True
        )
        subprocess.run(
            f"tc filter add dev {if_dnn} protocol ip parent 1:0 prio {pcc_prio} u32 match ip src {ue_ip}/32 match ip dst {remote_ip_clean}/32 flowid 1:{pcc_class_id}",
            shell=True, check=True
        )

    # 6. Aggiornamento record nel Database
    c.execute("""
        UPDATE pcc_rules
        SET rule_name = ?, direction = ?, remote_ip = ?, protocol = ?, gbr_ul = ?, gbr_dl = ?, mbr_ul = ?, mbr_dl = ?, priority = ?
        WHERE id = ?
    """, (
        req.rule_name, req.direction, req.flow_description.remote_ip, req.flow_description.protocol,
        req.qos.gbr_ul, req.qos.gbr_dl, req.qos.mbr_ul, req.qos.mbr_dl, pcc_prio, pcc_id
    ))

    conn.commit()
    conn.close()

    return {
        "status": "SUCCESS",
        "ue_id": ue_id,
        "slice_id": slice_id,
        "rule_name": req.rule_name,
        "class_id": pcc_class_id
    }


@router.delete("/{rule_name}")
def delete_pcc_rule(ue_id: str, slice_id: str, rule_name: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id, ue_ip, if_slice, if_dnn FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, slice_id))
    sc_row = c.fetchone()
    if not sc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' per UE '{ue_id}' non trovata.")

    sc_id, ue_ip, if_slice, if_dnn = sc_row
    c.execute("SELECT id, remote_ip, class_id, priority FROM pcc_rules WHERE slice_config_id = ? AND rule_name = ?",
              (sc_id, rule_name))
    pcc_row = c.fetchone()
    if not pcc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"PCC Rule '{rule_name}' non trovata.")

    pcc_id, remote_ip, pcc_class_id, pcc_prio = pcc_row

    # Fallback difensivo nel caso priority sia NULL nel DB
    effective_prio = pcc_prio if pcc_prio is not None else 1

    teardown_pcc_rule_network(if_slice, if_dnn, ue_ip, remote_ip, pcc_class_id, pcc_prio=effective_prio)

    c.execute("DELETE FROM pcc_rules WHERE id = ?", (pcc_id,))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "message": f"PCC Rule '{rule_name}' rimossa per l'UE '{ue_id}'."}