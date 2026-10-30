import re
from typing import Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.audit_support import (
    AUDIT_LOG_SERVICE_DEP,
    get_audit_log_service,  # noqa: F401 - re-exported: tests override this dependency here
    record_audit_safely,
)
from app.api.auth import CURRENT_USER_DEP
from app.api.workspace_support import workspace_response, workspace_responses
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User, Workspace, WorkspaceEnvVar
from app.schemas.repositories import (
    GitCommitResponse,
    RepositoryCommitDetailResponse,
    RepositoryCommitRequest,
    RepositoryConnectRequest,
    RepositoryCredentialResponse,
    RepositoryDiffResponse,
    RepositoryDiscardRequest,
    RepositoryFileStatus,
    RepositoryLogResponse,
    RepositoryOperationResponse,
    RepositoryStatusResponse,
    SshKeyRequest,
)
from app.schemas.workspaces import (
    AffectedServiceResponse,
    DeleteResponse,
    WorkspaceCreateRequest,
    WorkspaceEnvVarBulkDeleteRequest,
    WorkspaceEnvVarBulkDeleteResponse,
    WorkspaceEnvVarBulkItem,
    WorkspaceEnvVarBulkRequest,
    WorkspaceEnvVarBulkResponse,
    WorkspaceEnvVarResponse,
    WorkspaceEnvVarUpsertRequest,
    WorkspaceEnvVarUpsertResponse,
    WorkspaceResponse,
    WorkspaceStatusResponse,
    WorkspaceUpdateRequest,
)
from app.schemas.worktrees import (
    PullRequestCreateRequest,
    PullRequestResponse,
    WorktreeCommitRequest,
    WorktreeCreateRequest,
    WorktreeResponse,
    WorktreeSyncRequest,
)
from app.services.audit import AuditLogService
from app.services.env_file import parse_env_text
from app.services.git_providers import supports_pull_requests
from app.services.github_client import GitHubApiError, PullRequestService
from app.services.notifications import NotificationService
from app.services.repositories import (
    GitCommandError,
    InvalidRepositoryUrlError,
    NoChangesToCommitError,
    PullRequestsUnsupportedError,
    RepositoryAlreadyClonedError,
    RepositoryCredentialNotFoundError,
    RepositoryNotClonedError,
    RepositoryPathError,
    RepositoryService,
    RepositoryTokenError,
)
from app.services.sandbox import SandboxRuntimeError, get_sandbox_runtime
from app.services.templates import TemplateNotFoundError
from app.services.workspace_env import affected_running_services, egress_warnings
from app.services.workspace_init import WorkspaceInitializationError
from app.services.workspaces import (
    APP_ROOT_UNSET,
    ENV_FILE_PATH_UNSET,
    SNAPSHOT_UNSET,
    BulkItem,
    InvalidEnvScopeError,
    InvalidEnvVarKeyError,
    SqlAlchemyWorkspaceService,
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspaceRuntimeError,
    WorkspaceSecretCipher,
    mask_env_value,
)
from app.services.worktrees import (
    CannotRemoveBaseWorktreeError,
    CannotSyncBaseWorktreeError,
    GitWorktreeError,
    InvalidBranchNameError,
    MergeConflictError,
    WorktreeAlreadyExistsError,
    WorktreeDirtyError,
    WorktreeLimitError,
    WorktreeNotFoundError,
    WorktreeService,
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
        repo_url: str | None,
        default_branch: str,
        snapshot: str | None = None,
        *,
        source: str = "repository",
        template_id: UUID | None = None,
    ) -> Workspace: ...

    async def update(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        name: str | None = None,
        repo_url: str | None = None,
        default_branch: str | None = None,
        snapshot: str | None | object = SNAPSHOT_UNSET,
        app_root: str | None | object = APP_ROOT_UNSET,
        env_file_path: str | None | object = ENV_FILE_PATH_UNSET,
    ) -> Workspace: ...

    async def reprovision(self, workspace_id: UUID, owner_id: UUID) -> Workspace: ...

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None: ...

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[WorkspaceEnvVar]: ...

    async def upsert_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        value: str,
        *,
        custom_command_id: UUID | None = None,
    ) -> tuple[WorkspaceEnvVar, Literal["created", "updated", "unchanged"]]: ...

    async def bulk_upsert_env_vars(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        values: dict[str, str],
        *,
        custom_command_id: UUID | None = None,
        dry_run: bool = False,
    ) -> list[BulkItem]: ...

    async def delete_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        *,
        custom_command_id: UUID | None = None,
    ) -> None: ...

    async def bulk_delete_env_vars(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        keys: list[str],
        *,
        custom_command_id: UUID | None = None,
    ) -> tuple[list[str], list[str]]: ...

    async def env_template(self, workspace_id: UUID, owner_id: UUID) -> str: ...


