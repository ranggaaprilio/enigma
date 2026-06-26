from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import User
from app.schemas.projects import (
    AddProjectMemberRequest,
    ProjectCreateRequest,
    ProjectMemberResponse,
    ProjectResponse,
    ProjectUpdateRequest,
    UpdateProjectMemberRoleRequest,
)
from app.services.projects import (
    ProjectMemberNotFoundError,
    ProjectNotFoundError,
    ProjectPermissionError,
    SqlAlchemyProjectService,
)

router = APIRouter(prefix="/projects", tags=["projects"])

DB_SESSION_DEP = Depends(get_db_session)


async def get_project_service(session: AsyncSession = DB_SESSION_DEP) -> SqlAlchemyProjectService:
    return SqlAlchemyProjectService(session)


PROJECT_SERVICE_DEP = Depends(get_project_service)


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> ProjectResponse:
    project = await svc.create(
        name=payload.name,
        created_by_id=current_user.id,
        description=payload.description,
    )
    return ProjectResponse.model_validate(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> list[ProjectResponse]:
    projects = await svc.list_accessible(current_user.id)
    return [ProjectResponse.model_validate(p) for p in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> ProjectResponse:
    try:
        project = await svc.get_accessible(project_id, current_user.id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return ProjectResponse.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdateRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> ProjectResponse:
    try:
        project = await svc.update(
            project_id=project_id,
            user_id=current_user.id,
            name=payload.name,
            description=payload.description,
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    except ProjectPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return ProjectResponse.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> None:
    try:
        await svc.delete(project_id, current_user.id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    except ProjectPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")


@router.get("/{project_id}/members", response_model=list[ProjectMemberResponse])
async def list_project_members(
    project_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> list[ProjectMemberResponse]:
    try:
        members = await svc.list_members(project_id, current_user.id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return [ProjectMemberResponse.model_validate(m) for m in members]


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_project_member(
    project_id: UUID,
    payload: AddProjectMemberRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> ProjectMemberResponse:
    try:
        member = await svc.add_member(
            project_id=project_id,
            actor_id=current_user.id,
            target_user_id=payload.user_id,
            role=payload.role,
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    except ProjectPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    return ProjectMemberResponse.model_validate(member)


@router.patch("/{project_id}/members/{user_id}", response_model=ProjectMemberResponse)
async def update_project_member_role(
    project_id: UUID,
    user_id: UUID,
    payload: UpdateProjectMemberRoleRequest,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> ProjectMemberResponse:
    try:
        member = await svc.update_member_role(
            project_id=project_id,
            actor_id=current_user.id,
            target_user_id=user_id,
            role=payload.role,
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    except ProjectPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    except ProjectMemberNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return ProjectMemberResponse.model_validate(member)


@router.delete("/{project_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project_member(
    project_id: UUID,
    user_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyProjectService = PROJECT_SERVICE_DEP,
) -> None:
    try:
        await svc.remove_member(
            project_id=project_id,
            actor_id=current_user.id,
            target_user_id=user_id,
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    except ProjectPermissionError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    except ProjectMemberNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
