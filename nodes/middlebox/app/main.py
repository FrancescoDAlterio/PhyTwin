import threading
from fastapi import FastAPI
from core.config import logger
from services.db_service import init_config_db, init_metrics_db
from services.tc_service import init_firewall_and_tc
from services.telemetry_service import collect_metrics_loop

from routers.ues import router as ues_router
from routers.slices import router as slices_router
from routers.pcc import router as pcc_router

app = FastAPI(
    title="5G Slice & QoS Control Plane / Middlebox",
    version="7.0"
)

# Registrazione dei router modulari
app.include_router(ues_router)
app.include_router(slices_router)
app.include_router(pcc_router)

@app.on_event("startup")
def startup_event():
    init_config_db()
    init_metrics_db()
    init_firewall_and_tc()
    threading.Thread(target=collect_metrics_loop, daemon=True).start()
    logger.info("Control Plane & Middlebox pronti.")