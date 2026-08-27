import os
import re
import time
import yaml
import sqlite3
import ipaddress
import subprocess
import threading
import logging
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field
import docker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MiddleboxEngine")

# ------------------------------------------------------------------------------
# INIZIALIZZAZIONE & CONFIGURAZIONE
# ------------------------------------------------------------------------------

CONFIG_PATH = os.getenv("CONFIG_PATH", "./configs/config.yaml")
try:
    with open(CONFIG_PATH, "r") as f:
        CONFIG = yaml.safe_load(f)
    logger.info(f"Configurazione caricata da {CONFIG_PATH}")
except Exception as e:
    logger.error(f"Errore caricamento file di configurazione: {e}")
    raise RuntimeError(f"Errore config.yaml: {e}")

CONFIG_DB_PATH = os.getenv("CONFIG_DB_PATH", "/data/configs.db")
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", "/data/metrics.db")

app = FastAPI(title="5G Slice & QoS Control Plane / Middlebox", version="7.0")
docker_client = docker.from_env()

CLASS_COUNTER = 100
SLICES_MAP: Dict[str, dict] = {}
DNNS_MAP: Dict[str, dict] = {}


# ------------------------------------------------------------------------------
# GESTIONE DATABASE (CONFIGS.DB & METRICS.DB)
# ------------------------------------------------------------------------------

