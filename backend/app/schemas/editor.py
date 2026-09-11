from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

EditorSessionStatus = Literal["active", "stopped", "error"]
EditorAvailability = Literal["inactive", "active", "stopped", "error"]


class EditorStartRequest(BaseModel):
    # ``None`` => the base clone. Anything else must be a worktree of this workspace.
    worktree_id: UUID | None = None


class EditorSessionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    user_id: UUID
    # The stable alias URL, safe to display: it carries no credential.
    url: str
    # The same URL plus a short-lived grant. This is what the iframe must load, because only a
    # request carrying the grant gets the proxy's host-scoped cookie set. Minted per response.
    prime_url: str
    status: EditorSessionStatus
    worktree_id: UUID | None = None
    folder: str | None = None
    started_at: datetime
    last_active_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EditorStatusResponse(BaseModel):
    status: EditorAvailability
    session: EditorSessionResponse | None = None
