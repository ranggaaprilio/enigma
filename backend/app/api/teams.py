from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.audit_support import AUDIT_LOG_SERVICE_DEP, record_audit_safely
from app.api.auth import CURRENT_USER_DEP
from app.api.workspace_support import workspace_response, workspace_responses
from app.api.workspaces import WORKSPACE_CREATE_ERRORS, workspace_create_error
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User
from app.schemas.projects import (
    AddTeamMemberRequest,
    TeamCreateRequest,
    TeamMemberResponse,
    TeamResponse,
    TeamUpdateRequest,
    UpdateTeamMemberRoleRequest,
)
from app.schemas.workspaces import WorkspaceCreateRequest, WorkspaceResponse
from app.services.audit import AuditLogService
from app.services.daytona_runtime import get_daytona_runtime
from app.services.projects import (
    ProjectNotFoundError,
    SqlAlchemyProjectService,
    TeamMemberNotFoundError,
    TeamNotFoundError,
    TeamPermissionError,
)
from app.services.workspaces import (
    SqlAlchemyWorkspaceService,
    WorkspaceNotFoundError,
    WorkspaceRuntimeError,
    WorkspaceSecretCipher,
)

router = APIRouter(prefix="/projects/{project_id}/teams", tags=["teams"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


async def get_project_service(session: AsyncSession = DB_SESSION_DEP) -> SqlAlchemyProjectService:
    return SqlAlchemyProjectService(session)


PROJECT_SERVICE_DEP = Depends(get_project_service)


# ── teams ─────────────────────────────────────────────────────────────────────


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    project_id: UUID,
    payload: TeamCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> TeamResponse:
    try:
        team = await svc.create_team(
            project_id=project_id,
            name=payload.name,
            created_by_id=current_user.id,
            description=payload.description,
        )
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from None
    except TeamPermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        ) from None
    return TeamResponse.model_validate(team)


@router.get("", response_model=list[TeamResponse])
async def list_teams(
    project_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> list[TeamResponse]:
    try:
        teams = await svc.list_teams(project_id, current_user.id)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from None
    return [TeamResponse.model_validate(t) for t in teams]


@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
    project_id: UUID,
    team_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> TeamResponse:
    try:
        team = await svc.get_team(team_id, current_user.id)
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    if team.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return TeamResponse.model_validate(team)


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team(
    project_id: UUID,
    team_id: UUID,
    payload: TeamUpdateRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> TeamResponse:
    try:
        team = await svc.update_team(
            team_id=team_id,
            user_id=current_user.id,
            name=payload.name,
            description=payload.description,
        )
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    except TeamPermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        ) from None
    return TeamResponse.model_validate(team)


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    project_id: UUID,
    team_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> None:
    try:
        await svc.delete_team(team_id, current_user.id)
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    except (TeamPermissionError, ProjectNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        ) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=None,
        action="team_delete",
        target_type="team",
        target_id=team_id,
        metadata={"project_id": project_id},
    )


# ── team members ──────────────────────────────────────────────────────────────


@router.get("/{team_id}/members", response_model=list[TeamMemberResponse])
async def list_team_members(
    project_id: UUID,
    team_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> list[TeamMemberResponse]:
    try:
        members = await svc.list_team_members(team_id, current_user.id)
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    return [TeamMemberResponse.model_validate(m) for m in members]


@router.post(
    "/{team_id}/members",
    response_model=TeamMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_team_member(
    project_id: UUID,
    team_id: UUID,
    payload: AddTeamMemberRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> TeamMemberResponse:
    try:
        member = await svc.add_team_member(
            team_id=team_id,
            actor_id=current_user.id,
            target_user_id=payload.user_id,
            role=payload.role,
        )
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    except TeamPermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        ) from None
    return TeamMemberResponse.model_validate(member)


@router.patch("/{team_id}/members/{user_id}", response_model=TeamMemberResponse)
async def update_team_member_role(
    project_id: UUID,
    team_id: UUID,
    user_id: UUID,
    payload: UpdateTeamMemberRoleRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> TeamMemberResponse:
    try:
        member = await svc.update_team_member_role(
            team_id=team_id,
            actor_id=current_user.id,
            target_user_id=user_id,
            role=payload.role,
        )
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    except TeamPermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        ) from None
    except TeamMemberNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        ) from None
    return TeamMemberResponse.model_validate(member)


@router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    project_id: UUID,
    team_id: UUID,
    user_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> None:
    try:
        await svc.remove_team_member(
            team_id=team_id,
            actor_id=current_user.id,
            target_user_id=user_id,
        )
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    except TeamPermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        ) from None
    except TeamMemberNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        ) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=None,
        action="team_member_remove",
        target_type="team_member",
        target_id=user_id,
        metadata={"project_id": project_id, "team_id": team_id},
    )


# ── team workspaces ───────────────────────────────────────────────────────────


async def get_workspace_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> SqlAlchemyWorkspaceService:
    return SqlAlchemyWorkspaceService(
        session=session,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=get_daytona_runtime(),
    )


WORKSPACE_SERVICE_DEP = Depends(get_workspace_service)


@router.get("/{team_id}/workspaces", response_model=list[WorkspaceResponse])
async def list_team_workspaces(
    project_id: UUID,
    team_id: UUID,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    workspace_svc: SqlAlchemyWorkspaceService = WORKSPACE_SERVICE_DEP,
) -> list[WorkspaceResponse]:
    try:
        workspaces = await workspace_svc.list_for_team(team_id, current_user.id)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None
    return await workspace_responses(session, workspaces, current_user.id)


@router.post(
    "/{team_id}/workspaces",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_team_workspace(
    project_id: UUID,
    team_id: UUID,
    payload: WorkspaceCreateRequest,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
    workspace_svc: SqlAlchemyWorkspaceService = WORKSPACE_SERVICE_DEP,
) -> WorkspaceResponse:
    try:
        await svc.get_team(team_id, current_user.id)
    except TeamNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        ) from None

    try:
        workspace = await workspace_svc.create(
            owner_id=current_user.id,
            name=payload.name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
            snapshot=payload.snapshot,
            team_id=team_id,
            source=payload.source,
            template_id=payload.template_id,
        )
    except WorkspaceRuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Workspace runtime error: {exc}",
        ) from None
    except WORKSPACE_CREATE_ERRORS as exc:
        raise workspace_create_error(exc) from None
    return await workspace_response(session, workspace, current_user.id)
