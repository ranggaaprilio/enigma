from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User, Workspace, WorkspaceEnvVar
from app.schemas.repositories import (
    RepositoryCommitRequest,
    RepositoryConnectRequest,
    RepositoryDiffResponse,
    RepositoryFileStatus,
    RepositoryOperationResponse,
    RepositoryStatusResponse,
)
from app.schemas.workspaces import (
    DeleteResponse,
    WorkspaceCreateRequest,
    WorkspaceEnvVarResponse,
    WorkspaceEnvVarUpsertRequest,
    WorkspaceResponse,
    WorkspaceStatusResponse,
    WorkspaceUpdateRequest,
)
from app.services.audit import AuditLogService
from app.services.repositories import (
    GitCommandError,
    InvalidRepositoryUrlError,
    NoChangesToCommitError,
    RepositoryAlreadyClonedError,
    RepositoryCredentialNotFoundError,
    RepositoryNotClonedError,
    RepositoryPathError,
    RepositoryService,
    RepositoryTokenError,
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


async def get_repository_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> RepositoryService:
    return RepositoryService(
        session=session,
        workspace_root=settings.workspace_root,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
    )


REPOSITORY_SERVICE_DEP = Depends(get_repository_service)


async def get_audit_log_service(session: AsyncSession = DB_SESSION_DEP) -> AuditLogService:
    return AuditLogService(session)


AUDIT_LOG_SERVICE_DEP = Depends(get_audit_log_service)


async def record_audit_safely(audit_log_service: AuditLogService, **values) -> None:
    try:
        await audit_log_service.record(**values)
    except Exception:
        return


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def repository_error(error: Exception) -> HTTPException:
    if isinstance(error, InvalidRepositoryUrlError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only GitHub HTTPS repository URLs are supported",
        )
    if isinstance(error, RepositoryPathError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository path is invalid",
        )
    if isinstance(error, RepositoryCredentialNotFoundError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect repository credentials before running this operation",
        )
    if isinstance(error, RepositoryTokenError):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Repository credential could not be decrypted",
        )
    if isinstance(error, RepositoryAlreadyClonedError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repository is already cloned or the workspace directory is not empty",
        )
    if isinstance(error, RepositoryNotClonedError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Clone the repository before running this operation",
        )
    if isinstance(error, NoChangesToCommitError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There are no changes to commit",
        )
    if isinstance(error, GitCommandError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.message)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


def env_response(env_var: WorkspaceEnvVar) -> WorkspaceEnvVarResponse:
    return WorkspaceEnvVarResponse(
        id=env_var.id,
        key=env_var.key,
        masked_value=mask_env_value(),
        created_at=env_var.created_at,
        updated_at=env_var.updated_at,
    )


@router.post("/{workspace_id}/repo/connect", response_model=WorkspaceResponse)
async def connect_repository(
    workspace_id: UUID,
    payload: RepositoryConnectRequest,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> Workspace:
    try:
        workspace = await service.connect(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            repo_url=payload.repo_url,
            token=payload.token,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="repo_connect",
        target_type="repository",
        target_id=workspace.id,
        metadata={"repo_url": workspace.repo_url},
    )
    return workspace


@router.post("/{workspace_id}/repo/clone", response_model=RepositoryOperationResponse)
async def clone_repository(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        await service.clone(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="repo_clone",
        target_type="repository",
        target_id=workspace_id,
        metadata={},
    )
    return RepositoryOperationResponse(ok=True, message="Repository cloned.")


@router.get("/{workspace_id}/repo/status", response_model=RepositoryStatusResponse)
async def get_repository_status(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
) -> RepositoryStatusResponse:
    try:
        repo_status = await service.status(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    return RepositoryStatusResponse(
        cloned=repo_status.cloned,
        branch=repo_status.branch,
        is_clean=repo_status.is_clean,
        files=[
            RepositoryFileStatus(path=file_status.path, status=file_status.status)
            for file_status in repo_status.files
        ],
    )


@router.get("/{workspace_id}/repo/diff", response_model=RepositoryDiffResponse)
async def get_repository_diff(
    workspace_id: UUID,
    path: str | None = Query(default=None, max_length=1024),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
) -> RepositoryDiffResponse:
    try:
        diff = await service.diff(workspace_id, current_user.id, path)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    return RepositoryDiffResponse(diff=diff)


@router.post("/{workspace_id}/repo/pull", response_model=RepositoryOperationResponse)
async def pull_repository(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        await service.pull(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="repo_pull",
        target_type="repository",
        target_id=workspace_id,
        metadata={},
    )
    return RepositoryOperationResponse(ok=True, message="Repository pulled.")


@router.post("/{workspace_id}/repo/commit", response_model=RepositoryOperationResponse)
async def commit_repository(
    workspace_id: UUID,
    payload: RepositoryCommitRequest,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        await service.commit(workspace_id, current_user.id, payload.message)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="repo_commit",
        target_type="repository",
        target_id=workspace_id,
        metadata={"message": payload.message},
    )
    return RepositoryOperationResponse(ok=True, message="Changes committed.")


@router.post("/{workspace_id}/repo/push", response_model=RepositoryOperationResponse)
async def push_repository(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        await service.push(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (
        InvalidRepositoryUrlError,
        RepositoryPathError,
        RepositoryCredentialNotFoundError,
        RepositoryTokenError,
        RepositoryAlreadyClonedError,
        RepositoryNotClonedError,
        NoChangesToCommitError,
        GitCommandError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="repo_push",
        target_type="repository",
        target_id=workspace_id,
        metadata={},
    )
    return RepositoryOperationResponse(ok=True, message="Changes pushed.")


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> Workspace:
    try:
        workspace = await service.create(
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
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="workspace_create",
        target_type="workspace",
        target_id=workspace.id,
        metadata={"name": workspace.name},
    )
    return workspace


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
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> Workspace:
    try:
        workspace = await service.update(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            name=payload.name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="workspace_update",
        target_type="workspace",
        target_id=workspace.id,
        metadata={"name": workspace.name},
    )
    return workspace


@router.delete("/{workspace_id}", response_model=DeleteResponse)
async def delete_workspace(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> DeleteResponse:
    try:
        workspace = await service.get_for_owner(workspace_id, current_user.id)
        await record_audit_safely(
            audit_log_service,
            user_id=current_user.id,
            workspace_id=workspace.id,
            action="workspace_delete",
            target_type="workspace",
            target_id=workspace.id,
            metadata={},
        )
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
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
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
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="workspace_env_var_upsert",
        target_type="workspace_env_var",
        target_id=env_var.key,
        metadata={"key": env_var.key},
    )
    return env_response(env_var)


@router.delete("/{workspace_id}/env-vars", response_model=DeleteResponse)
async def delete_env_var(
    workspace_id: UUID,
    key: str = Query(min_length=1, max_length=128),
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
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
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="workspace_env_var_delete",
        target_type="workspace_env_var",
        target_id=key,
        metadata={"key": key},
    )
    return DeleteResponse(ok=True)
