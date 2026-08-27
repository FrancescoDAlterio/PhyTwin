#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

# Gestione opzionale dell'importazione di PyYAML
try:
    import yaml
except ImportError:
    yaml = None

# Percorsi di riferimento relativi alla radice di progetto (../..)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TEMP_DIR = PROJECT_ROOT / "temp"
DB_DIR = PROJECT_ROOT / "db"
UE_CONTEXT_DIR = PROJECT_ROOT / "images" / "ue"
MIDDLEBOX_API_URL = "http://localhost:8000/ues"


def log_info(msg: str):
    print(f"\033[94m[INFO]\033[0m {msg}")


def log_success(msg: str):
    print(f"\033[92m[SUCCESS]\033[0m {msg}")


def log_warn(msg: str):
    print(f"\033[93m[WARNING]\033[0m {msg}")


def log_error(msg: str):
    print(f"\033[91m[ERROR]\033[0m {msg}", file=sys.stderr)


def is_docker_compose_running() -> bool:
    """Verifica se ci sono container attivi appartenenti allo stack docker compose."""
    cmd = ["docker", "compose", "ps", "--services", "--filter", "status=running"]
    res = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True, text=True)
    return len(res.stdout.strip()) > 0


def check_ue_image_exists() -> bool:
    """Verifica se l'immagine ue-image:latest esiste su Docker."""
    res = subprocess.run(["docker", "image", "inspect", "ue-image:latest"], capture_output=True, text=True)
    return res.returncode == 0


def cleanup_procedure():
    """Step 2: Procedura di pulizia container e DB."""
    log_info("Avvio procedura di pulizia...")

    # 1. Arresto docker compose
    log_info("Esecuzione 'docker compose down'...")
    subprocess.run(["docker", "compose", "down"], cwd=PROJECT_ROOT)

    # 2. Rimozione container UE (prefisso 'phytwin_')
    log_info("Ricerca container UE con prefisso 'phytwin_'...")
    find_cmd = ["docker", "ps", "-a", "--filter", "name=phytwin_", "-q"]
    res = subprocess.run(find_cmd, capture_output=True, text=True)
    container_ids = res.stdout.strip().split()

    if container_ids:
        log_info(f"Rimozione container UE trovati: {container_ids}")
        subprocess.run(["docker", "rm", "-f"] + container_ids)
    else:
        log_info("Nessun container UE da rimuovere.")

    # 3. Rimozione file dei database (.db, .db-wal, .db-shm)
    log_info("Pulizia file dei database...")
    for target_dir in [TEMP_DIR, DB_DIR]:
        if target_dir.exists():
            for db_file in target_dir.glob("*.db*"):
                try:
                    db_file.unlink()
                    log_info(f"Rimosso: {db_file}")
                except Exception as e:
                    log_warn(f"Impossibile rimuovere {db_file}: {e}")

    log_success("Pulizia completata con successo.")


def build_procedure():
    """Step 3: Build preliminare dei container e dell'immagine UE."""
    log_info("Avvio procedura di build (--force-build)...")

    # Rebuild container docker compose
    log_info("Rebuild dello stack Docker Compose...")
    subprocess.run(["docker", "compose", "build", "--no-cache"], cwd=PROJECT_ROOT, check=True)

    # Rimozione vecchia immagine UE
    if check_ue_image_exists():
        log_info("Rimozione immagine UE precedente 'ue-image:latest'...")
        subprocess.run(["docker", "rmi", "-f", "ue-image:latest"])

    # Build nuova immagine UE
    log_info("Build nuova immagine UE da /images/ue...")
    subprocess.run(["docker", "build", "-t", "ue-image:latest", "."], cwd=UE_CONTEXT_DIR, check=True)
    log_success("Build completata.")


def start_primary_procedure():
    """Step 4: Avvio dei container primari."""
    log_info("Avvio primario dell'architettura PhyTwin...")

    # Verifica o build automatica dell'immagine UE
    if not check_ue_image_exists():
        log_info("Immagine 'ue-image:latest' non trovata. Creazione in corso...")
        subprocess.run(["docker", "build", "-t", "ue-image:latest", "."], cwd=UE_CONTEXT_DIR, check=True)

    # Avvio docker compose
    log_info("Esecuzione 'docker compose up -d'...")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=PROJECT_ROOT, check=True)
    log_success("Stack Docker Compose avviato.")


def wait_for_middlebox(timeout=30):
    """Attende l'avvio e la prontezza dell'API REST di Middlebox."""
    log_info("In attesa che il servizio Middlebox sia pronto...")
    start_t = time.time()
    while time.time() - start_t < timeout:
        try:
            req = urllib.request.Request(MIDDLEBOX_API_URL, method="GET")
            with urllib.request.urlopen(req) as resp:
                if resp.status == 200:
                    log_success("Middlebox API attiva e raggiungibile.")
                    return True
        except Exception:
            time.sleep(1)
    log_error("Timeout: Middlebox API non raggiungibile.")
    return False


