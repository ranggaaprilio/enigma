from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

PreviewSessionStatus = Literal["active", "stopped", "error"]
PreviewAvailability = Literal["inactive", "active", "stopped", "error"]


class PreviewSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    port: int
    public_url: str
    status: PreviewSessionStatus
    created_at: datetime
    last_seen_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PreviewDetectResponse(BaseModel):
    status: PreviewAvailability
    session: PreviewSessionResponse | None = None
