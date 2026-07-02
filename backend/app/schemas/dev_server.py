from typing import Literal
from uuid import UUID

from pydantic import BaseModel

DevServerStatus = Literal[
    "idle",
    "installing",
    "ready",
    "starting",
    "running",
    "stopped",
    "no-dev-task",
    "no-config",
    "error",
]


class DevServerStatusResponse(BaseModel):
    status: DevServerStatus
    port: int | None = None
    message: str | None = None
    # The worktree the dev server is running in (None = base clone) and its branch.
    worktree_id: UUID | None = None
    branch: str | None = None


class DevServerActionResponse(BaseModel):
    ok: bool
    status: DevServerStatus
    message: str | None = None
