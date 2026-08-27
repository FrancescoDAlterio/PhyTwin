# images/server/app/server_agent.py
import datetime
import os
import queue
import re
import socket
import sqlite3
import subprocess
import threading
import time

DB_PATH = os.getenv("DB_PATH", "/data/metrics.db")
LISTEN_PORT = 5201

db_queue = queue.Queue()
active_workers = {}
workers_lock = threading.Lock()

INTERVAL_PATTERN = re.compile(
    r"\[\s*(\d+)\]\s+([\d\.]+\s*-\s*[\d\.]+)\s+sec\s+([\d\.]+)\s+([KMGkmg]?[Bb]ytes?)\s+([\d\.]+)\s+([KMGkmg]?(?:bits/sec|bit/sec|b/s))"
    r"(?:\s+([\d\.]+)\s+ms\s+(\d+)/\s*(\d+)\s+\(([\d\.]+)%\))?"
)


def setup_routes():
    """Configura automaticamente le route statiche verso le reti UE."""
    gateway_ip = os.getenv("GATEWAY_IP")
    ue_networks = os.getenv("UE_NETWORKS")

    if not gateway_ip or not ue_networks:
        print("[Routing Warning] GATEWAY_IP o UE_NETWORKS non definiti. Nessuna route aggiunta.")
        return
    networks = [net.strip() for net in ue_networks.split(",") if net.strip()]
    for net in networks:
        try:
            # Usa 'replace' per rendere l'operazione idempotente senza errori se la route esiste già
            res = subprocess.run(
                ["ip", "route", "replace", net, "via", gateway_ip],
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"[Routing] Route applicata: {net} via {gateway_ip}")
        except subprocess.CalledProcessError as e:
            print(f"[Routing Error] Impossibile aggiungere la route {net} via {gateway_ip}: {e.stderr}")


class WorkerSession:

    def __init__(
        self,
        client_ip: str,
        client_port: int,
        server_ip: str,
        worker_port: int,
        proc: subprocess.Popen,
    ):
        self.client_ip = client_ip
        self.client_port = client_port
        self.server_ip = server_ip
        self.worker_port = worker_port
        self.proc = proc
        self.protocol = "TCP"
        self.bytes_c2s = 0  # Byte Client -> Server
        self.bytes_s2c = 0  # Byte Server -> Client
        self.udp_relay_sock = None
        self.lock = threading.Lock()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS server_iperf_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
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