def get_config_db():
    os.makedirs(os.path.dirname(CONFIG_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(CONFIG_DB_PATH, timeout=10.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def get_metrics_db():
    os.makedirs(os.path.dirname(METRICS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(METRICS_DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_config_db():
    conn = get_config_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ues (
            ue_id TEXT PRIMARY KEY,
            container_id TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ue_slice_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ue_id TEXT NOT NULL,
            slice_id TEXT NOT NULL,
            dnn TEXT NOT NULL,
            ue_ip TEXT NOT NULL,
            ambr_ul TEXT NOT NULL,
            ambr_dl TEXT NOT NULL,
            ambr_prio INTEGER NOT NULL,
            ue_class_id INTEGER NOT NULL,
            if_slice TEXT NOT NULL,
            if_dnn TEXT NOT NULL,
            subclass_counter INTEGER DEFAULT 1,
            FOREIGN KEY (ue_id) REFERENCES ues(ue_id) ON DELETE CASCADE,
            UNIQUE(ue_id, slice_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pcc_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slice_config_id INTEGER NOT NULL,
            rule_name TEXT NOT NULL,
            direction TEXT NOT NULL,
            remote_ip TEXT NOT NULL,
            protocol TEXT DEFAULT 'any',
            gbr_ul TEXT NOT NULL,
            gbr_dl TEXT NOT NULL,
            mbr_ul TEXT NOT NULL,
            mbr_dl TEXT NOT NULL,
            priority INTEGER NOT NULL,
            class_id INTEGER NOT NULL,
            FOREIGN KEY (slice_config_id) REFERENCES ue_slice_configs(id) ON DELETE CASCADE,
            UNIQUE(slice_config_id, rule_name)
        )
    """)

    conn.commit()
    conn.close()


def init_metrics_db():
    conn = get_metrics_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mb_global_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            unauth_pkts_tot INTEGER,
            unauth_bytes_tot INTEGER,
            unauth_pkts_sec REAL,
            unauth_bytes_sec REAL,
            active_ues INTEGER,
            active_pcc_rules INTEGER,
            published INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mb_ue_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ue_ip TEXT,
            slice_id TEXT,
            dnn TEXT,
            bytes_tot_ul INTEGER, bytes_tot_dl INTEGER,
            pkts_tot_ul INTEGER, pkts_tot_dl INTEGER,
            dropped_tot_ul INTEGER, dropped_tot_dl INTEGER,
            overlimits_tot_ul INTEGER, overlimits_tot_dl INTEGER,
            bytes_sec_ul REAL, bytes_sec_dl REAL,
            pkts_sec_ul REAL, pkts_sec_dl REAL,
            dropped_sec_ul REAL, dropped_sec_dl REAL,
            overlimits_sec_ul REAL, overlimits_sec_dl REAL,
            throughput_mbps_ul REAL, throughput_mbps_dl REAL,
            ul_status TEXT, dl_status TEXT,
            published INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mb_flow_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            flow_id TEXT, ue_ip TEXT, rule_name TEXT, direction TEXT,
            remote_ip TEXT, gbr TEXT, mbr TEXT, priority INTEGER,
            bytes_tot INTEGER, pkts_tot INTEGER, dropped_tot INTEGER, overlimits_tot INTEGER,
            bytes_sec REAL, pkts_sec REAL, dropped_sec REAL, overlimits_sec REAL,
            throughput_mbps REAL, operation TEXT, is_throttled INTEGER, is_dropping INTEGER,
            published INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


# ------------------------------------------------------------------------------
# UTILITIES RETE & TC
# ------------------------------------------------------------------------------

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
    global SLICES_MAP, DNNS_MAP, CLASS_COUNTER

    for s_key, s_cfg in CONFIG.get("slices", {}).items():
        net_obj = ipaddress.ip_interface(f"{s_cfg['ip']}/{s_cfg['netmask']}").network
        SLICES_MAP[s_key] = {
            "network_name": s_cfg["network_name"],
            "ip": s_cfg["ip"], "netmask": s_cfg["netmask"],
            "network_obj": net_obj, "subnet": str(net_obj),
            "interface": get_interface_by_ip_netmask(s_cfg['ip'], s_cfg['netmask'])
        }

    for d_key, d_cfg in CONFIG.get("dnns", {}).items():
        net_obj = ipaddress.ip_interface(f"{d_cfg['ip']}/{d_cfg['netmask']}").network
        DNNS_MAP[d_key] = {
            "network_name": d_cfg["network_name"],
            "ip": d_cfg["ip"], "netmask": d_cfg["netmask"],
            "network_obj": net_obj, "subnet": str(net_obj),
            "interface": get_interface_by_ip_netmask(d_cfg['ip'], d_cfg['netmask'])
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


# ------------------------------------------------------------------------------
# LOGICA DI RIMOZIONE A CASCATA (NETWORK LEVEL)
# ------------------------------------------------------------------------------

def teardown_pcc_rule_network(if_slice: str, if_dnn: str, ue_ip: str, remote_ip: str, pcc_class_id: int):
    rem_ip = remote_ip.split("/")[0]
    subprocess.run(f"tc filter del dev {if_slice} protocol ip parent 1:0 prio 1 u32 match ip dst {ue_ip}/32 match ip src {rem_ip}/32", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run(f"tc filter del dev {if_dnn} protocol ip parent 1:0 prio 1 u32 match ip src {ue_ip}/32 match ip dst {rem_ip}/32", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run(f"tc class del dev {if_slice} classid 1:{pcc_class_id}", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run(f"tc class del dev {if_dnn} classid 1:{pcc_class_id}", shell=True, stderr=subprocess.DEVNULL)


def teardown_slice_config_network(conn: sqlite3.Connection, slice_config_id: int):
    c = conn.cursor()
    c.execute("SELECT ue_ip, dnn, ue_class_id, if_slice, if_dnn FROM ue_slice_configs WHERE id = ?", (slice_config_id,))
    sc = c.fetchone()
    if not sc:
        return
    ue_ip, dnn_req, ue_class_id, if_slice, if_dnn = sc

    c.execute("SELECT remote_ip, class_id FROM pcc_rules WHERE slice_config_id = ?", (slice_config_id,))
    pcc_rules = c.fetchall()
    for remote_ip, pcc_class_id in pcc_rules:
        teardown_pcc_rule_network(if_slice, if_dnn, ue_ip, remote_ip, pcc_class_id)

    dnn_subnet = None
    for d_data in DNNS_MAP.values():
        if dnn_req in (d_data["network_name"], d_data["ip"]):
            dnn_subnet = d_data["subnet"]
            break

    if dnn_subnet:
        subprocess.run(f"tc filter del dev {if_slice} protocol ip parent 1:0 prio 10 u32 match ip dst {ue_ip}/32 match ip src {dnn_subnet}", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run(f"tc filter del dev {if_dnn} protocol ip parent 1:0 prio 10 u32 match ip src {ue_ip}/32 match ip dst {dnn_subnet}", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run(f"iptables -D MB_FORWARD -i {if_slice} -o {if_dnn} -s {ue_ip}/32 -d {dnn_subnet} -m conntrack --ctstate NEW -j ACCEPT", shell=True, stderr=subprocess.DEVNULL)

    subprocess.run(f"tc class del dev {if_slice} classid 1:{ue_class_id}", shell=True, stderr=subprocess.DEVNULL)
    subprocess.run(f"tc class del dev {if_dnn} classid 1:{ue_class_id}", shell=True, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------------------
# TELEMETRIA E RACCOLTA METRICHE (BACKGROUND THREAD)
# ------------------------------------------------------------------------------

def parse_tc_classes(iface: str) -> Dict[str, Dict]:
    classes = {}
    try:
        res = subprocess.run(["tc", "-s", "class", "show", "dev", iface], capture_output=True, text=True, check=True)
        lines = res.stdout.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if "class htb" in line:
                cid_match = re.search(r"class htb (\d+:\d+)", line)
                if cid_match:
                    cid = cid_match.group(1)
                    stats_line = lines[i + 1] if (i + 1) < len(lines) else ""
                    b_m = re.search(r"Sent (\d+) bytes", stats_line)
                    p_m = re.search(r"(\d+) pkt", stats_line)
                    d_m = re.search(r"dropped (\d+)", stats_line)
                    o_m = re.search(r"overlimits (\d+)", stats_line)

                    classes[cid] = {
                        "bytes": int(b_m.group(1)) if b_m else 0,
                        "pkts": int(p_m.group(1)) if p_m else 0,
                        "dropped": int(d_m.group(1)) if d_m else 0,
                        "overlimits": int(o_m.group(1)) if o_m else 0
                    }
            i += 1
    except Exception as e:
        logger.error(f"Errore parsing TC {iface}: {e}")
    return classes


def collect_metrics_loop():
    prev_time = time.time()
    prev_global = {"pkts": 0, "bytes": 0}
    prev_ue = {}
    prev_flow = {}

    while True:
        time.sleep(1.0)
        curr_time = time.time()
        dt = max(curr_time - prev_time, 0.001)
        prev_time = curr_time
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(curr_time))

        all_ifaces = set([s["interface"] for s in SLICES_MAP.values()] + [d["interface"] for d in DNNS_MAP.values()])
        tc_cache = {iface: parse_tc_classes(iface) for iface in all_ifaces}

        conn_cfg = get_config_db()
        c_cfg = conn_cfg.cursor()

        conn_met = get_metrics_db()
        c_met = conn_met.cursor()

        # 1. METRICHE GLOBALI
        unauth_pkts_tot, unauth_bytes_tot = 0, 0
        try:
            res_fw = subprocess.run(["iptables", "-L", "MB_DROPPED", "-v", "-n", "-x"], capture_output=True, text=True, check=True)
            for line in res_fw.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
                    if parts[2] == "DROP":
                        unauth_pkts_tot, unauth_bytes_tot = int(parts[0]), int(parts[1])
        except Exception as e:
            logger.error(f"Errore lettura Firewall: {e}")

        unauth_pkts_sec = round(max(unauth_pkts_tot - prev_global["pkts"], 0) / dt, 2)
        unauth_bytes_sec = round(max(unauth_bytes_tot - prev_global["bytes"], 0) / dt, 2)
        prev_global = {"pkts": unauth_pkts_tot, "bytes": unauth_bytes_tot}

        c_cfg.execute("SELECT COUNT(DISTINCT ue_id) FROM ue_slice_configs")
        active_ues = c_cfg.fetchone()[0] or 0
        c_cfg.execute("SELECT COUNT(*) FROM pcc_rules")
        active_pcc_rules = c_cfg.fetchone()[0] or 0

        c_met.execute("""
            INSERT INTO mb_global_stats
            (timestamp, unauth_pkts_tot, unauth_bytes_tot, unauth_pkts_sec, unauth_bytes_sec, active_ues, active_pcc_rules)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ts_iso, unauth_pkts_tot, unauth_bytes_tot, unauth_pkts_sec, unauth_bytes_sec, active_ues, active_pcc_rules))

        # 2. METRICHE PER UE & SLICE
        c_cfg.execute("SELECT id, ue_id, slice_id, dnn, ue_ip, ue_class_id, if_slice, if_dnn FROM ue_slice_configs")
        slices = c_cfg.fetchall()

        for s_id, u_id, s_name, d_name, u_ip, u_cid_num, if_slice, if_dnn in slices:
            ue_cid = f"1:{u_cid_num}"
            dl_stats = tc_cache.get(if_slice, {}).get(ue_cid, {"bytes": 0, "pkts": 0, "dropped": 0, "overlimits": 0})
            ul_stats = tc_cache.get(if_dnn, {}).get(ue_cid, {"bytes": 0, "pkts": 0, "dropped": 0, "overlimits": 0})

            p_key = f"{u_id}_{s_name}"
            p_ue = prev_ue.get(p_key, {"b_ul": 0, "b_dl": 0, "p_ul": 0, "p_dl": 0, "d_ul": 0, "d_dl": 0, "o_ul": 0, "o_dl": 0})

            b_sec_ul = round(max(ul_stats["bytes"] - p_ue["b_ul"], 0) / dt, 2)
            b_sec_dl = round(max(dl_stats["bytes"] - p_ue["b_dl"], 0) / dt, 2)
            p_sec_ul = round(max(ul_stats["pkts"] - p_ue["p_ul"], 0) / dt, 2)
            p_sec_dl = round(max(dl_stats["pkts"] - p_ue["p_dl"], 0) / dt, 2)
            d_sec_ul = round(max(ul_stats["dropped"] - p_ue["d_ul"], 0) / dt, 2)
            d_sec_dl = round(max(dl_stats["dropped"] - p_ue["d_dl"], 0) / dt, 2)
            o_sec_ul = round(max(ul_stats["overlimits"] - p_ue["o_ul"], 0) / dt, 2)
            o_sec_dl = round(max(dl_stats["overlimits"] - p_ue["o_dl"], 0) / dt, 2)

            tp_ul = round((b_sec_ul * 8) / 1_000_000, 2)
            tp_dl = round((b_sec_dl * 8) / 1_000_000, 2)

            ul_status = "POLICING_DROPPING" if d_sec_ul > 0 else ("SHAPING_ACTIVE" if o_sec_ul > 0 else "NORMAL_FORWARDING")
            dl_status = "POLICING_DROPPING" if d_sec_dl > 0 else ("SHAPING_ACTIVE" if o_sec_dl > 0 else "NORMAL_FORWARDING")

            prev_ue[p_key] = {
                "b_ul": ul_stats["bytes"], "b_dl": dl_stats["bytes"],
                "p_ul": ul_stats["pkts"], "p_dl": dl_stats["pkts"],
                "d_ul": ul_stats["dropped"], "d_dl": dl_stats["dropped"],
                "o_ul": ul_stats["overlimits"], "o_dl": dl_stats["overlimits"]
            }

            c_met.execute("""
                INSERT INTO mb_ue_stats (timestamp, ue_ip, slice_id, dnn,
                                         bytes_tot_ul, bytes_tot_dl, pkts_tot_ul, pkts_tot_dl,
                                         dropped_tot_ul, dropped_tot_dl, overlimits_tot_ul, overlimits_tot_dl,
                                         bytes_sec_ul, bytes_sec_dl, pkts_sec_ul, pkts_sec_dl,
                                         dropped_sec_ul, dropped_sec_dl, overlimits_sec_ul, overlimits_sec_dl,
                                         throughput_mbps_ul, throughput_mbps_dl, ul_status, dl_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ts_iso, u_ip, s_name, d_name,
                ul_stats["bytes"], dl_stats["bytes"], ul_stats["pkts"], dl_stats["pkts"],
                ul_stats["dropped"], dl_stats["dropped"], ul_stats["overlimits"], dl_stats["overlimits"],
                b_sec_ul, b_sec_dl, p_sec_ul, p_sec_dl, d_sec_ul, d_sec_dl, o_sec_ul, o_sec_dl,
                tp_ul, tp_dl, ul_status, dl_status
            ))

            # 3. METRICHE PER REGOLA PCC (FLUSSI)
            c_cfg.execute("SELECT rule_name, direction, remote_ip, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority, class_id FROM pcc_rules WHERE slice_config_id = ?", (s_id,))
            rules = c_cfg.fetchall()

            for r_name, r_dir, rem_ip, g_ul, g_dl, m_ul, m_dl, r_prio, r_cid_num in rules:
                pcc_cid = f"1:{r_cid_num}"
                dirs = ["downlink", "uplink"] if r_dir == "both" else [r_dir]

                for d in dirs:
                    flow_id = f"{u_ip}_{r_name}_{d}"
                    target_if = if_slice if d == "downlink" else if_dnn
                    f_stats = tc_cache.get(target_if, {}).get(pcc_cid, {"bytes": 0, "pkts": 0, "dropped": 0, "overlimits": 0})
                    p_flow = prev_flow.get(flow_id, {"b": 0, "p": 0, "d": 0, "o": 0})

                    bs = round(max(f_stats["bytes"] - p_flow["b"], 0) / dt, 2)
                    ps = round(max(f_stats["pkts"] - p_flow["p"], 0) / dt, 2)
                    ds = round(max(f_stats["dropped"] - p_flow["d"], 0) / dt, 2)
                    os_val = round(max(f_stats["overlimits"] - p_flow["o"], 0) / dt, 2)
                    tp = round((bs * 8) / 1_000_000, 2)

                    is_drop = 1 if ds > 0 else 0
                    is_throt = 1 if (ds > 0 or os_val > 0) else 0
                    op = "POLICING_DROPPING" if is_drop else ("SHAPING_ACTIVE" if is_throt else "NORMAL_FORWARDING")

                    prev_flow[flow_id] = {"b": f_stats["bytes"], "p": f_stats["pkts"], "d": f_stats["dropped"], "o": f_stats["overlimits"]}

                    c_met.execute("""
                        INSERT INTO mb_flow_stats (timestamp, flow_id, ue_ip, rule_name, direction,
                                                   remote_ip, gbr, mbr, priority,
                                                   bytes_tot, pkts_tot, dropped_tot, overlimits_tot,
                                                   bytes_sec, pkts_sec, dropped_sec, overlimits_sec,
                                                   throughput_mbps, operation, is_throttled, is_dropping)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        ts_iso, flow_id, u_ip, r_name, d, rem_ip,
                        g_dl if d == "downlink" else g_ul,
                        m_dl if d == "downlink" else m_ul, r_prio,
                        f_stats["bytes"], f_stats["pkts"], f_stats["dropped"], f_stats["overlimits"],
                        bs, ps, ds, os_val, tp, op, is_throt, is_drop
                    ))

        conn_cfg.close()
        conn_met.commit()
        conn_met.close()


# ------------------------------------------------------------------------------
# MODELLI PYDANTIC PER LE RESTful API
# ------------------------------------------------------------------------------

class FlowDescription(BaseModel):
    remote_ip: str
    protocol: Optional[str] = "any"

class QoSParams(BaseModel):
    five_qi: int = 9
    arp: int = 1
    gbr_ul: str
    gbr_dl: str
    mbr_ul: str
    mbr_dl: str
    priority: Optional[int] = Field(default=1, description="Priorità HTB: 0 (max) - 7 (min)")

class PCCRuleReq(BaseModel):
    rule_name: str
    direction: str = "both"
    flow_description: FlowDescription
    qos: QoSParams

class AMBR(BaseModel):
    ul_br: str
    dl_br: str
    priority: Optional[int] = Field(default=7, description="Priorità HTB AMBR")

class SliceConfigReq(BaseModel):
    slice_id: str
    dnn: str
    ue_ip: str
    ambr: AMBR

class UECreateReq(BaseModel):
    ue_id: str
    spawn_container: Optional[bool] = False
    slice_ips: Optional[Dict[str, str]] = None


# ------------------------------------------------------------------------------
# ENDPOINTS RESTful API
# ------------------------------------------------------------------------------

@app.on_event("startup")
def startup_event():
    init_config_db()
    init_metrics_db()
    init_firewall_and_tc()
    threading.Thread(target=collect_metrics_loop, daemon=True).start()
    logger.info("Control Plane & Middlebox pronti.")


# --- 1. GESTIONE UE (/ues) ---

@app.post("/ues", status_code=status.HTTP_201_CREATED)
def create_ue(req: UECreateReq):
    # Controllo obbligatorietà slice_ips: un UE deve appartenere ad almeno una slice
    if not req.slice_ips or len(req.slice_ips) == 0:
        raise HTTPException(
            status_code=400,
            detail="Impossibile creare l'UE: è obbligatorio specificare almeno un indirizzo IP per una slice in 'slice_ips'."
        )

    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT ue_id FROM ues WHERE ue_id = ?", (req.ue_id,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"UE '{req.ue_id}' già esistente.")

    # Validazione preventiva dell'esistenza di tutte le slice specificate
    for s_key_or_name in req.slice_ips.keys():
        matched_slice = find_slice_info(s_key_or_name)
        if not matched_slice:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"Slice/Rete '{s_key_or_name}' non trovata. Slice valide: {list(SLICES_MAP.keys())}"
            )

    container_id = None
    if req.spawn_container:
        try:
            # 1. Creazione container base
            container = docker_client.containers.run(
                image="ubuntu:22.04",
                name=req.ue_id, detach=True, tty=True, cap_add=["NET_ADMIN"],
                command="bash -c 'apt-get update && apt-get install -y iproute2 iperf3 iperf iputils-ping && tail -f /dev/null'"
            )
            container_id = container.id

            # 2. Collegamento esplicito alle reti delle slice
            for s_key_or_name, ip_val in req.slice_ips.items():
                matched_slice = find_slice_info(s_key_or_name)
                docker_net = docker_client.networks.get(matched_slice["network_name"])
                docker_net.connect(container, ipv4_address=ip_val)
                logger.info(f"Connesso {req.ue_id} alla rete {matched_slice['network_name']} con IP {ip_val}")

            # 3. Rimozione dalla rete bridge di default
            try:
                default_bridge = docker_client.networks.get("bridge")
                default_bridge.disconnect(container)
            except Exception:
                pass

        except HTTPException:
            raise
        except Exception as e:
            conn.close()
            logger.error(f"Errore durante lo spawn Docker: {e}")
            raise HTTPException(status_code=500, detail=f"Docker spawn error: {e}")

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c.execute("INSERT INTO ues (ue_id, container_id, created_at) VALUES (?, ?, ?)", (req.ue_id, container_id, now_iso))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "ue_id": req.ue_id, "container_id": container_id, "created_at": now_iso}


@app.get("/ues")
def list_ues():
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT ue_id, container_id, created_at FROM ues")
    ues_db = c.fetchall()

    result = []
    for u_id, c_id, c_at in ues_db:
        c.execute("SELECT id, slice_id, dnn, ue_ip, ambr_ul, ambr_dl, ambr_prio FROM ue_slice_configs WHERE ue_id = ?", (u_id,))
        slices_db = c.fetchall()

        slices = []
        for sc_id, s_id, d_name, u_ip, a_ul, a_dl, a_prio in slices_db:
            c.execute("SELECT rule_name, direction, remote_ip, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority FROM pcc_rules WHERE slice_config_id = ?", (sc_id,))
            pccs = [{
                "rule_name": r[0], "direction": r[1], "remote_ip": r[2],
                "gbr_ul": r[3], "gbr_dl": r[4], "mbr_ul": r[5], "mbr_dl": r[6], "priority": r[7]
            } for r in c.fetchall()]

            slices.append({
                "slice_id": s_id, "dnn": d_name, "ue_ip": u_ip,
                "ambr": {"ul_br": a_ul, "dl_br": a_dl, "priority": a_prio},
                "pcc_rules": pccs
            })

        result.append({"ue_id": u_id, "container_id": c_id, "created_at": c_at, "slices": slices})

    conn.close()
    return result


@app.get("/ues/{ue_id}")
def get_ue(ue_id: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT ue_id, container_id, created_at FROM ues WHERE ue_id = ?", (ue_id,))
    ue_row = c.fetchone()
    if not ue_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"UE '{ue_id}' non trovato.")

    c.execute("SELECT id, slice_id, dnn, ue_ip, ambr_ul, ambr_dl, ambr_prio FROM ue_slice_configs WHERE ue_id = ?", (ue_id,))
    slices_db = c.fetchall()

    slices = []
    for sc_id, s_id, d_name, u_ip, a_ul, a_dl, a_prio in slices_db:
        c.execute("SELECT rule_name, direction, remote_ip, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority FROM pcc_rules WHERE slice_config_id = ?", (sc_id,))
        pccs = [{
            "rule_name": r[0], "direction": r[1], "remote_ip": r[2],
            "gbr_ul": r[3], "gbr_dl": r[4], "mbr_ul": r[5], "mbr_dl": r[6], "priority": r[7]
        } for r in c.fetchall()]

        slices.append({
            "slice_id": s_id, "dnn": d_name, "ue_ip": u_ip,
            "ambr": {"ul_br": a_ul, "dl_br": a_dl, "priority": a_prio},
            "pcc_rules": pccs
        })

    conn.close()
    return {"ue_id": ue_row[0], "container_id": ue_row[1], "created_at": ue_row[2], "slices": slices}


@app.delete("/ues/{ue_id}")
def delete_ue(ue_id: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT ue_id, container_id FROM ues WHERE ue_id = ?", (ue_id,))
    ue_row = c.fetchone()
    if not ue_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"UE '{ue_id}' non trovato.")

    c.execute("SELECT id FROM ue_slice_configs WHERE ue_id = ?", (ue_id,))
    slice_ids = [row[0] for row in c.fetchall()]
    for sc_id in slice_ids:
        teardown_slice_config_network(conn, sc_id)

    c.execute("DELETE FROM ues WHERE ue_id = ?", (ue_id,))
    conn.commit()
    conn.close()

    try:
        container = docker_client.containers.get(ue_id)
        container.stop()
        container.remove(force=True)
    except Exception:
        pass

    return {"status": "SUCCESS", "message": f"UE '{ue_id}' e tutte le sue risorse sono state rimosse."}


# --- 2. GESTIONE SLICE (/ues/{ue_id}/slices) ---

@app.post("/ues/{ue_id}/slices", status_code=status.HTTP_201_CREATED)
def create_slice_config(ue_id: str, req: SliceConfigReq):
    global CLASS_COUNTER
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

    matched_dnn = next((d for k, d in DNNS_MAP.items() if req.dnn in (k, d["network_name"])), None)
    if not matched_dnn:
        conn.close()
        raise HTTPException(status_code=400, detail=f"DNN '{req.dnn}' non valida.")

    c.execute("SELECT id FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, req.slice_id))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"Slice '{req.slice_id}' già configurata per UE '{ue_id}'. Usare PUT per aggiornare.")

    ue_class_id = CLASS_COUNTER
    CLASS_COUNTER += 10

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


@app.get("/ues/{ue_id}/slices")
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


@app.put("/ues/{ue_id}/slices/{slice_id}")
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
    matched_dnn = next((d for k, d in DNNS_MAP.items() if req.dnn in (k, d["network_name"])), None)

    if_slice = matched_slice["interface"]
    if_dnn = matched_dnn["interface"]
    dnn_subnet = matched_dnn["subnet"]

    ambr_ul = format_rate(req.ambr.ul_br)
    ambr_dl = format_rate(req.ambr.dl_br)
    ambr_prio = clamp_prio(req.ambr.priority if req.ambr.priority is not None else 7)

    # subprocess.run(f"tc class add dev {if_slice} parent 1: classid 1:{old_ue_class_id} htb rate {ambr_dl} ceil {ambr_dl} prio {ambr_prio}", shell=True, check=True)
    # subprocess.run(f"tc class add dev {if_dnn} parent 1: classid 1:{old_ue_class_id} htb rate {ambr_ul} ceil {ambr_ul} prio {ambr_prio}", shell=True, check=True)

    subprocess.run(
        f"tc class replace dev {if_slice} parent 1: classid 1:{old_ue_class_id} htb rate {ambr_dl} ceil {ambr_dl} prio {ambr_prio}",
        shell=True, check=True)
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


@app.delete("/ues/{ue_id}/slices/{slice_id}")
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


# --- 3. GESTIONE PCC RULES (/ues/{ue_id}/slices/{slice_id}/pcc_rules) ---

@app.post("/ues/{ue_id}/slices/{slice_id}/pcc_rules", status_code=status.HTTP_201_CREATED)
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
        subprocess.run(f"tc class add dev {if_slice} parent 1:{ue_class_id} classid 1:{pcc_class_id} htb rate {gbr_dl} ceil {mbr_dl} prio {pcc_prio}", shell=True, check=True)
        subprocess.run(f"tc filter add dev {if_slice} protocol ip parent 1:0 prio 1 u32 match ip dst {ue_ip}/32 match ip src {remote_ip_clean}/32 flowid 1:{pcc_class_id}", shell=True, check=True)

    if req.direction in ["uplink", "both"]:
        subprocess.run(f"tc class add dev {if_dnn} parent 1:{ue_class_id} classid 1:{pcc_class_id} htb rate {gbr_ul} ceil {mbr_ul} prio {pcc_prio}", shell=True, check=True)
        subprocess.run(f"tc filter add dev {if_dnn} protocol ip parent 1:0 prio 1 u32 match ip src {ue_ip}/32 match ip dst {remote_ip_clean}/32 flowid 1:{pcc_class_id}", shell=True, check=True)

    c.execute("""
        INSERT INTO pcc_rules (slice_config_id, rule_name, direction, remote_ip, protocol, gbr_ul, gbr_dl, mbr_ul, mbr_dl, priority, class_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (sc_id, req.rule_name, req.direction, req.flow_description.remote_ip, req.flow_description.protocol,
          req.qos.gbr_ul, req.qos.gbr_dl, req.qos.mbr_ul, req.qos.mbr_dl, pcc_prio, pcc_class_id))

    c.execute("UPDATE ue_slice_configs SET subclass_counter = ? WHERE id = ?", (subclass_counter + 1, sc_id))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "ue_id": ue_id, "slice_id": slice_id, "rule_name": req.rule_name, "class_id": pcc_class_id}


@app.get("/ues/{ue_id}/slices/{slice_id}/pcc_rules")
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


@app.delete("/ues/{ue_id}/slices/{slice_id}/pcc_rules/{rule_name}")
def delete_pcc_rule(ue_id: str, slice_id: str, rule_name: str):
    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT id, ue_ip, if_slice, if_dnn FROM ue_slice_configs WHERE ue_id = ? AND slice_id = ?", (ue_id, slice_id))
    sc_row = c.fetchone()
    if not sc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Slice '{slice_id}' per UE '{ue_id}' non trovata.")

    sc_id, ue_ip, if_slice, if_dnn = sc_row
    c.execute("SELECT id, remote_ip, class_id FROM pcc_rules WHERE slice_config_id = ? AND rule_name = ?", (sc_id, rule_name))
    pcc_row = c.fetchone()
    if not pcc_row:
        conn.close()
        raise HTTPException(status_code=404, detail=f"PCC Rule '{rule_name}' non trovata.")

    pcc_id, remote_ip, pcc_class_id = pcc_row
    teardown_pcc_rule_network(if_slice, if_dnn, ue_ip, remote_ip, pcc_class_id)

    c.execute("DELETE FROM pcc_rules WHERE id = ?", (pcc_id,))
    conn.commit()
    conn.close()

    return {"status": "SUCCESS", "message": f"PCC Rule '{rule_name}' rimossa per l'UE '{ue_id}'."}