def start_secondary_procedure(config_path_str: str):
    """Step 5: Avvio secondario tramite file di configurazione YAML."""
    log_info(f"Avvio secondario con configurazione: {config_path_str}")

    config_path = Path(config_path_str).resolve()
    if not config_path.exists():
        log_error(f"File di configurazione non trovato: {config_path}")
        sys.exit(1)

    if yaml is None:
        log_error("La libreria 'PyYAML' non è installata. Eseguire 'pip install pyyaml'.")
        sys.exit(1)

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        log_error(f"Errore durante il parsing del file YAML: {e}")
        sys.exit(1)

    if not isinstance(data, dict) or "ues" not in data or not isinstance(data["ues"], list):
        log_error("Formato YAML non valido. Il file deve contenere una lista sotto la chiave 'ues'.")
        sys.exit(1)

    if not wait_for_middlebox():
        sys.exit(1)

    # Creazione degli UE tramite API POST /ues
    for ue_cfg in data["ues"]:
        ue_id = ue_cfg.get("ue_id")
        spawn_container = ue_cfg.get("spawn_container", True)
        slice_ips = ue_cfg.get("slice_ips", {})

        if not ue_id or not slice_ips:
            log_warn(f"Configurazione UE saltata per parametri mancanti: {ue_cfg}")
            continue

        payload = {
            "ue_id": ue_id,
            "spawn_container": spawn_container,
            "slice_ips": slice_ips
        }

        log_info(f"Invio creazione UE '{ue_id}' a Middlebox...")
        try:
            req = urllib.request.Request(
                MIDDLEBOX_API_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                log_success(f"UE '{ue_id}' creato con successo: Container Name -> '{res_data.get('container_name')}'")
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            log_error(f"Errore API per UE '{ue_id}' (HTTP {e.code}): {err_msg}")
        except Exception as e:
            log_error(f"Errore di connessione durante la creazione di UE '{ue_id}': {e}")


def main():
    manual_text = """
================================================================================
                    MANUALE UTENTE - PHYTWIN STARTUP (v1.0)
================================================================================
Questo script gestisce il ciclo di vita e l'avvio completo dell'architettura 
PhyTwin (Middlebox, Server DNN, Telemetry Manager e nodi UE dinamici).

FLUSSO DI ESECUZIONE:
  1. Controlla se l'infrastruttura PhyTwin è già in esecuzione.
  2. Esegue il riavvio con pulizia dei container attivi e dei DB SQLite (se usato --force-restart).
  3. Rebuilda le immagini Docker da zero senza cache (se usato --force-build).
  4. Avvia lo stack Docker Compose primario.
  5. Avvia gli User Equipments (UE) leggendo la configurazione YAML (se usato --config).

ESEMPI DI UTILIZZO:
  * Avvio normale con file di configurazione UE:
      python3 phytwin_start.py --config config.example.yaml

  * Riavvio forzato con pulizia DB + rebuild completo + avvio con config:
      python3 phytwin_start.py -r -b -c config.example.yaml

  * Solo riavvio e pulizia dell'ambiente:
      python3 phytwin_start.py --force-restart
================================================================================
"""

    parser = argparse.ArgumentParser(
        prog="phytwin_start.py",
        description=manual_text,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "-r", "--force-restart",
        action="store_true",
        help="Forza il riavvio dell'infrastruttura: arresta lo stack Docker Compose attivo, rimuove tutti i container UE (phytwin_*) ed elimina i file dei database SQLite (*.db*) prima del nuovo avvio."
    )

    parser.add_argument(
        "-b", "--force-build",
        action="store_true",
        help="Forza la ricompilazione da zero (senza cache) delle immagini Docker Compose e dell'immagine dell'User Equipment (ue-image:latest)."
    )

    parser.add_argument(
        "-c", "--config",
        metavar="PATH",
        type=str,
        help="Percorso al file di configurazione YAML contenente la lista degli UE da istanziare via API REST (POST /ues) all'avvio."
    )

    args = parser.parse_args()

    # STEP 1: Controlli sullo stato di esecuzione e gestore --force-restart
    running = is_docker_compose_running()
    if running:
        if args.force_restart:
            cleanup_procedure()
        else:
            log_error("PhyTwin è già attivo. Utilizzare il flag --force-restart (-r) per riavviare o eseguire la pulizia.")
            sys.exit(1)
    else:
        # Se non in esecuzione ma presente --force-restart, esegue comunque la pulizia preventiva
        if args.force_restart:
            cleanup_procedure()

    # STEP 3: Procedura di build opzionale
    if args.force_build:
        build_procedure()

    # STEP 4: Procedura di avvio primario
    start_primary_procedure()

    # STEP 5: Procedura di avvio secondario (se specificato file --config)
    if args.config:
        start_secondary_procedure(args.config)

    log_success("Lancio completato. Architettura PhyTwin v1.0 pronta.")


if __name__ == "__main__":
    main()