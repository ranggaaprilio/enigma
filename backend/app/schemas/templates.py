from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class WorkspaceTemplateResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str
    source_repo_url: str
    default_branch: str
    icon: str | None = None
    tags: list[str] = []
    preview_port: int | None = None
    sort_order: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
