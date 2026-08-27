from typing import Optional
from pydantic import BaseModel, Field


class FlowDescription(BaseModel):
    remote_ip: str
    protocol: Optional[str] = "any"


class QoSParams(BaseModel):
    five_qi: int = 9
    arp: int = 1
    gbr_ul: str
    gbr_dl: str
    mbr_ul: str
    mbr_dl: str
    priority: Optional[int] = Field(default=1, description="Priorità HTB: 0 (max) - 7 (min)")


class PCCRuleReq(BaseModel):
    rule_name: str
    direction: str = "both"
    flow_description: FlowDescription
    qos: QoSParams