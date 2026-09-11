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
    # The worktree this preview belongs to (None = base clone) and its branch.
    worktree_id: UUID | None = None
    branch: str | None = None
    # Which workspace service this preview was detected for (Epic 11); None for a session
    # detected via the legacy single-port scan, before any service row claimed the port.
    service_id: UUID | None = None

    model_config = ConfigDict(from_attributes=True)


class PreviewDetectResponse(BaseModel):
    status: PreviewAvailability
    session: PreviewSessionResponse | None = None