def db_writer_thread():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")

    while True:
        try:
            item = db_queue.get()
            if item is None:
                break

            cursor.execute(
                """
                INSERT INTO server_iperf_stats
                (timestamp, server_ip, server_port, client_ip, client_port, conn_id, interval,
                 bytes_transferred, bandwidth_mbps, protocol, direction, jitter_ms,
                 lost_packets, total_packets, packet_loss_pct, published)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
                (
                    item["timestamp"],
                    item["server_ip"],
                    item["server_port"],
                    item["client_ip"],
                    item["client_port"],
                    item["conn_id"],
                    item["interval"],
                    item["bytes_transferred"],
                    item["bandwidth_mbps"],
                    item["protocol"],
                    item["direction"],
                    item["jitter_ms"],
                    item["lost_packets"],
                    item["total_packets"],
                    item["packet_loss_pct"],
                ),
            )
            conn.commit()
            db_queue.task_done()
        except Exception as e:
            print(f"[DB Writer Error] {e}")


def to_bytes(val: float, unit: str) -> int:
    u = unit.upper()
    if "K" in u:
        return int(val * 1024)
    if "M" in u:
        return int(val * 1024 * 1024)
    if "G" in u:
        return int(val * 1024 * 1024 * 1024)
    return int(val)


def to_mbps(val: float, unit: str) -> float:
    u = unit.upper()
    if "K" in u:
        return round(val / 1000.0, 2)
    if "M" in u:
        return round(val, 2)
    if "G" in u:
        return round(val * 1000.0, 2)
    return round(val / 1_000_000.0, 2)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def monitor_worker_stdout(session: WorkerSession):
    proc = session.proc
    client_ip = session.client_ip

    for line in iter(proc.stdout.readline, ""):
        line = line.strip()
        if not line:
            continue

        match = INTERVAL_PATTERN.search(line)
        if match:
            conn_id = int(match.group(1))
            interval_str = match.group(2).replace(" ", "")
            bytes_val = float(match.group(3))
            bytes_unit = match.group(4)
            bw_val = float(match.group(5))
            bw_unit = match.group(6)

            jitter_ms = float(match.group(7)) if match.group(7) else None
            lost_pkts = int(match.group(8)) if match.group(8) else None
            total_pkts = int(match.group(9)) if match.group(9) else None
            loss_pct = float(match.group(10)) if match.group(10) else None

            with session.lock:
                if jitter_ms is not None:
                    session.protocol = "UDP"

                protocol = session.protocol
                # Determinazione direzione con terminologia telco
                if session.bytes_s2c > session.bytes_c2s and session.bytes_s2c > 5000:
                    direction = "DOWNLINK"
                else:
                    direction = "UPLINK"

                s_ip = session.server_ip
                c_port = session.client_port

            bytes_tx = to_bytes(bytes_val, bytes_unit)
            mbps = to_mbps(bw_val, bw_unit)
            dt_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            db_queue.put({
                "timestamp": dt_str,
                "server_ip": s_ip,
                "server_port": LISTEN_PORT,
                "client_ip": client_ip,
                "client_port": c_port,
                "conn_id": conn_id,
                "interval": interval_str,
                "bytes_transferred": bytes_tx,
                "bandwidth_mbps": mbps,
                "protocol": protocol,
                "direction": direction,
                "jitter_ms": jitter_ms,
                "lost_packets": lost_pkts,
                "total_packets": total_pkts,
                "packet_loss_pct": loss_pct,
            })

    # Pulizia sessione alla terminazione del worker iperf3
    proc.wait()
    if session.udp_relay_sock:
        try:
            session.udp_relay_sock.close()
        except Exception:
            pass
    with workers_lock:
        if (
            client_ip in active_workers
            and active_workers[client_ip] is session
        ):
            del active_workers[client_ip]


def get_or_create_session(
    client_ip: str, client_port: int, server_ip: str
) -> WorkerSession:
    with workers_lock:
        existing = active_workers.get(client_ip)
        if existing and existing.proc.poll() is None:
            if client_port:
                existing.client_port = client_port
            return existing

        worker_port = find_free_port()
        cmd = [
            "stdbuf",
            "-oL",
            "-eL",
            "iperf3",
            "-s",
            "-p",
            str(worker_port),
            "-1",
            "-i",
            "1",
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

        session = WorkerSession(
            client_ip, client_port, server_ip, worker_port, proc
        )
        active_workers[client_ip] = session

        threading.Thread(
            target=monitor_worker_stdout, args=(session,), daemon=True
        ).start()
        time.sleep(0.05)
        return session


def forward_tcp_stream(
    source_sock, dest_sock, session: WorkerSession, is_c2s: bool
):
    try:
        while True:
            data = source_sock.recv(4096)
            if not data:
                break
            dest_sock.sendall(data)
            with session.lock:
                if is_c2s:
                    session.bytes_c2s += len(data)
                else:
                    session.bytes_s2c += len(data)
    except Exception:
        pass
    finally:
        source_sock.close()
        dest_sock.close()


def handle_tcp_client(client_sock, client_addr):
    client_ip, client_port = client_addr
    server_ip = client_sock.getsockname()[0]

    session = get_or_create_session(client_ip, client_port, server_ip)

    try:
        worker_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        worker_sock.connect(("127.0.0.1", session.worker_port))

        t1 = threading.Thread(
            target=forward_tcp_stream,
            args=(client_sock, worker_sock, session, True),
            daemon=True,
        )
        t2 = threading.Thread(
            target=forward_tcp_stream,
            args=(worker_sock, client_sock, session, False),
            daemon=True,
        )
        t1.start()
        t2.start()
    except Exception as e:
        print(f"[TCP Proxy Error] {e}")
        client_sock.close()


def start_tcp_master():
    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind(("0.0.0.0", LISTEN_PORT))
    tcp_sock.listen(128)
    print(f"[Master TCP] In ascolto sulla porta {LISTEN_PORT}...")

    while True:
        client_sock, client_addr = tcp_sock.accept()
        threading.Thread(
            target=handle_tcp_client,
            args=(client_sock, client_addr),
            daemon=True,
        ).start()


def start_udp_master():
    udp_master_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_master_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_master_sock.bind(("0.0.0.0", LISTEN_PORT))
    print(f"[Master UDP] In ascolto sulla porta {LISTEN_PORT}...")

    while True:
        try:
            data, addr = udp_master_sock.recvfrom(65535)
            client_ip, client_port = addr
            server_ip = udp_master_sock.getsockname()[0]

            session = get_or_create_session(client_ip, client_port, server_ip)

            with session.lock:
                session.protocol = "UDP"
                session.bytes_c2s += len(data)

                if session.udp_relay_sock is None:
                    relay_sock = socket.socket(
                        socket.AF_INET, socket.SOCK_DGRAM
                    )
                    session.udp_relay_sock = relay_sock

                    # Thread per intercettare i datagrammi di ritorno del worker (UDP Reverse / Summary)
                    def udp_back_relay():
                        while True:
                            try:
                                resp, _ = relay_sock.recvfrom(65535)
                                if not resp:
                                    break
                                udp_master_sock.sendto(resp, (client_ip, client_port))
                                with session.lock:
                                    session.bytes_s2c += len(resp)
                            except Exception:
                                break

                    threading.Thread(target=udp_back_relay, daemon=True).start()

            # Inoltro del datagramma al worker iperf3 su localhost
            session.udp_relay_sock.sendto(
                data, ("127.0.0.1", session.worker_port)
            )

        except Exception as e:
            print(f"[Master UDP Error] {e}")


def start_server():
    setup_routes()
    init_db()
    threading.Thread(target=db_writer_thread, daemon=True).start()
    threading.Thread(target=start_tcp_master, daemon=True).start()
    threading.Thread(target=start_udp_master, daemon=True).start()

    print("Server iPerf3 Collector attivo in tempo reale.")
    while True:
        time.sleep(1)


if __name__ == "__main__":
    start_server()