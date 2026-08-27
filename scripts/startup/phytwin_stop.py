#!/usr/bin/env python3
import argparse
import sys
import subprocess
from pathlib import Path

# Percorsi di riferimento relativi alla radice di progetto (../..)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
TEMP_DIR = PROJECT_ROOT / "temp"
DB_DIR = PROJECT_ROOT / "db"


def log_info(msg: str):
    print(f"\033[94m[INFO]\033[0m {msg}")


def log_success(msg: str):
    print(f"\033[92m[SUCCESS]\033[0m {msg}")


def log_warn(msg: str):
    print(f"\033[93m[WARNING]\033[0m {msg}")


def log_error(msg: str):
    print(f"\033[91m[ERROR]\033[0m {msg}", file=sys.stderr)


def stop_docker_compose():
    """Arresta e rimuove lo stack Docker Compose primario."""
    log_info("Esecuzione 'docker compose down' per fermare i container primari...")
    res = subprocess.run(["docker", "compose", "down"], cwd=PROJECT_ROOT)
    if res.returncode == 0:
        log_success("Stack Docker Compose arrestato con successo.")
    else:
        log_warn("Si è verificato un avviso durante l'esecuzione di 'docker compose down'.")


def remove_ue_containers():
    """Ricerca e rimuove forzatamente tutti i container UE attivi/stoppati (prefisso 'phytwin_')."""
    log_info("Ricerca container UE con prefisso 'phytwin_'...")
    find_cmd = ["docker", "ps", "-a", "--filter", "name=phytwin_", "-q"]
    res = subprocess.run(find_cmd, capture_output=True, text=True)
    container_ids = res.stdout.strip().split()

    if container_ids:
        log_info(f"Rimozione forzata container UE trovati ({len(container_ids)}): {container_ids}")
        subprocess.run(["docker", "rm", "-f"] + container_ids)
        log_success("Tutti i container UE sono stati rimossi.")
    else:
        log_info("Nessun container UE ('phytwin_*') trovato.")


def cleanup_databases():
    """Cancella i file dei database SQLite (.db, .db-wal, .db-shm)."""
    log_info("Pulizia file dei database SQLite (*.db*)...")
    removed_count = 0
    for target_dir in [TEMP_DIR, DB_DIR]:
        if target_dir.exists():
            for db_file in target_dir.glob("*.db*"):
                try:
                    db_file.unlink()
                    log_info(f"Rimosso: {db_file}")
                    removed_count += 1
                except Exception as e:
                    log_warn(f"Impossibile rimuovere {db_file}: {e}")
    if removed_count == 0:
        log_info("Nessun file di database trovato.")
    else:
        log_success(f"Rimossi {removed_count} file di database.")


def main():
    manual_text = """
================================================================================
                    MANUALE UTENTE - PHYTWIN STOP (v1.0)
================================================================================
Questo script spegne e rimuove completamente tutti i componenti attivi 
dell'architettura PhyTwin.

OPERAZIONI ESEGUITE:
  1. Esegue 'docker compose down' per fermare e rimuovere lo stack primario 
     (Middlebox, DNN Server, Telemetry Manager).
  2. Ricerca e rimuove forzatamente tutti i container UE generati dinamici
     (con prefisso 'phytwin_').
  3. Opzionalmente rimuove i file dei database SQLite se viene specificato 
     il flag --clean-db (-d).

ESEMPI DI UTILIZZO:
  * Arresto standard dei container (mantiene i file dei DB su disco):
      python3 phytwin_stop.py

  * Arresto dei container e cancellazione contestuale dei database:
      python3 phytwin_stop.py --clean-db
================================================================================
"""

    parser = argparse.ArgumentParser(
        prog="phytwin_stop.py",
        description=manual_text,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "-d", "--clean-db",
        action="store_true",
        help="Cancella anche i file dei database SQLite (*.db*) situati in /db e /temp."
    )

    args = parser.parse_args()

    log_info("Avvio procedura di arresto dell'infrastruttura PhyTwin...")
    stop_docker_compose()
    remove_ue_containers()

    if args.clean_db:
        cleanup_databases()

    log_success("Arresto completo dell'architettura PhyTwin effettuato.")


if __name__ == "__main__":
    main()