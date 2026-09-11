from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TeamMember, Workspace
from app.services.workspaces import WorkspaceNotFoundError

_TEAM_ROLE_TO_WORKSPACE_ROLE = {
    "lead": "team_lead",
    "member": "team_member",
    "viewer": "team_viewer",
}


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    TEAM_LEAD = "team_lead"
    TEAM_MEMBER = "team_member"
    TEAM_VIEWER = "team_viewer"


class WorkspaceAccessDeniedError(Exception):
    """Raised when a user has some access to a workspace but not enough to drive it."""


def can_drive(role: WorkspaceRole) -> bool:
    return role in (WorkspaceRole.OWNER, WorkspaceRole.TEAM_LEAD, WorkspaceRole.TEAM_MEMBER)


async def resolve_workspace_role(
    session: AsyncSession, workspace: Workspace, user_id: UUID
) -> WorkspaceRole | None:
    if workspace.owner_id == user_id:
        return WorkspaceRole.OWNER
    if workspace.team_id is None:
        return None
    result = await session.execute(
        select(TeamMember.role).where(
            TeamMember.team_id == workspace.team_id,
            TeamMember.user_id == user_id,
        )
    )
    team_role = result.scalar_one_or_none()
    if team_role is None:
        return None
    return WorkspaceRole(_TEAM_ROLE_TO_WORKSPACE_ROLE[team_role])


async def require_workspace_access(
    session: AsyncSession,
    workspace_id: UUID,
    user_id: UUID,
    *,
    require_drive: bool = False,
) -> tuple[Workspace, WorkspaceRole]:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise WorkspaceNotFoundError
    role = await resolve_workspace_role(session, workspace, user_id)
    if role is None:
        raise WorkspaceNotFoundError
    if require_drive and not can_drive(role):
        raise WorkspaceAccessDeniedError
    return workspace, role
