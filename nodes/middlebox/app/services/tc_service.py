import ipaddress
import re
import subprocess
import sqlite3
from typing import Optional
from core.config import CONFIG, SLICES_MAP, DNNS_MAP, logger
from services.db_service import get_config_db

# Stato interno per la generazione sequenziale dei classid HTB
CLASS_COUNTER = 100

def get_next_class_id() -> int:
    global CLASS_COUNTER
    cid = CLASS_COUNTER
    CLASS_COUNTER += 10
    return cid


def get_interface_by_ip_netmask(ip: str, netmask: str) -> str:
    target_net = ipaddress.ip_interface(f"{ip}/{netmask}").network
    res = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, check=True)
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ifname = parts[1]
            cidr = parts[3]
            try:
                if ipaddress.ip_interface(cidr).network == target_net:
                    return ifname
            except ValueError:
                continue
    raise RuntimeError(f"Interfaccia non trovata per rete: {target_net}")


def init_firewall_and_tc():
    global CLASS_COUNTER

    # 1. Popolamento e validazione delle DNNs
    for d_key, d_cfg in CONFIG.get("dnns", {}).items():
        gateway_ip = d_cfg["gateway_ip"]
        subnet_mask = d_cfg["subnet_mask"]
        subnet_cidr = d_cfg["subnet_cidr"]

        net_obj = ipaddress.ip_interface(f"{gateway_ip}/{subnet_mask}").network
        DNNS_MAP[d_key] = {
            "network_name": d_cfg["network_name"],
            "gateway_ip": gateway_ip,
            "subnet_mask": subnet_mask,
            "subnet_cidr": subnet_cidr,
            "ip": gateway_ip,       # Alias per retrocompatibilità
            "netmask": subnet_mask, # Alias per retrocompatibilità
            "network_obj": net_obj,
            "subnet": str(net_obj),
            "interface": get_interface_by_ip_netmask(gateway_ip, subnet_mask)
        }

    # 2. Popolamento e validazione delle Slices
    for s_key, s_cfg in CONFIG.get("slices", {}).items():
        dnn_ref = s_cfg.get("dnn")
        if not dnn_ref or not isinstance(dnn_ref, str):
            raise RuntimeError(f"Configurazione non valida: la slice '{s_key}' deve specificare 'dnn'.")

        if dnn_ref not in DNNS_MAP and not any(d["network_name"] == dnn_ref for d in DNNS_MAP.values()):
            raise RuntimeError(
                f"Configurazione non valida: la DNN '{dnn_ref}' associata alla slice '{s_key}' non esiste tra le 'dnns'.")

        gateway_ip = s_cfg["gateway_ip"]
        subnet_mask = s_cfg["subnet_mask"]
        subnet_cidr = s_cfg["subnet_cidr"]

        net_obj = ipaddress.ip_interface(f"{gateway_ip}/{subnet_mask}").network
        SLICES_MAP[s_key] = {
            "network_name": s_cfg["network_name"],
            "gateway_ip": gateway_ip,
            "subnet_mask": subnet_mask,
            "subnet_cidr": subnet_cidr,
            "ip": gateway_ip,       # Alias per retrocompatibilità
            "netmask": subnet_mask, # Alias per retrocompatibilità
            "network_obj": net_obj,
            "subnet": str(net_obj),
            "interface": get_interface_by_ip_netmask(gateway_ip, subnet_mask),
            "dnn": dnn_ref
        }

    root_rate = CONFIG["middlebox"]["default_root_rate"]
    default_prio = CONFIG["middlebox"]["default_prio"]

    subprocess.run("iptables -F FORWARD", shell=True)
    subprocess.run("iptables -F MB_FORWARD 2>/dev/null", shell=True)
    subprocess.run("iptables -X MB_FORWARD 2>/dev/null", shell=True)
    subprocess.run("iptables -F MB_DROPPED 2>/dev/null", shell=True)
    subprocess.run("iptables -X MB_DROPPED 2>/dev/null", shell=True)

    subprocess.run("iptables -N MB_FORWARD", shell=True)
    subprocess.run("iptables -N MB_DROPPED", shell=True)
    subprocess.run("iptables -I FORWARD 1 -j MB_FORWARD", shell=True)
    subprocess.run("iptables -A MB_FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT", shell=True)
    subprocess.run("iptables -A MB_DROPPED -j DROP", shell=True)
    subprocess.run("iptables -A MB_FORWARD -j MB_DROPPED", shell=True)

    all_interfaces = set([s["interface"] for s in SLICES_MAP.values()] + [d["interface"] for d in DNNS_MAP.values()])
    for iface in all_interfaces:
        subprocess.run(f"tc qdisc del dev {iface} root", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run(f"tc qdisc add dev {iface} root handle 1: htb default 999", shell=True, check=True)
        subprocess.run(f"tc class add dev {iface} parent 1: classid 1:999 htb rate {root_rate} ceil {root_rate} prio {default_prio}", shell=True, check=True)

    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT MAX(ue_class_id) FROM ue_slice_configs")
    max_id = c.fetchone()[0]
    if max_id:
        CLASS_COUNTER = max(CLASS_COUNTER, max_id + 50)
    conn.close()


def format_rate(rate_str: str) -> str:
    rate = rate_str.lower().replace("b", "").replace("ps", "")
    if "m" in rate: return rate.replace("m", "mbit")
    if "k" in rate: return rate.replace("k", "kbit")
    if "g" in rate: return rate.replace("g", "gbit")
    return f"{rate}bit"


def clamp_prio(prio: int) -> int:
    return max(0, min(7, prio))


def find_slice_info(slice_identifier: str) -> Optional[dict]:
    s_id_lower = slice_identifier.lower().strip()
    for k, v in SLICES_MAP.items():
        if k.lower() == s_id_lower:
            return v
    for k, v in SLICES_MAP.items():
        if v.get("network_name", "").lower() == s_id_lower:
            return v
    return None


# def teardown_pcc_rule_network(if_slice: str, if_dnn: str, ue_ip: str, remote_ip: str, pcc_class_id: int):
#     rem_ip = remote_ip.split("/")[0]
#     subprocess.run(f"tc filter del dev {if_slice} protocol ip parent 1:0 prio 1 u32 match ip dst {ue_ip}/32 match ip src {rem_ip}/32", shell=True, stderr=subprocess.DEVNULL)
#     subprocess.run(f"tc filter del dev {if_dnn} protocol ip parent 1:0 prio 1 u32 match ip src {ue_ip}/32 match ip dst {rem_ip}/32", shell=True, stderr=subprocess.DEVNULL)
#     subprocess.run(f"tc class del dev {if_slice} classid 1:{pcc_class_id}", shell=True, stderr=subprocess.DEVNULL)
#     subprocess.run(f"tc class del dev {if_dnn} classid 1:{pcc_class_id}", shell=True, stderr=subprocess.DEVNULL)

########

# def teardown_pcc_rule_network(if_slice: str, if_dnn: str, ue_ip: str, remote_ip: str, pcc_class_id: int,
#                               pcc_prio: int = 1):
#     # Cancellazione deterministica per handle e priorità
#     subprocess.run(
#         f"tc filter del dev {if_slice} protocol ip parent 1:0 prio {pcc_prio} handle 800::{pcc_class_id} u32",
#         shell=True, stderr=subprocess.DEVNULL)
#     subprocess.run(f"tc filter del dev {if_dnn} protocol ip parent 1:0 prio {pcc_prio} handle 800::{pcc_class_id} u32",
#                    shell=True, stderr=subprocess.DEVNULL)
#
#     subprocess.run(f"tc class del dev {if_slice} classid 1:{pcc_class_id}", shell=True, stderr=subprocess.DEVNULL)
#     subprocess.run(f"tc class del dev {if_dnn} classid 1:{pcc_class_id}", shell=True, stderr=subprocess.DEVNULL)

def teardown_pcc_rule_network(if_slice: str, if_dnn: str, ue_ip: str, remote_ip: str, pcc_class_id: int,
                              pcc_prio: int = 1, direction: str = "both"):
    devs = []
    if direction in ["downlink", "both"] and if_slice:
        devs.append(if_slice)
    if direction in ["uplink", "both"] and if_dnn:
        devs.append(if_dnn)

    for dev in set(devs):
        # 1. Eliminazione dinamica del filtro basata sull'handle assegnato dal kernel
        remove_u32_filter_by_classid(dev, pcc_class_id)
        # 2. Eliminazione della classe HTB
        subprocess.run(
            f"tc class del dev {dev} classid 1:{pcc_class_id}",
            shell=True, stderr=subprocess.DEVNULL
        )

def teardown_slice_config_network(sc_id: int):
    conn = get_config_db()
    c = conn.cursor()

    # 1. Recupero dati della Slice Config
    c.execute(
        "SELECT ue_ip, ue_class_id, if_slice, if_dnn FROM ue_slice_configs WHERE id = ?",
        (sc_id,)
    )
    sc_row = c.fetchone()
    if not sc_row:
        conn.close()
        return

    ue_ip, ue_class_id, if_slice, if_dnn = sc_row

    # 2. Recupero ed eliminazione di tutte le PCC Rules collegate (incluso il campo priority)
    c.execute(
        "SELECT remote_ip, class_id, priority FROM pcc_rules WHERE slice_config_id = ?",
        (sc_id,)
    )
    pcc_rows = c.fetchall()

    for r_ip, p_class_id, p_prio in pcc_rows:
        # Fallback a 1 se priority nel DB è NULL
        effective_prio = p_prio if p_prio is not None else 1
        teardown_pcc_rule_network(if_slice, if_dnn, ue_ip, r_ip, p_class_id, effective_prio)

    # 3. Rimozione della classe UE principale ed eliminazione dal DB
    subprocess.run(f"tc class del dev {if_slice} classid 1:{ue_class_id}", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run(f"tc class del dev {if_dnn} classid 1:{ue_class_id}", shell=True, stderr=subprocess.DEVNULL)

    c.execute("DELETE FROM pcc_rules WHERE slice_config_id = ?", (sc_id,))
    c.execute("DELETE FROM ue_slice_configs WHERE id = ?", (sc_id,))

    conn.commit()
    conn.close()


### HELPERS ###
def remove_u32_filter_by_classid(dev: str, class_id: int):
    if not dev:
        return
    res = subprocess.run(f"tc filter show dev {dev} parent 1:0", shell=True, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout:
        return

    # Il classid può comparire in decimale (1:101) o esadecimale (1:65)
    target_ids = [f"1:{class_id}", f"1:{class_id:x}"]
    blocks = res.stdout.split("filter ")

    for block in blocks:
        if not block.strip():
            continue
        if any(f"flowid {tid}" in block or f"flowid  {tid}" in block for tid in target_ids):
            fh_match = re.search(r'fh\s+([0-9a-fA-F:]+)', block)
            prio_match = re.search(r'(?:pref|prio)\s+(\d+)', block)

            handle = fh_match.group(1) if fh_match else None
            prio = prio_match.group(1) if prio_match else "1"

            if handle:
                subprocess.run(
                    f"tc filter del dev {dev} parent 1:0 protocol ip prio {prio} handle {handle} u32",
                    shell=True, stderr=subprocess.DEVNULL
                )