async def get_workspace_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorkspaceService:
    return SqlAlchemyWorkspaceService(
        session=session,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=get_sandbox_runtime(),
    )


WORKSPACE_SERVICE_DEP = Depends(get_workspace_service)


async def get_repository_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> RepositoryService:
    return RepositoryService(
        session=session,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=get_sandbox_runtime(),
        settings=settings,
    )


REPOSITORY_SERVICE_DEP = Depends(get_repository_service)


async def get_worktree_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorktreeService:
    return WorktreeService(
        session=session,
        runtime=get_sandbox_runtime(),
        settings=settings,
    )


WORKTREE_SERVICE_DEP = Depends(get_worktree_service)


async def get_pull_request_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> PullRequestService:
    return PullRequestService(
        session=session,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        settings=settings,
    )


PULL_REQUEST_SERVICE_DEP = Depends(get_pull_request_service)


async def get_notification_service(session: AsyncSession = DB_SESSION_DEP) -> NotificationService:
    return NotificationService(session)


NOTIFICATION_SERVICE_DEP = Depends(get_notification_service)


async def notify_safely(notification_service: NotificationService, **values) -> None:
    try:
        await notification_service.create(**values)
    except Exception:
        return


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


WORKSPACE_CREATE_ERRORS = (
    WorkspaceRuntimeError,
    InvalidRepositoryUrlError,
    TemplateNotFoundError,
    WorkspaceInitializationError,
)


def workspace_create_error(error: Exception) -> HTTPException:
    """Map workspace-creation failures shared by the personal and team create routes."""
    if isinstance(error, InvalidRepositoryUrlError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.message)
    if isinstance(error, TemplateNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")
    if isinstance(error, WorkspaceInitializationError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Workspace initialization failed: {error.message}",
        )
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))


