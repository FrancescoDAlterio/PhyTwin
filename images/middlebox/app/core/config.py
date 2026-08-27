import os
import yaml
import logging
import docker
from typing import Dict

# Configurazione Logging globale
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MiddleboxEngine")

# Percorsi di configurazione e DB
CONFIG_PATH = os.getenv("CONFIG_PATH", "./configs/config.yaml")
CONFIG_DB_PATH = os.getenv("CONFIG_DB_PATH", "/data/configs.db")
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", "/data/metrics.db")

# Caricamento file YAML
try:
    with open(CONFIG_PATH, "r") as f:
        CONFIG = yaml.safe_load(f)
    logger.info(f"Configurazione caricata da {CONFIG_PATH}")
except Exception as e:
    logger.error(f"Errore caricamento file di configurazione: {e}")
    raise RuntimeError(f"Errore config.yaml: {e}")

# Client Docker
#TODO verificare se stoppare esecuzione
try:
    docker_client = docker.from_env()
except Exception as e:
    logger.warning(f"Impossibile inizializzare il client Docker: {e}")
    docker_client = None

# Mappe runtime e contatori condivisi
SLICES_MAP: Dict[str, dict] = {}
DNNS_MAP: Dict[str, dict] = {}