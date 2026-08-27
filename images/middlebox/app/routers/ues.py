import os
import time
import socket
import docker
from fastapi import APIRouter, HTTPException, status
from core.config import logger, docker_client, SLICES_MAP, DNNS_MAP
from schemas.ue import UECreateReq, UEIperfStartReq
from services.db_service import get_config_db
from services.tc_service import find_slice_info, teardown_slice_config_network

router = APIRouter(prefix="/ues", tags=["UEs"])

IMAGE_NAME = "ue-image:latest"
UE_BUILD_CONTEXT = "/app/ue_docker_context"


def _get_shared_volume_mount():
    """Recupera la sorgente del volume /data montato su Middlebox da condividere con gli UE."""
    try:
        hostname = socket.gethostname()
        mb_container = docker_client.containers.get(hostname)
        for m in mb_container.attrs.get("Mounts", []):
            if m.get("Destination") == "/data":
                return {m["Source"]: {"bind": "/data", "mode": "rw"}}
    except Exception:
        pass
    return {"/data": {"bind": "/data", "mode": "rw"}}


def ensure_ue_image():
    """Verifica la presenza di ue-image:latest; se assente la builda dal Dockerfile su disco."""
    try:
        docker_client.images.get(IMAGE_NAME)
    except docker.errors.ImageNotFound:
        logger.info(f"Immagine '{IMAGE_NAME}' non trovata. Avvio build da '{UE_BUILD_CONTEXT}'...")
        if not os.path.exists(os.path.join(UE_BUILD_CONTEXT, "Dockerfile")):
            raise HTTPException(
                status_code=500,
                detail=f"Dockerfile non trovato in '{UE_BUILD_CONTEXT}'. Verificare i volumi in docker-compose.yaml."
            )
        docker_client.images.build(path=UE_BUILD_CONTEXT, tag=IMAGE_NAME, rm=True)
        logger.info(f"Build completata con successo: {IMAGE_NAME}")


@router.post("", status_code=status.HTTP_201_CREATED)
def create_ue(req: UECreateReq):
    if not req.slice_ips or len(req.slice_ips) == 0:
        raise HTTPException(
            status_code=400,
            detail="Impossibile creare l'UE: è obbligatorio specificare almeno un indirizzo IP per una slice in 'slice_ips'."
        )

    # Aggiunge il prefisso 'phytwin_' all'ID dell'UE se non già presente
    ue_id = req.ue_id if req.ue_id.startswith("phytwin_") else f"phytwin_{req.ue_id}"

    conn = get_config_db()
    c = conn.cursor()
    c.execute("SELECT ue_id FROM ues WHERE ue_id = ?", (ue_id,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"UE '{ue_id}' già esistente.")

    route_cmds = []
    slices_to_connect = []

    for s_key_or_name, ip_val in req.slice_ips.items():
        matched_slice = find_slice_info(s_key_or_name)
        if not matched_slice:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"Slice/Rete '{s_key_or_name}' non trovata. Slice valide: {list(SLICES_MAP.keys())}"
            )
        slices_to_connect.append((matched_slice, ip_val))
        gateway_ip = matched_slice.get("gateway_ip")
        dnn_ref = matched_slice.get("dnn")
        dnn_info = DNNS_MAP.get(dnn_ref) if dnn_ref else None

        if gateway_ip and dnn_info and dnn_info.get("subnet"):
            route_cmds.append(f"ip route replace {dnn_info['subnet']} via {gateway_ip}")

    container_id = None
    if req.spawn_container:
        if not docker_client:
            conn.close()
            raise HTTPException(status_code=500, detail="Client Docker non disponibile.")
        try:
            ensure_ue_image()
            volumes_config = _get_shared_volume_mount()

            container = docker_client.containers.run(
                image=IMAGE_NAME,
                name=ue_id,
                detach=True,
                tty=True,
                init=True,  # Attiva l'init process interno al container
                cap_add=["NET_ADMIN"],
                volumes=volumes_config
            )
            container_id = container.id

            for matched_slice, ip_val in slices_to_connect:
                docker_net = docker_client.networks.get(matched_slice["network_name"])
                docker_net.connect(container, ipv4_address=ip_val)

            for cmd in route_cmds:
                container.exec_run(cmd)

            try:
                docker_client.networks.get("bridge").disconnect(container)
            except Exception:
                pass

        except Exception as e:
            conn.close()
            logger.error(f"Errore durante lo spawn Docker: {e}")
            raise HTTPException(status_code=500, detail=f"Docker spawn error: {e}")

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c.execute("INSERT INTO ues (ue_id, container_id, created_at) VALUES (?, ?, ?)", (ue_id, container_id, now_iso))
    conn.commit()
    conn.close()

    return {
        "status": "SUCCESS",
        "ue_id": ue_id,
        "container_name": ue_id,
        "container_id": container_id,
        "created_at": now_iso
    }


@router.post("/{ue_id}/iperf/start")
def start_ue_iperf(ue_id: str, req: UEIperfStartReq):
    if not docker_client:
        raise HTTPException(status_code=500, detail="Client Docker non disponibile.")

    ue_id = ue_id if ue_id.startswith("phytwin_") else f"phytwin_{ue_id}"

    try:
        container = docker_client.containers.get(ue_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Container UE '{ue_id}' non trovato.")

    cmd = (
        f"python3 /usr/local/bin/ue_agent.py "
        f"--ue-id {ue_id} "
        f"--target-ip {req.target_ip} "
        f"--target-port {req.target_port} "
        f"--protocol {req.protocol} "
        f"--bitrate {req.bitrate} "
        f"--direction {req.direction}"
    )

    exec_res = container.exec_run(f"bash -c '{cmd} > /dev/null 2>&1 & echo $!'")
    if exec_res.exit_code != 0:
        raise HTTPException(status_code=500, detail="Errore avvio agente iperf3.")

    pid = exec_res.output.decode("utf-8").strip()
    return {"status": "SUCCESS", "message": f"Test iperf3 avviato su UE '{ue_id}'", "pid": pid}


@router.post("/{ue_id}/iperf/stop")
def stop_ue_iperf(ue_id: str):
    if not docker_client:
        raise HTTPException(status_code=500, detail="Client Docker non disponibile.")

    ue_id = ue_id if ue_id.startswith("phytwin_") else f"phytwin_{ue_id}"

    try:
        container = docker_client.containers.get(ue_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Container UE '{ue_id}' non trovato.")

    # Invio SIGTERM (15) per consentire una chiusura graziosa di socket e processo
    container.exec_run("pkill -15 -f ue_agent.py")
    container.exec_run("pkill -15 iperf3")

    logger.info(f"Stoppato iperf3 su UE '{ue_id}'")
    return {"status": "SUCCESS", "message": f"Test iperf3 fermato su UE '{ue_id}'."}


@router.get("")
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


@router.get("/{ue_id}")
def get_ue(ue_id: str):
    ue_id = ue_id if ue_id.startswith("phytwin_") else f"phytwin_{ue_id}"
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


@router.delete("/{ue_id}")
def delete_ue(ue_id: str):
    ue_id = ue_id if ue_id.startswith("phytwin_") else f"phytwin_{ue_id}"
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

    if docker_client:
        try:
            container = docker_client.containers.get(ue_id)
            container.stop()
            container.remove(force=True)
        except Exception:
            pass

    return {"status": "SUCCESS", "message": f"UE '{ue_id}' e tutte le sue risorse sono state rimosse."}