def repository_error(error: Exception) -> HTTPException:
    if isinstance(error, SandboxRuntimeError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        )
    if isinstance(error, InvalidRepositoryUrlError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.message)
    if isinstance(error, PullRequestsUnsupportedError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Pull requests are only supported for GitHub repositories connected with an "
                f"access token (connected provider: {error.provider})"
            ),
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
    if isinstance(error, WorktreeNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worktree not found")
    if isinstance(error, InvalidBranchNameError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
    if isinstance(error, WorktreeAlreadyExistsError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A worktree for this branch already exists",
        )
    if isinstance(error, WorktreeLimitError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, CannotRemoveBaseWorktreeError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The base worktree cannot be removed",
        )
    if isinstance(error, WorktreeDirtyError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Worktree has uncommitted changes; pass force to remove it",
        )
    if isinstance(error, CannotSyncBaseWorktreeError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The base worktree has no base branch to sync from",
        )
    # MergeConflictError subclasses GitWorktreeError, so it must be checked first.
    if isinstance(error, MergeConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.message)
    if isinstance(error, GitWorktreeError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.message)
    if isinstance(error, GitHubApiError):
        code = error.status_code if error.status_code and 400 <= error.status_code < 500 else (
            status.HTTP_502_BAD_GATEWAY
        )
        return HTTPException(status_code=code, detail=error.message)
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


def credential_response(credential, workspace: Workspace) -> RepositoryCredentialResponse:
    """Public view of a credential — never includes the token or private key."""
    return RepositoryCredentialResponse(
        provider=credential.provider,
        auth_type=credential.auth_type,
        username=credential.username,
        public_key=credential.public_key,
        supports_pull_requests=(
            supports_pull_requests(credential.provider)
            and credential.auth_type == "personal_access_token"
        ),
        repo_url=workspace.repo_url,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


def env_response(env_var: WorkspaceEnvVar) -> WorkspaceEnvVarResponse:
    return WorkspaceEnvVarResponse(
        id=env_var.id,
        key=env_var.key,
        masked_value=mask_env_value(),
        created_at=env_var.created_at,
        updated_at=env_var.updated_at,
        scope=env_var.scope,
        custom_command_id=env_var.custom_command_id,
    )


def affected_service_responses(services) -> list[AffectedServiceResponse]:
    return [
        AffectedServiceResponse(
            service_id=item.service_id,
            custom_command_id=item.custom_command_id,
            name=item.name,
            status=item.status,
            keys=item.keys,
        )
        for item in services
    ]


@router.post("/{workspace_id}/repo/connect", response_model=WorkspaceResponse)
async def connect_repository(
    workspace_id: UUID,
    payload: RepositoryConnectRequest,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> WorkspaceResponse:
    try:
        workspace = await service.connect(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            repo_url=payload.repo_url,
            provider=payload.provider,
            auth_type=payload.auth_type,
            token=payload.token,
            username=payload.username,
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
        SandboxRuntimeError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="repo_connect",
        target_type="repository",
        target_id=workspace.id,
        metadata={
            "repo_url": workspace.repo_url,
            "provider": payload.provider,
            "auth_type": payload.auth_type,
        },
    )
    return await workspace_response(session, workspace, current_user.id)


@router.get("/{workspace_id}/repo/credential", response_model=RepositoryCredentialResponse)
async def get_repository_credential(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    workspace_service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> RepositoryCredentialResponse:
    try:
        workspace = await workspace_service.get_for_owner(workspace_id, current_user.id)
        credential = await service.get_credential(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except RepositoryCredentialNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No repository credential configured",
        ) from None
    return credential_response(credential, workspace)


@router.post("/{workspace_id}/repo/ssh-key", response_model=RepositoryCredentialResponse)
async def ensure_repository_ssh_key(
    workspace_id: UUID,
    payload: SshKeyRequest | None = None,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    workspace_service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryCredentialResponse:
    """Generate (or rotate) the workspace's SSH deploy key and return its public half."""
    rotate = bool(payload and payload.rotate)
    try:
        workspace = await workspace_service.get_for_owner(workspace_id, current_user.id)
        credential = await service.ensure_ssh_key(workspace_id, current_user.id, rotate=rotate)
    except WorkspaceNotFoundError:
        raise not_found() from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="repo_ssh_key_generate",
        target_type="repository",
        target_id=workspace.id,
        metadata={"rotate": rotate},
    )
    return credential_response(credential, workspace)


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
        SandboxRuntimeError,
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
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> RepositoryStatusResponse:
    try:
        worktree_path: str | None = None
        if worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, worktree_id
            )
            worktree_path = worktree.path
        repo_status = await service.status(
            workspace_id, current_user.id, worktree_path=worktree_path
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
        SandboxRuntimeError,
        WorktreeNotFoundError,
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
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> RepositoryDiffResponse:
    try:
        worktree_path: str | None = None
        if worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, worktree_id
            )
            worktree_path = worktree.path
        diff = await service.diff(
            workspace_id, current_user.id, path, worktree_path=worktree_path
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
        SandboxRuntimeError,
        WorktreeNotFoundError,
    ) as error:
        raise repository_error(error) from None
    return RepositoryDiffResponse(diff=diff)


@router.get("/{workspace_id}/repo/log", response_model=RepositoryLogResponse)
async def get_repository_log(
    workspace_id: UUID,
    worktree_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    skip: int = Query(default=0, ge=0),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> RepositoryLogResponse:
    try:
        worktree_path: str | None = None
        if worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, worktree_id
            )
            worktree_path = worktree.path
        commits = await service.log(
            workspace_id,
            current_user.id,
            worktree_path=worktree_path,
            limit=limit,
            skip=skip,
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
        SandboxRuntimeError,
        WorktreeNotFoundError,
    ) as error:
        raise repository_error(error) from None
    return RepositoryLogResponse(
        commits=[
            GitCommitResponse(
                hash=commit.hash,
                short_hash=commit.short_hash,
                author_name=commit.author_name,
                author_email=commit.author_email,
                date=commit.date,
                subject=commit.subject,
            )
            for commit in commits
        ]
    )


@router.get(
    "/{workspace_id}/repo/commits/{sha}", response_model=RepositoryCommitDetailResponse
)
async def get_repository_commit(
    workspace_id: UUID,
    sha: str,
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> RepositoryCommitDetailResponse:
    try:
        worktree_path: str | None = None
        if worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, worktree_id
            )
            worktree_path = worktree.path
        detail = await service.commit_detail(
            workspace_id, current_user.id, sha, worktree_path=worktree_path
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
        SandboxRuntimeError,
        WorktreeNotFoundError,
    ) as error:
        raise repository_error(error) from None
    return RepositoryCommitDetailResponse(
        hash=detail.hash,
        short_hash=detail.short_hash,
        author_name=detail.author_name,
        author_email=detail.author_email,
        date=detail.date,
        subject=detail.subject,
        body=detail.body,
        diff=detail.diff,
    )


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
        SandboxRuntimeError,
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


@router.post("/{workspace_id}/repo/fetch", response_model=RepositoryOperationResponse)
async def fetch_repository(
    workspace_id: UUID,
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        worktree_path: str | None = None
        if worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, worktree_id
            )
            worktree_path = worktree.path
        await service.fetch(workspace_id, current_user.id, worktree_path=worktree_path)
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
        SandboxRuntimeError,
        WorktreeNotFoundError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="repo_fetch",
        target_type="repository",
        target_id=workspace_id,
        metadata={"worktree_id": str(worktree_id) if worktree_id else None},
    )
    return RepositoryOperationResponse(ok=True, message="Fetched from origin.")


@router.post("/{workspace_id}/repo/commit", response_model=RepositoryOperationResponse)
async def commit_repository(
    workspace_id: UUID,
    payload: RepositoryCommitRequest,
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        await service.commit(
            workspace_id,
            current_user.id,
            payload.message,
            author_name=current_user.name,
            author_email=current_user.email,
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
        SandboxRuntimeError,
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


@router.post("/{workspace_id}/repo/discard", response_model=RepositoryOperationResponse)
async def discard_repository_changes(
    workspace_id: UUID,
    payload: RepositoryDiscardRequest,
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        worktree_path: str | None = None
        if worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, worktree_id
            )
            worktree_path = worktree.path
        await service.discard(
            workspace_id,
            current_user.id,
            path=payload.path,
            worktree_path=worktree_path,
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
        SandboxRuntimeError,
        WorktreeNotFoundError,
    ) as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="repo_discard",
        target_type="repository",
        target_id=workspace_id,
        metadata={"path": payload.path},
    )
    message = (
        f"Discarded changes to {payload.path}." if payload.path else "Discarded all changes."
    )
    return RepositoryOperationResponse(ok=True, message=message)


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
        SandboxRuntimeError,
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


# Errors raised by the worktree / pull-request flows, mapped to HTTP by ``repository_error``.
WORKTREE_ERRORS = (
    WorktreeNotFoundError,
    WorktreeAlreadyExistsError,
    WorktreeLimitError,
    InvalidBranchNameError,
    CannotRemoveBaseWorktreeError,
    CannotSyncBaseWorktreeError,
    WorktreeDirtyError,
    MergeConflictError,
    GitWorktreeError,
    GitHubApiError,
    PullRequestsUnsupportedError,
    InvalidRepositoryUrlError,
    RepositoryPathError,
    RepositoryCredentialNotFoundError,
    RepositoryTokenError,
    RepositoryNotClonedError,
    NoChangesToCommitError,
    GitCommandError,
    SandboxRuntimeError,
)


def worktree_response(info) -> WorktreeResponse:
    return WorktreeResponse(
        id=info.id,
        branch=info.branch,
        path=info.path,
        base_ref=info.base_ref,
        is_base=info.is_base,
        status=info.status,
        is_clean=info.is_clean,
        ahead=info.ahead,
        behind=info.behind,
    )


def pull_request_response(row) -> PullRequestResponse:
    return PullRequestResponse(
        id=row.id,
        worktree_id=row.worktree_id,
        number=row.number,
        url=row.url,
        state=row.state,
        head=row.head,
        base=row.base,
        title=row.title,
    )


@router.get("/{workspace_id}/repo/worktrees", response_model=list[WorktreeResponse])
async def list_worktrees(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> list[WorktreeResponse]:
    try:
        infos = await service.list_worktrees(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    return [worktree_response(info) for info in infos]


@router.post("/{workspace_id}/repo/worktrees", response_model=WorktreeResponse, status_code=201)
async def create_worktree(
    workspace_id: UUID,
    payload: WorktreeCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorktreeService = WORKTREE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    notification_service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> WorktreeResponse:
    try:
        row = await service.create_worktree(
            workspace_id, current_user.id, payload.branch, payload.base
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="worktree_create",
        target_type="worktree",
        target_id=row.id,
        metadata={"branch": row.branch, "base_ref": row.base_ref},
    )
    await notify_safely(
        notification_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        notification_type="worktree_created",
        title="Worktree created",
        message=f"Worktree for branch '{row.branch}' is ready.",
    )
    return WorktreeResponse(
        id=row.id,
        branch=row.branch,
        path=row.path,
        base_ref=row.base_ref,
        is_base=row.is_base,
        status=row.status,
        is_clean=True,
        ahead=0,
        behind=0,
    )


@router.delete(
    "/{workspace_id}/repo/worktrees/{worktree_id}", response_model=RepositoryOperationResponse
)
async def remove_worktree(
    workspace_id: UUID,
    worktree_id: UUID,
    force: bool = Query(default=False),
    current_user: User = CURRENT_USER_DEP,
    service: WorktreeService = WORKTREE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    notification_service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        row = await service.remove_worktree(
            workspace_id, current_user.id, worktree_id, force=force
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="worktree_remove",
        target_type="worktree",
        target_id=worktree_id,
        metadata={"branch": row.branch, "force": force},
    )
    await notify_safely(
        notification_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        notification_type="worktree_removed",
        title="Worktree removed",
        message=f"Worktree for branch '{row.branch}' was removed.",
    )
    return RepositoryOperationResponse(ok=True, message="Worktree removed.")


@router.post(
    "/{workspace_id}/repo/worktrees/{worktree_id}/commit",
    response_model=RepositoryOperationResponse,
)
async def commit_worktree(
    workspace_id: UUID,
    worktree_id: UUID,
    payload: WorktreeCommitRequest,
    current_user: User = CURRENT_USER_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        worktree = await worktree_service.get_worktree(
            workspace_id, current_user.id, worktree_id
        )
        await service.commit(
            workspace_id,
            current_user.id,
            payload.message,
            worktree_path=worktree.path,
            author_name=current_user.name,
            author_email=current_user.email,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="worktree_commit",
        target_type="worktree",
        target_id=worktree_id,
        metadata={"branch": worktree.branch, "message": payload.message},
    )
    return RepositoryOperationResponse(ok=True, message="Changes committed.")


@router.post(
    "/{workspace_id}/repo/worktrees/{worktree_id}/push",
    response_model=RepositoryOperationResponse,
)
async def push_worktree(
    workspace_id: UUID,
    worktree_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    notification_service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        worktree = await worktree_service.get_worktree(
            workspace_id, current_user.id, worktree_id
        )
        await service.push(workspace_id, current_user.id, worktree_path=worktree.path)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="worktree_push",
        target_type="worktree",
        target_id=worktree_id,
        metadata={"branch": worktree.branch},
    )
    await notify_safely(
        notification_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        notification_type="worktree_pushed",
        title="Branch pushed",
        message=f"Branch '{worktree.branch}' was pushed to the remote.",
    )
    return RepositoryOperationResponse(ok=True, message="Changes pushed.")


@router.post(
    "/{workspace_id}/repo/worktrees/{worktree_id}/pull",
    response_model=RepositoryOperationResponse,
)
async def pull_worktree(
    workspace_id: UUID,
    worktree_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        worktree = await worktree_service.get_worktree(
            workspace_id, current_user.id, worktree_id
        )
        await service.pull(workspace_id, current_user.id, worktree_path=worktree.path)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="worktree_pull",
        target_type="worktree",
        target_id=worktree_id,
        metadata={"branch": worktree.branch},
    )
    return RepositoryOperationResponse(ok=True, message="Branch pulled.")


@router.post(
    "/{workspace_id}/repo/worktrees/{worktree_id}/sync",
    response_model=RepositoryOperationResponse,
)
async def sync_worktree(
    workspace_id: UUID,
    worktree_id: UUID,
    payload: WorktreeSyncRequest,
    current_user: User = CURRENT_USER_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    service: RepositoryService = REPOSITORY_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> RepositoryOperationResponse:
    try:
        worktree = await worktree_service.get_worktree(
            workspace_id, current_user.id, worktree_id
        )
        # Fetch first (authenticated) so origin/<base_ref> reflects the true remote base, then
        # merge/rebase it into the worktree locally.
        await service.fetch(workspace_id, current_user.id, worktree_path=worktree.path)
        await worktree_service.sync_from_base(
            workspace_id, current_user.id, worktree_id, strategy=payload.strategy
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="worktree_sync",
        target_type="worktree",
        target_id=worktree_id,
        metadata={
            "branch": worktree.branch,
            "base": worktree.base_ref,
            "strategy": payload.strategy,
        },
    )
    return RepositoryOperationResponse(
        ok=True, message=f"Updated '{worktree.branch}' from {worktree.base_ref}."
    )


@router.post(
    "/{workspace_id}/repo/pull-requests",
    response_model=PullRequestResponse,
    status_code=201,
)
async def create_pull_request(
    workspace_id: UUID,
    payload: PullRequestCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
    service: PullRequestService = PULL_REQUEST_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    notification_service: NotificationService = NOTIFICATION_SERVICE_DEP,
) -> PullRequestResponse:
    try:
        head = payload.head
        if payload.worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, payload.worktree_id
            )
            head = worktree.branch
        if not head:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide a worktree_id or an explicit head branch",
            )
        row = await service.create(
            workspace_id,
            current_user.id,
            head=head,
            base=payload.base,
            title=payload.title,
            body=payload.body,
            draft=payload.draft,
            worktree_id=payload.worktree_id,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="pull_request_create",
        target_type="pull_request",
        target_id=row.number,
        metadata={"head": row.head, "base": row.base, "url": row.url},
    )
    await notify_safely(
        notification_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        notification_type="pull_request_opened",
        title="Pull request opened",
        message=f"PR #{row.number} ({row.head} → {row.base}) was opened.",
    )
    return pull_request_response(row)


@router.get("/{workspace_id}/repo/pull-requests", response_model=list[PullRequestResponse])
async def list_pull_requests(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: PullRequestService = PULL_REQUEST_SERVICE_DEP,
) -> list[PullRequestResponse]:
    try:
        rows = await service.list_open(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    return [pull_request_response(row) for row in rows]


@router.get(
    "/{workspace_id}/repo/pull-requests/{number}", response_model=PullRequestResponse
)
async def get_pull_request(
    workspace_id: UUID,
    number: int,
    current_user: User = CURRENT_USER_DEP,
    service: PullRequestService = PULL_REQUEST_SERVICE_DEP,
) -> PullRequestResponse:
    try:
        row = await service.get(workspace_id, current_user.id, number)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKTREE_ERRORS as error:
        raise repository_error(error) from None
    return pull_request_response(row)


@router.post("", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> WorkspaceResponse:
    try:
        create_values = dict(
            owner_id=current_user.id,
            name=payload.name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
            source=payload.source,
            template_id=payload.template_id,
        )
        if payload.snapshot is not None:
            create_values["snapshot"] = payload.snapshot
        workspace = await service.create(**create_values)
    except WorkspaceAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already has a workspace",
        ) from None
    except WORKSPACE_CREATE_ERRORS as error:
        raise workspace_create_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="workspace_create",
        target_type="workspace",
        target_id=workspace.id,
        metadata={
            "name": workspace.name,
            "source": workspace.source,
            "template_id": str(workspace.template_id) if workspace.template_id else None,
        },
    )
    return await workspace_response(session, workspace, current_user.id)


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> list[WorkspaceResponse]:
    workspaces = await service.list_for_owner(current_user.id)
    return await workspace_responses(session, workspaces, current_user.id)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: UUID,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> WorkspaceResponse:
    try:
        workspace = await service.get_for_owner(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    return await workspace_response(session, workspace, current_user.id)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: UUID,
    payload: WorkspaceUpdateRequest,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> WorkspaceResponse:
    try:
        update_values = dict(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            name=payload.name,
            repo_url=payload.repo_url,
            default_branch=payload.default_branch,
        )
        if "snapshot" in payload.model_fields_set:
            update_values["snapshot"] = payload.snapshot
        if "app_root" in payload.model_fields_set:
            update_values["app_root"] = payload.app_root
        if "env_file_path" in payload.model_fields_set:
            update_values["env_file_path"] = payload.env_file_path
        workspace = await service.update(**update_values)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidRepositoryUrlError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=error.message
        ) from None
    except WorkspaceRuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="workspace_update",
        target_type="workspace",
        target_id=workspace.id,
        metadata={"name": workspace.name},
    )
    return await workspace_response(session, workspace, current_user.id)


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
    except WorkspaceRuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from None
    return DeleteResponse(ok=True)


@router.post("/{workspace_id}/provision", response_model=WorkspaceResponse)
async def reprovision_workspace(
    workspace_id: UUID,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> WorkspaceResponse:
    """Retry provisioning a workspace whose sandbox never came up.

    `GET /status` cannot recover such a workspace — it has no sandbox to query — so this is
    the only way back from "error" short of deleting and recreating it.
    """
    try:
        workspace = await service.reprovision(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WORKSPACE_CREATE_ERRORS as error:
        raise workspace_create_error(error) from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace.id,
        action="workspace_reprovision",
        target_type="workspace",
        target_id=workspace.id,
        metadata={"status": workspace.status},
    )
    return await workspace_response(session, workspace, current_user.id)


@router.get("/{workspace_id}/status", response_model=WorkspaceStatusResponse)
async def get_workspace_status(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> WorkspaceStatusResponse:
    try:
        workspace = await service.get_for_owner(workspace_id, current_user.id)
        detail: str | None = None
        if isinstance(service, SqlAlchemyWorkspaceService):
            try:
                workspace.status = await service.runtime.status(workspace)
                service.session.add(workspace)
                await service.session.commit()
                await service.session.refresh(workspace)
            except SandboxRuntimeError:
                workspace.status = "error"
            else:
                try:  # a note, never a reason to report a healthy sandbox as broken
                    detail = await service.runtime.status_detail(workspace)
                except SandboxRuntimeError:
                    detail = None
        response = WorkspaceStatusResponse.model_validate(workspace)
        return response.model_copy(update={"status_detail": detail})
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


# Best-effort key extraction from a `ParseProblem.message` for the "duplicate key" case
# (`"KEY appears N times; last value used"`) -- every other problem message has no key at all.
_DUPLICATE_KEY_MESSAGE_PATTERN = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*) appears \d+ times; last value used$"
)


def _bulk_item_for_problem(problem) -> WorkspaceEnvVarBulkItem:
    match = _DUPLICATE_KEY_MESSAGE_PATTERN.match(problem.message)
    key = match.group("key") if match else f"line {problem.line_no}"
    return WorkspaceEnvVarBulkItem(key=key, status="invalid", message=problem.message)


def _invalid_env_scope_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="custom_command_id does not belong to this workspace",
    )


def _invalid_env_key_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Environment variable keys must use uppercase letters, numbers, and underscores",
    )


@router.put("/{workspace_id}/env-vars", response_model=WorkspaceEnvVarUpsertResponse)
async def upsert_env_var(
    workspace_id: UUID,
    payload: WorkspaceEnvVarUpsertRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorkspaceEnvVarUpsertResponse:
    try:
        env_var, _write_status = await service.upsert_env_var(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            key=payload.key,
            value=payload.value,
            custom_command_id=payload.custom_command_id,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidEnvScopeError:
        raise _invalid_env_scope_error() from None
    except InvalidEnvVarKeyError:
        raise _invalid_env_key_error() from None

    affected = await affected_running_services(
        session, workspace_id, [(env_var.key, payload.custom_command_id)]
    )
    warnings = egress_warnings({env_var.key: payload.value}, settings)

    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="workspace_env_var_upsert",
        target_type="workspace_env_var",
        target_id=env_var.key,
        metadata={"variable": env_var.key, "scope": env_var.scope},
    )
    return WorkspaceEnvVarUpsertResponse(
        **env_response(env_var).model_dump(),
        affected_services=affected_service_responses(affected),
        warnings=warnings,
    )


@router.delete("/{workspace_id}/env-vars", response_model=DeleteResponse)
async def delete_env_var(
    workspace_id: UUID,
    key: str = Query(min_length=1, max_length=128),
    custom_command_id: UUID | None = Query(None),
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete_env_var(
            workspace_id, current_user.id, key, custom_command_id=custom_command_id
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidEnvVarKeyError:
        raise _invalid_env_key_error() from None
    await record_audit_safely(
        audit_log_service,
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="workspace_env_var_delete",
        target_type="workspace_env_var",
        target_id=key,
        metadata={"variable": key, "scope": "service" if custom_command_id else "workspace"},
    )
    return DeleteResponse(ok=True)


@router.post("/{workspace_id}/env-vars/bulk", response_model=WorkspaceEnvVarBulkResponse)
async def bulk_upsert_env_vars(
    workspace_id: UUID,
    payload: WorkspaceEnvVarBulkRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorkspaceEnvVarBulkResponse:
    parse_invalid: list[WorkspaceEnvVarBulkItem] = []
    if payload.text is not None:
        parsed_lines, problems = parse_env_text(payload.text)
        values = {line.key: line.value for line in parsed_lines}
        parse_invalid = [_bulk_item_for_problem(problem) for problem in problems]
    else:
        values = payload.values or {}

    try:
        results: list[BulkItem] = await service.bulk_upsert_env_vars(
            workspace_id=workspace_id,
            owner_id=current_user.id,
            values=values,
            custom_command_id=payload.custom_command_id,
            dry_run=payload.dry_run,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidEnvScopeError:
        raise _invalid_env_scope_error() from None

    items = [
        WorkspaceEnvVarBulkItem(key=item.key, status=item.status, message=item.message)
        for item in results
    ] + parse_invalid

    counts = {"created": 0, "updated": 0, "unchanged": 0, "invalid": 0}
    for item in items:
        counts[item.status] += 1

    written_pairs = [
        (item.key, payload.custom_command_id)
        for item in items
        if item.status in ("created", "updated")
    ]
    affected = await affected_running_services(session, workspace_id, written_pairs)
    warnings = egress_warnings(
        {item.key: values[item.key] for item in items if item.key in values}, settings
    )

    if not payload.dry_run and written_pairs:
        await record_audit_safely(
            audit_log_service,
            user_id=current_user.id,
            workspace_id=workspace_id,
            action="workspace_env_var_bulk_upsert",
            target_type="workspace_env_var",
            target_id=str(workspace_id),
            metadata={
                "variables": [key for key, _ in written_pairs],
                "scope": "service" if payload.custom_command_id else "workspace",
                "custom_command_id": (
                    str(payload.custom_command_id) if payload.custom_command_id else None
                ),
            },
        )

    return WorkspaceEnvVarBulkResponse(
        items=items,
        created=counts["created"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        invalid=counts["invalid"],
        dry_run=payload.dry_run,
        affected_services=affected_service_responses(affected),
        warnings=warnings,
    )


@router.post(
    "/{workspace_id}/env-vars/bulk-delete", response_model=WorkspaceEnvVarBulkDeleteResponse
)
async def bulk_delete_env_vars(
    workspace_id: UUID,
    payload: WorkspaceEnvVarBulkDeleteRequest,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
    audit_log_service: AuditLogService = AUDIT_LOG_SERVICE_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> WorkspaceEnvVarBulkDeleteResponse:
    try:
        deleted, missing = await service.bulk_delete_env_vars(
            workspace_id,
            current_user.id,
            payload.keys,
            custom_command_id=payload.custom_command_id,
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except InvalidEnvScopeError:
        raise _invalid_env_scope_error() from None

    affected = await affected_running_services(
        session, workspace_id, [(key, payload.custom_command_id) for key in deleted]
    )

    if deleted:
        await record_audit_safely(
            audit_log_service,
            user_id=current_user.id,
            workspace_id=workspace_id,
            action="workspace_env_var_bulk_delete",
            target_type="workspace_env_var",
            target_id=str(workspace_id),
            metadata={
                "variables": deleted,
                "scope": "service" if payload.custom_command_id else "workspace",
                "custom_command_id": (
                    str(payload.custom_command_id) if payload.custom_command_id else None
                ),
            },
        )

    return WorkspaceEnvVarBulkDeleteResponse(
        deleted=deleted,
        missing=missing,
        affected_services=affected_service_responses(affected),
    )


@router.get("/{workspace_id}/env-vars/template")
async def env_var_template(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: WorkspaceService = WORKSPACE_SERVICE_DEP,
) -> PlainTextResponse:
    try:
        text = await service.env_template(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename=".env.example"'},
    )
