import re
import time
import subprocess
from typing import Dict
from core.config import logger, SLICES_MAP, DNNS_MAP
from services.db_service import get_config_db, get_metrics_db


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

        # Scrive sul DB solo se vi è traffico non autorizzato scartato nel secondo corrente
        if unauth_pkts_sec > 0 or unauth_bytes_sec > 0:
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

            # Scrive sul DB se c'è traffico attivo, pacchetti inviati o pacchetti scartati
            if b_sec_ul > 0 or b_sec_dl > 0 or p_sec_ul > 0 or p_sec_dl > 0 or d_sec_ul > 0 or d_sec_dl > 0:
                c_met.execute("""
                    INSERT INTO mb_ue_stats (timestamp, ue_id, ue_ip, slice_id, dnn,
                                             bytes_tot_ul, bytes_tot_dl, pkts_tot_ul, pkts_tot_dl,
                                             dropped_tot_ul, dropped_tot_dl, overlimits_tot_ul, overlimits_tot_dl,
                                             bytes_sec_ul, bytes_sec_dl, pkts_sec_ul, pkts_sec_dl,
                                             dropped_sec_ul, dropped_sec_dl, overlimits_sec_ul, overlimits_sec_dl,
                                             throughput_mbps_ul, throughput_mbps_dl, ul_status, dl_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ts_iso, u_id, u_ip, s_name, d_name,
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

                    # Scrive sul DB se ci sono byte, pacchetti trasmessi o pacchetti droppati nel secondo corrente
                    if bs > 0 or ps > 0 or ds > 0:
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