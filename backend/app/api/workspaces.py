from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User, Workspace, WorkspaceEnvVar
from app.schemas.workspaces import (
    DeleteResponse,
    WorkspaceCreateRequest,
    WorkspaceEnvVarResponse,
    WorkspaceEnvVarUpsertRequest,
    WorkspaceResponse,
    WorkspaceStatusResponse,
    WorkspaceUpdateRequest,
)
from app.services.workspaces import (
    InvalidEnvVarKeyError,
    SqlAlchemyWorkspaceService,
    WorkspaceAlreadyExistsError,
    WorkspaceDirectoryError,
    WorkspaceNotFoundError,
    WorkspaceSecretCipher,
    mask_env_value,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class WorkspaceService(Protocol):
    async def list_for_owner(self, owner_id: UUID) -> list[Workspace]: ...

    async def get_for_owner(self, workspace_id: UUID, owner_id: UUID) -> Workspace: ...

    async def create(
        self,
        owner_id: UUID,
        name: str,
        repo_url: str,
        default_branch: str,
    ) -> Workspace: ...

    async def update(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        name: str | None = None,
        repo_url: str | None = None,
        default_branch: str | None = None,
    ) -> Workspace: ...

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None: ...

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[WorkspaceEnvVar]: ...

    async def upsert_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        value: str,
    ) -> WorkspaceEnvVar: ...

    async def delete_env_var(self, workspace_id: UUID, owner_id: UUID, key: str) -> None: ...


async def get_workspace_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorkspaceService:
    return SqlAlchemyWorkspaceService(
        session=session,
        workspace_root=settings.workspace_root,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
    )


WORKSPACE_SERVICE_DEP = Depends(get_workspace_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def env_response(env_var: WorkspaceEnvVar) -> WorkspaceEnvVarResponse:
    return WorkspaceEnvVarResponse(
        id=env_var.id,
        key=env_var.key,
        masked_value=mask_env_value(),
        created_at=env_var.created_at,
        updated_at=env_var.updated_at,
    )


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> Workspace:
    try:
        return await service.create(
            owner_id=current_user.id,
            name=payload.name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
        )
    except WorkspaceAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already has a workspace",
        ) from None
    except WorkspaceDirectoryError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Workspace directory could not be created",
        ) from None


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> list[Workspace]:
    return await service.list_for_owner(current_user.id)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> Workspace:
    try:
        return await service.get_for_owner(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    payload: WorkspaceUpdateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> Workspace:
    try:
        return await service.update(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            name=payload.name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None


@router.delete("/{workspace_id}", response_model=DeleteResponse)
async def delete_workspace(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    return DeleteResponse(ok=True)


@router.get("/{workspace_id}/status", response_model=WorkspaceStatusResponse)
async def get_workspace_status(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> Workspace:
    try:
        return await service.get_for_owner(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None


@router.get("/{workspace_id}/env-vars", response_model=list[WorkspaceEnvVarResponse])
async def list_env_vars(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> list[WorkspaceEnvVarResponse]:
    try:
        env_vars = await service.list_env_vars(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    return [env_response(env_var) for env_var in env_vars]


@router.put("/{workspace_id}/env-vars", response_model=WorkspaceEnvVarResponse)
async def upsert_env_var(
    workspace_id: UUID,
    payload: WorkspaceEnvVarUpsertRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> WorkspaceEnvVarResponse:
    try:
        env_var = await service.upsert_env_var(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            key=payload.key,
            value=payload.value,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidEnvVarKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Environment variable keys must use uppercase letters, numbers, and underscores",
        ) from None
    return env_response(env_var)


@router.delete("/{workspace_id}/env-vars", response_model=DeleteResponse)
async def delete_env_var(
    workspace_id: UUID,
    key: str = Query(min_length=1, max_length=128),
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete_env_var(workspace_id, current_user.id, key)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidEnvVarKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Environment variable keys must use uppercase letters, numbers, and underscores",
        ) from None
    return DeleteResponse(ok=True)
