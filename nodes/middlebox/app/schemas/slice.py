from typing import Optional
from pydantic import BaseModel, Field


class AMBR(BaseModel):
    ul_br: str
    dl_br: str
    priority: Optional[int] = Field(default=7, description="Priorità HTB AMBR")


class SliceConfigReq(BaseModel):
    slice_id: str
    dnn: str
    ue_ip: str
    ambr: AMBR