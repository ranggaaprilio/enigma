from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

EditorSessionStatus = Literal["active", "stopped", "error"]
EditorAvailability = Literal["inactive", "active", "stopped", "error"]


class EditorSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    url: str
    status: EditorSessionStatus
    started_at: datetime
    last_active_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EditorStatusResponse(BaseModel):
    status: EditorAvailability
    session: EditorSessionResponse | None = None
