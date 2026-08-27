from pydantic import BaseModel, Field
from typing import Dict, Optional


class UECreateReq(BaseModel):
    ue_id: str = Field(
        ..., 
        example="ue1", 
        description="Identificativo univoco dell'UE"
    )
    slice_ips: Dict[str, str] = Field(
        ..., 
        example={"eMBB": "10.0.1.100", "URLLC": "10.0.2.100"}, 
        description="Mappatura tra nome o chiave della slice e l'indirizzo IP assegnato all'UE"
    )
    spawn_container: bool = Field(
        True, 
        description="Se True, istanzia fisicamente il container Docker dedicato all'UE"
    )


class UEIperfStartReq(BaseModel):
    target_ip: str = Field(
        ..., 
        example="10.0.1.1", 
        description="Indirizzo IP del server iperf3 di destinazione"
    )
    target_port: int = Field(
        5201, 
        example=5201, 
        description="Porta di ascolto del server iperf3"
    )
    protocol: str = Field(
        "TCP", 
        pattern="^(TCP|UDP|tcp|udp)$", 
        description="Protocollo di trasporto da utilizzare (TCP o UDP)"
    )
    bitrate: Optional[str] = Field(
        "10M", 
        example="20M", 
        description="Banda richiesta per il test (es. '10M', '1G'). Rilevante soprattutto per UDP"
    )
    direction: str = Field(
        "uplink", 
        pattern="^(uplink|downlink)$", 
        description="Direzione del flusso dati: 'uplink' (default) o 'downlink' (abilita il flag -R su iperf3)"
    )