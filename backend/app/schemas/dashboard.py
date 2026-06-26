from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.schemas.workspaces import WorkspaceStatus


class WorkspaceStatusCounts(BaseModel):
    creating: int = 0
    ready: int = 0
    busy: int = 0
    error: int = 0
    stopped: int = 0


class DashboardProjectItem(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    team_count: int
    workspace_count: int
    created_at: datetime


class DashboardWorkspaceItem(BaseModel):
    id: UUID
    name: str
    status: WorkspaceStatus
    project_id: UUID | None
    project_name: str | None
    team_id: UUID | None
    team_name: str | None
    created_at: datetime


class DashboardSummaryResponse(BaseModel):
    projects_total: int
    teams_total: int
    workspaces_total: int
    workspaces_by_status: WorkspaceStatusCounts
    recent_projects: list[DashboardProjectItem]
    recent_workspaces: list[DashboardWorkspaceItem]
