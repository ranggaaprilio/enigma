from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    workspace_id: UUID
    type: str
    title: str
    message: str
    read_at: datetime | None
    created_at: datetime


class AuditLogResponse(BaseModel):
    id: UUID
    user_id: UUID
    workspace_id: UUID | None
    action: str
    target_type: str
    target_id: str
    metadata: dict
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
