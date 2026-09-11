from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

WorkspaceServiceStatus = Literal[
    "installing", "starting", "running", "stopped", "exited", "error"
]
# Epic 13: readiness, orthogonal to WorkspaceServiceStatus -- a service can be "running" (port
# bound) and "unhealthy" (failing its health check) at the same time.
WorkspaceServiceHealth = Literal["unknown", "starting", "healthy", "unhealthy"]


class WorkspaceServiceResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    # None => the built-in mise `dev` service (no registry command owns it).
    custom_command_id: UUID | None = None
    name: str
    role: str
    kind: str
    command: str | None = None
    port: int | None = None
    status: WorkspaceServiceStatus
    desired: bool
    worktree_id: UUID | None = None
    branch: str | None = None
    last_error: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    last_seen_at: datetime | None = None
    # Populated once a service is `running` and has a confirmed port (Epic 11 preview
    # reconciliation); None otherwise.
    preview_session_id: UUID | None = None
    public_url: str | None = None
    # Epic 13: health/readiness + entry-point marker. health_path/is_entry_point are None/False
    # for the built-in mise service, which has no user-editable command row to carry them on.
    health_status: WorkspaceServiceHealth = "unknown"
    health_checked_at: datetime | None = None
    health_path: str | None = None
    is_entry_point: bool = False

    model_config = ConfigDict(from_attributes=True)


class WorkspaceEnvironmentState(BaseModel):
    """The workspace-wide mise environment (install/mise-trust), separate from any one
    service's run status — mirrors the pre-Epic-11 `/dev/setup` concept."""

    status: str
    message: str | None = None


class WorkspaceServicesResponse(BaseModel):
    services: list[WorkspaceServiceResponse]
    environment: WorkspaceEnvironmentState
    limit: int


class ServiceActionRequest(BaseModel):
    worktree_id: UUID | None = None


class ServiceActionResponse(BaseModel):
    service: WorkspaceServiceResponse
    warning: str | None = None


class ServiceLogsResponse(BaseModel):
    content: str
