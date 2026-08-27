import json
import logging
import os
import sqlite3
import time
import zmq

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("TelemetryManager")

DB_PATH = os.getenv("DB_PATH", "/data/metrics.db")
ZMQ_PORT = os.getenv("ZMQ_PORT", "5555")


def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def start_telemetry_publisher():
    context = zmq.Context()
    socket = context.socket(zmq.PUB)
    socket.bind(f"tcp://0.0.0.0:{ZMQ_PORT}")
    logger.info(
        f"Telemetry Manager pronto. Broadcasting ZeroMQ attivo su porta {ZMQ_PORT}..."
    )

    while True:
        if not os.path.exists(DB_PATH):
            time.sleep(0.5)
            continue

        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            # 1. TOPIC: metrics.server.iperf (Server Side)
            cursor.execute("""
                SELECT id, timestamp, server_ip, server_port, client_ip, client_port, conn_id, interval, 
                       bytes_transferred, bandwidth_mbps, protocol, direction, jitter_ms, lost_packets, total_packets, packet_loss_pct
                FROM server_iperf_stats WHERE published = 0 ORDER BY id ASC LIMIT 50
            """)
            for row in cursor.fetchall():
                (
                    r_id,
                    ts,
                    s_ip,
                    s_port,
                    c_ip,
                    c_port,
                    conn_id,
                    interval,
                    bytes_tx,
                    mbps,
                    protocol,
                    direction,
                    jitter_ms,
                    lost_pkts,
                    total_pkts,
                    loss_pct,
                ) = row

                payload = {
                    "topic": "metrics.server.iperf",
                    "timestamp": ts,
                    "server": {"ip": s_ip, "port": s_port},
                    "client": {"ip": c_ip, "port": c_port},
                    "session": {
                        "connection_id": conn_id,
                        "protocol": protocol,  # Dinamico: 'TCP' o 'UDP'
                        "direction": direction,  # Dinamico: 'UPLINK' o 'DOWNLINK'
                        "interval_seconds": interval,
                    },
                    "performance": {
                        "bytes_transferred": bytes_tx,
                        "bandwidth_mbps": mbps,
                        "jitter_ms": jitter_ms,
                        "lost_packets": lost_pkts,
                        "total_packets": total_pkts,
                        "packet_loss_pct": loss_pct,
                    },
                }
                socket.send_string(f"metrics.server.iperf {json.dumps(payload)}")
                cursor.execute(
                    "UPDATE server_iperf_stats SET published = 1 WHERE id = ?", (r_id,)
                )

            # 2. TOPIC: metrics.ue.iperf (UE Side Agent)
            cursor.execute("""
                SELECT id, timestamp, ue_id, server_ip, server_port, client_ip, client_port, conn_id, interval, 
                       bytes_transferred, bandwidth_mbps, protocol, direction, jitter_ms, lost_packets, total_packets, packet_loss_pct
                FROM ue_iperf_stats WHERE published = 0 ORDER BY id ASC LIMIT 50
            """)
            for row in cursor.fetchall():
                (
                    r_id,
                    ts,
                    ue_id,
                    s_ip,
                    s_port,
                    c_ip,
                    c_port,
                    conn_id,
                    interval,
                    bytes_tx,
                    mbps,
                    protocol,
                    direction,
                    jitter_ms,
                    lost_pkts,
                    total_pkts,
                    loss_pct,
                ) = row

                payload = {
                    "topic": "metrics.ue.iperf",
                    "timestamp": ts,
                    "ue_id": ue_id,
                    "server": {"ip": s_ip, "port": s_port},
                    "client": {"ip": c_ip, "port": c_port},
                    "session": {
                        "connection_id": conn_id,
                        "protocol": protocol,
                        "direction": direction,
                        "interval_seconds": interval,
                    },
                    "performance": {
                        "bytes_transferred": bytes_tx,
                        "bandwidth_mbps": mbps,
                        "jitter_ms": jitter_ms,
                        "lost_packets": lost_pkts,
                        "total_packets": total_pkts,
                        "packet_loss_pct": loss_pct,
                    },
                }
                socket.send_string(f"metrics.ue.iperf {json.dumps(payload)}")
                cursor.execute(
                    "UPDATE ue_iperf_stats SET published = 1 WHERE id = ?", (r_id,)
                )

            # 3. TOPIC: metrics.middlebox.global
            cursor.execute("""
                SELECT id, timestamp, unauth_pkts_tot, unauth_bytes_tot, unauth_pkts_sec, unauth_bytes_sec, active_ues, active_pcc_rules
                FROM mb_global_stats WHERE published = 0 ORDER BY id ASC LIMIT 50
            """)
            for row in cursor.fetchall():
                r_id, ts, p_tot, b_tot, p_sec, b_sec, ues, rules = row
                payload = {
                    "topic": "metrics.middlebox.global",
                    "timestamp": ts,
                    "metrics": {
                        "aggregated_tot": {
                            "unauthorized_packets_dropped_tot": p_tot,
                            "unauthorized_bytes_dropped_tot": b_tot,
                        },
                        "per_second": {
                            "unauthorized_packets_dropped_per_second": p_sec,
                            "unauthorized_bytes_dropped_per_second": b_sec,
                        },
                    },
                    "summary": {"active_ues": ues, "active_pcc_rules": rules},
                }
                socket.send_string(
                    f"metrics.middlebox.global {json.dumps(payload)}"
                )
                cursor.execute(
                    "UPDATE mb_global_stats SET published = 1 WHERE id = ?",
                    (r_id,),
                )

            # 4. TOPIC: metrics.middlebox.ue
            cursor.execute("""
                SELECT id, timestamp, ue_id, ue_ip, slice_id, dnn,
                       bytes_tot_ul, bytes_tot_dl, pkts_tot_ul, pkts_tot_dl, dropped_tot_ul, dropped_tot_dl, overlimits_tot_ul, overlimits_tot_dl,
                       bytes_sec_ul, bytes_sec_dl, pkts_sec_ul, pkts_sec_dl, dropped_sec_ul, dropped_sec_dl, overlimits_sec_ul, overlimits_sec_dl,
                       throughput_mbps_ul, throughput_mbps_dl, ul_status, dl_status
                FROM mb_ue_stats WHERE published = 0 ORDER BY id ASC LIMIT 50
            """)
            for row in cursor.fetchall():
                (
                    r_id,
                    ts,
                    ue_id,
                    ue_ip,
                    slice_id,
                    dnn,
                    b_tot_ul,
                    b_tot_dl,
                    p_tot_ul,
                    p_tot_dl,
                    d_tot_ul,
                    d_tot_dl,
                    o_tot_ul,
                    o_tot_dl,
                    b_sec_ul,
                    b_sec_dl,
                    p_sec_ul,
                    p_sec_dl,
                    d_sec_ul,
                    d_sec_dl,
                    o_sec_ul,
                    o_sec_dl,
                    tp_ul,
                    tp_dl,
                    ul_stat,
                    dl_stat,
                ) = row

                payload = {
                    "topic": "metrics.middlebox.ue",
                    "timestamp": ts,
                    "ue_id": ue_id,
                    "details": {
                        "ue_ip": ue_ip,
                        "slice_id": slice_id,
                        "dnn": dnn,
                    },
                    "metrics": {
                        "aggregated_tot": {
                            "bytes_sent_tot_ul": b_tot_ul,
                            "bytes_sent_tot_dl": b_tot_dl,
                            "packets_sent_tot_ul": p_tot_ul,
                            "packets_sent_tot_dl": p_tot_dl,
                            "packets_dropped_tot_ul": d_tot_ul,
                            "packets_dropped_tot_dl": d_tot_dl,
                            "overlimits_tot_ul": o_tot_ul,
                            "overlimits_tot_dl": o_tot_dl,
                        },
                        "per_second": {
                            "bytes_sent_per_second_ul": b_sec_ul,
                            "bytes_sent_per_second_dl": b_sec_dl,
                            "packets_sent_per_second_ul": p_sec_ul,
                            "packets_sent_per_second_dl": p_sec_dl,
                            "packets_dropped_per_second_ul": d_sec_ul,
                            "packets_dropped_per_second_dl": d_sec_dl,
                            "overlimits_per_second_ul": o_sec_ul,
                            "overlimits_per_second_dl": o_sec_dl,
                            "current_throughput_mbps_ul_per_second": tp_ul,
                            "current_throughput_mbps_dl_per_second": tp_dl,
                        },
                    },
                    "status": {"ul_status": ul_stat, "dl_status": dl_stat},
                }
                socket.send_string(
                    f"metrics.middlebox.ue {json.dumps(payload)}"
                )
                cursor.execute(
                    "UPDATE mb_ue_stats SET published = 1 WHERE id = ?", (r_id,)
                )

            # 5. TOPIC: metrics.middlebox.flow
            cursor.execute("""
                SELECT id, timestamp, flow_id, ue_ip, rule_name, direction, remote_ip, gbr, mbr, priority,
                       bytes_tot, pkts_tot, dropped_tot, overlimits_tot,
                       bytes_sec, pkts_sec, dropped_sec, overlimits_sec, throughput_mbps, operation, is_throttled, is_dropping
                FROM mb_flow_stats WHERE published = 0 ORDER BY id ASC LIMIT 50
            """)
            for row in cursor.fetchall():
                (
                    r_id,
                    ts,
                    f_id,
                    ue_ip,
                    r_name,
                    dir_f,
                    rem_ip,
                    gbr,
                    mbr,
                    prio,
                    b_tot,
                    p_tot,
                    d_tot,
                    o_tot,
                    b_sec,
                    p_sec,
                    d_sec,
                    o_sec,
                    tp_mbps,
                    op,
                    is_th,
                    is_dr,
                ) = row

                payload = {
                    "topic": "metrics.middlebox.flow",
                    "timestamp": ts,
                    "flow_id": f_id,
                    "details": {
                        "ue_ip": ue_ip,
                        "rule_name": r_name,
                        "direction": dir_f,
                        "remote_ip": rem_ip,
                        "qos_profile": {"gbr": gbr, "mbr": mbr, "priority": prio},
                    },
                    "metrics": {
                        "aggregated_tot": {
                            "bytes_sent_tot": b_tot,
                            "packets_sent_tot": p_tot,
                            "packets_dropped_tot": d_tot,
                            "overlimits_tot": o_tot,
                        },
                        "per_second": {
                            "bytes_sent_per_second": b_sec,
                            "packets_sent_per_second": p_sec,
                            "packets_dropped_per_second": d_sec,
                            "overlimits_per_second": o_sec,
                            "current_throughput_mbps_per_second": tp_mbps,
                        },
                    },
                    "status": {
                        "operation": op,
                        "is_throttled": bool(is_th),
                        "is_dropping": bool(is_dr),
                    },
                }
                socket.send_string(
                    f"metrics.middlebox.flow {json.dumps(payload)}"
                )
                cursor.execute(
                    "UPDATE mb_flow_stats SET published = 1 WHERE id = ?",
                    (r_id,),
                )

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Errore durante lo streaming ZeroMQ: {e}")

        time.sleep(0.5)


if __name__ == "__main__":
    start_telemetry_publisher()