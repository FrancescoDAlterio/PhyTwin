import re
import sys
import time
import signal
import sqlite3
import argparse
import subprocess

DB_PATH = "/data/metrics.db"
running = True
proc = None


def signal_handler(sig, frame):
    global running, proc
    running = False
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ue_iperf_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            ue_id TEXT,
            server_ip TEXT,
            server_port INTEGER,
            client_ip TEXT,
            client_port INTEGER,
            conn_id INTEGER,
            interval TEXT,
            bytes_transferred INTEGER,
            bandwidth_mbps REAL,
            protocol TEXT DEFAULT 'TCP',
            direction TEXT DEFAULT 'UPLINK',
            jitter_ms REAL,
            lost_packets INTEGER,
            total_packets INTEGER,
            packet_loss_pct REAL,
            published INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def main():
    global proc, running
    parser = argparse.ArgumentParser()
    parser.add_argument("--ue-id", required=True)
    parser.add_argument("--target-ip", required=True)
    parser.add_argument("--target-port", type=int, default=5201)
    parser.add_argument("--protocol", default="TCP")
    parser.add_argument("--bitrate", default="10M")
    parser.add_argument("--direction", default="uplink")
    args = parser.parse_args()

    init_db()

    cmd = [
        "iperf3",
        "-c", args.target_ip,
        "-p", str(args.target_port),
        "-t", "0",
        "-i", "1",
        "--forceflush"
    ]
    if args.protocol.upper() == "UDP":
        cmd.append("-u")
    if args.bitrate:
        cmd.extend(["-b", args.bitrate])
    if args.direction.lower() == "downlink":
        cmd.append("-R")

    conn_pattern = re.compile(
        r'\[\s*(\d+)\]\s+local\s+([\d\.]+)\s+port\s+(\d+)\s+connected\s+to'
    )
    base_pattern = re.compile(
        r'\[\s*(\d+)\]\s+([\d\.]+-[\d\.]+)\s+sec\s+([\d\.]+)\s+([KMGT]?Bytes)\s+([\d\.]+)\s+Mbits/sec'
    )
    udp_pattern = re.compile(
        r'([\d\.]+)\s+ms\s+(\d+)/(\d+)\s+\(([\d\.]+)%\)'
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    conn_details = {}

    for line in iter(proc.stdout.readline, ''):
        if not running:
            break

        match_conn = conn_pattern.search(line)
        if match_conn:
            cid = int(match_conn.group(1))
            c_ip = match_conn.group(2)
            c_port = int(match_conn.group(3))
            conn_details[cid] = (c_ip, c_port)
            continue

        if "Mbits/sec" not in line or "SUM" in line:
            continue

        match_base = base_pattern.search(line)
        if not match_base:
            continue

        conn_id = int(match_base.group(1))
        interval = match_base.group(2)
        transferred_val = float(match_base.group(3))
        unit = match_base.group(4)
        bw_mbps = float(match_base.group(5))

        client_ip, client_port = conn_details.get(conn_id, ("0.0.0.0", 0))

        bytes_transferred = int(transferred_val)
        if unit == "KBytes":
            bytes_transferred = int(transferred_val * 1024)
        elif unit == "MBytes":
            bytes_transferred = int(transferred_val * 1024 * 1024)
        elif unit == "GBytes":
            bytes_transferred = int(transferred_val * 1024 * 1024 * 1024)

        jitter_ms, lost_pkts, total_pkts, loss_pct = 0.0, 0, 0, 0.0
        if args.protocol.upper() == "UDP":
            match_udp = udp_pattern.search(line)
            if match_udp:
                jitter_ms = float(match_udp.group(1))
                lost_pkts = int(match_udp.group(2))
                total_pkts = int(match_udp.group(3))
                loss_pct = float(match_udp.group(4))

        # Formato ISO 8601 UTC (es. 2026-08-27T08:20:55Z)
        iso_timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("""
                INSERT INTO ue_iperf_stats (
                    timestamp, ue_id, server_ip, server_port, client_ip, client_port,
                    conn_id, interval, bytes_transferred, bandwidth_mbps,
                    protocol, direction, jitter_ms, lost_packets, total_packets,
                    packet_loss_pct, published
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """, (
                iso_timestamp, args.ue_id, args.target_ip, args.target_port,
                client_ip, client_port, conn_id, interval, bytes_transferred, bw_mbps,
                args.protocol.upper(), args.direction.upper(), jitter_ms, lost_pkts, total_pkts, loss_pct
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            sys.stderr.write(f"DB Error: {e}\n")

    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()