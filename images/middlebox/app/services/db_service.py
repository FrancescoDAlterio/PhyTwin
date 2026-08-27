import os
import sqlite3
from core.config import CONFIG_DB_PATH, METRICS_DB_PATH


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
            ue_id TEXT,
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