from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.api.rate_limits import SANDBOX_RATE_LIMIT_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User
from app.schemas.dev_server import DevServerActionResponse, DevServerStatusResponse
from app.services.dev_server import (
    DevServerNotClonedError,
    DevServerRuntimeError,
    DevServerService,
    DevServerState,
    DevServerWorkspacePathError,
)
from app.services.sandbox import SandboxRuntimeError, get_sandbox_runtime
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/dev", tags=["dev-server"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class DevServerServiceProtocol(Protocol):
    async def setup(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState: ...

    async def run(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState: ...

    async def status(self, workspace_id: UUID, user_id: UUID) -> DevServerState: ...

    async def ensure_running(self, workspace_id: UUID, user_id: UUID) -> DevServerState: ...

    async def stop(self, workspace_id: UUID, user_id: UUID) -> DevServerState: ...


async def get_dev_server_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> DevServerServiceProtocol:
    return DevServerService(session, settings, get_sandbox_runtime())


DEV_SERVER_SERVICE_DEP = Depends(get_dev_server_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


@router.post(
    "/setup",
    response_model=DevServerActionResponse,
    dependencies=[SANDBOX_RATE_LIMIT_DEP],
)
async def setup_dev_server(
    workspace_id: UUID,
    worktree_id: UUID | None = None,
    current_user: User = CURRENT_USER_DEP,
    service: DevServerServiceProtocol = DEV_SERVER_SERVICE_DEP,
) -> DevServerActionResponse:
    """Prepare the environment (toolchain + dependencies) for a worktree without
    running the app. Defaults to the base clone when no worktree is given."""
    try:
        state = await service.setup(workspace_id, current_user.id, worktree_id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WorktreeNotFoundError:
        raise HTTPException(status_code=404, detail="Worktree not found") from None
    except DevServerWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except DevServerNotClonedError:
        raise HTTPException(status_code=409, detail="Repository is not cloned yet") from None
    except (DevServerRuntimeError, SandboxRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return DevServerActionResponse(ok=True, status=state.status, message=state.message)


@router.post("/run", response_model=DevServerActionResponse, dependencies=[SANDBOX_RATE_LIMIT_DEP])
async def run_dev_server(
    workspace_id: UUID,
    worktree_id: UUID | None = None,
    current_user: User = CURRENT_USER_DEP,
    service: DevServerServiceProtocol = DEV_SERVER_SERVICE_DEP,
) -> DevServerActionResponse:
    """Run the app's `dev` task in a worktree on demand (defaults to the base clone).
    Switching to a different worktree stops the previously running dev server first."""
    try:
        state = await service.run(workspace_id, current_user.id, worktree_id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WorktreeNotFoundError:
        raise HTTPException(status_code=404, detail="Worktree not found") from None
    except DevServerWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except DevServerNotClonedError:
        raise HTTPException(status_code=409, detail="Repository is not cloned yet") from None
    except (DevServerRuntimeError, SandboxRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return DevServerActionResponse(ok=True, status=state.status, message=state.message)


@router.get("/status", response_model=DevServerStatusResponse)
async def get_dev_server_status(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: DevServerServiceProtocol = DEV_SERVER_SERVICE_DEP,
) -> DevServerStatusResponse:
    try:
        state = await service.status(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except DevServerWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except (DevServerRuntimeError, SandboxRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return DevServerStatusResponse(
        status=state.status,
        port=state.port,
        message=state.message,
        worktree_id=state.worktree_id,
        branch=state.branch,
    )


@router.post(
    "/ensure",
    response_model=DevServerStatusResponse,
    dependencies=[SANDBOX_RATE_LIMIT_DEP],
)
async def ensure_dev_server(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: DevServerServiceProtocol = DEV_SERVER_SERVICE_DEP,
) -> DevServerStatusResponse:
    """Auto-resume the dev server if it was running before the sandbox slept, then
    report the current status. Safe to poll: a no-op unless a relaunch is needed."""
    try:
        state = await service.ensure_running(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except DevServerWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except (DevServerRuntimeError, SandboxRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return DevServerStatusResponse(
        status=state.status,
        port=state.port,
        message=state.message,
        worktree_id=state.worktree_id,
        branch=state.branch,
    )


@router.post("/stop", response_model=DevServerActionResponse)
async def stop_dev_server(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: DevServerServiceProtocol = DEV_SERVER_SERVICE_DEP,
) -> DevServerActionResponse:
    try:
        state = await service.stop(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except DevServerWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except (DevServerRuntimeError, SandboxRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return DevServerActionResponse(ok=True, status=state.status, message=state.message)
