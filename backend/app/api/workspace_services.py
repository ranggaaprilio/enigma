from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.api.commands import websocket_user
from app.core.config import Settings, get_settings
from app.db.session import async_session_maker, get_db_session
from app.models import User, Workspace, WorkspaceService
from app.schemas.workspace_services import (
    ServiceActionRequest,
    ServiceActionResponse,
    ServiceLogsResponse,
    WorkspaceEnvironmentState,
    WorkspaceServiceResponse,
    WorkspaceServicesResponse,
)
from app.services.commands import load_workspace_env, redactable_env_values
from app.services.daytona_runtime import (
    DaytonaCommandTimeoutError,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.git_providers import redact_secrets
from app.services.service_scripts import ServicePaths
from app.services.workspace_services import (
    WorkspaceServiceLimitError,
    WorkspaceServiceManager,
    WorkspaceServiceNotClonedError,
    WorkspaceServiceNotFoundError,
    WorkspaceServicePortConflictError,
    WorkspaceServiceRuntimeError,
    WorkspaceServiceWorkspacePathError,
)
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/services", tags=["workspace-services"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)

_DEFAULT_LOG_TAIL_CHARS = 20_000


class WorkspaceServiceManagerProtocol(Protocol):
    async def get_workspace_for(self, workspace_id: UUID, user_id: UUID) -> Workspace: ...

    async def to_response(self, workspace: Workspace, row: WorkspaceService) -> dict: ...

    async def to_response_list(
        self, workspace: Workspace, rows: list[WorkspaceService]
    ) -> list[dict]: ...

    def environment_from_primary(self, primary: WorkspaceService) -> dict: ...

    def max_concurrent_services(self) -> int: ...

    async def list(
        self, workspace_id: UUID, user_id: UUID
    ) -> tuple[list[WorkspaceService], WorkspaceService]: ...

    async def start(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> tuple[WorkspaceService, str | None]: ...

    async def stop(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID
    ) -> WorkspaceService: ...

    async def restart(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> WorkspaceService: ...

    async def ensure_running(
        self, workspace_id: UUID, user_id: UUID
    ) -> tuple[list[WorkspaceService], WorkspaceService]: ...

    async def logs_tail(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, tail: int
    ) -> str: ...

    async def log_paths(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID
    ) -> ServicePaths: ...


async def get_workspace_service_manager(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorkspaceServiceManagerProtocol:
    return WorkspaceServiceManager(session, settings, get_daytona_runtime())


WORKSPACE_SERVICE_MANAGER_DEP = Depends(get_workspace_service_manager)


def workspace_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def service_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")


def worktree_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worktree not found")


def sandbox_not_ready() -> HTTPException:
    return HTTPException(status_code=409, detail="Workspace sandbox is not ready")


def not_cloned() -> HTTPException:
    return HTTPException(status_code=409, detail="Repository is not cloned yet")


async def _aggregate_response(
    manager: WorkspaceServiceManagerProtocol,
    workspace_id: UUID,
    user_id: UUID,
    rows: list[WorkspaceService],
    primary: WorkspaceService,
) -> WorkspaceServicesResponse:
    workspace = await manager.get_workspace_for(workspace_id, user_id)
    services_data = await manager.to_response_list(workspace, rows)
    return WorkspaceServicesResponse(
        services=[WorkspaceServiceResponse(**data) for data in services_data],
        environment=WorkspaceEnvironmentState(**manager.environment_from_primary(primary)),
        limit=manager.max_concurrent_services(),
    )


async def _service_response(
    manager: WorkspaceServiceManagerProtocol,
    workspace_id: UUID,
    user_id: UUID,
    row: WorkspaceService,
    warning: str | None = None,
) -> ServiceActionResponse:
    workspace = await manager.get_workspace_for(workspace_id, user_id)
    data = await manager.to_response(workspace, row)
    return ServiceActionResponse(service=WorkspaceServiceResponse(**data), warning=warning)


@router.get("", response_model=WorkspaceServicesResponse)
async def list_workspace_services(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    manager: WorkspaceServiceManagerProtocol = WORKSPACE_SERVICE_MANAGER_DEP,
) -> WorkspaceServicesResponse:
    try:
        rows, primary = await manager.list(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorkspaceServiceWorkspacePathError:
        raise sandbox_not_ready() from None
    except DaytonaRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return await _aggregate_response(manager, workspace_id, current_user.id, rows, primary)


@router.post("/ensure", response_model=WorkspaceServicesResponse)
async def ensure_workspace_services(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    manager: WorkspaceServiceManagerProtocol = WORKSPACE_SERVICE_MANAGER_DEP,
) -> WorkspaceServicesResponse:
    try:
        rows, primary = await manager.ensure_running(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorkspaceServiceWorkspacePathError:
        raise sandbox_not_ready() from None
    except DaytonaRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return await _aggregate_response(manager, workspace_id, current_user.id, rows, primary)


@router.post("/{service_id}/start", response_model=ServiceActionResponse)
async def start_workspace_service(
    workspace_id: UUID,
    service_id: UUID,
    payload: ServiceActionRequest | None = None,
    current_user: User = CURRENT_USER_DEP,
    manager: WorkspaceServiceManagerProtocol = WORKSPACE_SERVICE_MANAGER_DEP,
) -> ServiceActionResponse:
    worktree_id = payload.worktree_id if payload else None
    try:
        row, warning = await manager.start(workspace_id, service_id, current_user.id, worktree_id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorkspaceServiceNotFoundError:
        raise service_not_found() from None
    except WorktreeNotFoundError:
        raise worktree_not_found() from None
    except WorkspaceServiceWorkspacePathError:
        raise sandbox_not_ready() from None
    except WorkspaceServiceNotClonedError:
        raise not_cloned() from None
    except WorkspaceServiceLimitError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except WorkspaceServicePortConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except (WorkspaceServiceRuntimeError, DaytonaRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return await _service_response(manager, workspace_id, current_user.id, row, warning)


@router.post("/{service_id}/stop", response_model=ServiceActionResponse)
async def stop_workspace_service(
    workspace_id: UUID,
    service_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    manager: WorkspaceServiceManagerProtocol = WORKSPACE_SERVICE_MANAGER_DEP,
) -> ServiceActionResponse:
    try:
        row = await manager.stop(workspace_id, service_id, current_user.id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorkspaceServiceNotFoundError:
        raise service_not_found() from None
    except WorkspaceServiceWorkspacePathError:
        raise sandbox_not_ready() from None
    except (WorkspaceServiceRuntimeError, DaytonaRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return await _service_response(manager, workspace_id, current_user.id, row)


@router.post("/{service_id}/restart", response_model=ServiceActionResponse)
async def restart_workspace_service(
    workspace_id: UUID,
    service_id: UUID,
    payload: ServiceActionRequest | None = None,
    current_user: User = CURRENT_USER_DEP,
    manager: WorkspaceServiceManagerProtocol = WORKSPACE_SERVICE_MANAGER_DEP,
) -> ServiceActionResponse:
    worktree_id = payload.worktree_id if payload else None
    try:
        row = await manager.restart(workspace_id, service_id, current_user.id, worktree_id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorkspaceServiceNotFoundError:
        raise service_not_found() from None
    except WorktreeNotFoundError:
        raise worktree_not_found() from None
    except WorkspaceServiceWorkspacePathError:
        raise sandbox_not_ready() from None
    except WorkspaceServiceNotClonedError:
        raise not_cloned() from None
    except (WorkspaceServiceRuntimeError, DaytonaRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return await _service_response(manager, workspace_id, current_user.id, row)


@router.get("/{service_id}/logs", response_model=ServiceLogsResponse)
async def get_workspace_service_logs(
    workspace_id: UUID,
    service_id: UUID,
    tail: int = _DEFAULT_LOG_TAIL_CHARS,
    current_user: User = CURRENT_USER_DEP,
    manager: WorkspaceServiceManagerProtocol = WORKSPACE_SERVICE_MANAGER_DEP,
) -> ServiceLogsResponse:
    try:
        content = await manager.logs_tail(workspace_id, service_id, current_user.id, tail)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorkspaceServiceNotFoundError:
        raise service_not_found() from None
    except (WorkspaceServiceRuntimeError, DaytonaRuntimeError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return ServiceLogsResponse(content=content)


@router.websocket("/{service_id}/logs/stream")
async def stream_workspace_service_logs(
    websocket: WebSocket, workspace_id: UUID, service_id: UUID
) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        manager = WorkspaceServiceManager(session, settings, get_daytona_runtime())
        try:
            workspace = await manager.get_workspace_for(workspace_id, user.id)
            paths = await manager.log_paths(workspace_id, service_id, user.id)
            env = await load_workspace_env(session, settings, workspace_id)
        except (
            WorkspaceNotFoundError,
            WorkspaceServiceNotFoundError,
            WorkspaceServiceWorkspacePathError,
        ):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    secrets = redactable_env_values(env)
    runtime = get_daytona_runtime()

    async def on_output(content: str) -> None:
        try:
            await websocket.send_json(
                {"type": "output", "content": redact_secrets(content, *secrets)}
            )
        except Exception:
            pass

    stream_command = (
        f"timeout {settings.service_log_stream_timeout_seconds} "
        f"tail -c {_DEFAULT_LOG_TAIL_CHARS} -F {paths.log}"
    )
    stream_task = asyncio.create_task(
        runtime.stream_command(
            workspace,
            session_id=f"svc-logs-{service_id.hex}-{uuid4().hex}",
            command=stream_command,
            cwd="/tmp",
            env=None,
            timeout=None,
            on_stdout=on_output,
            on_stderr=on_output,
        )
    )
    receive_task = asyncio.create_task(websocket.receive())
    try:
        done, pending = await asyncio.wait(
            {stream_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if stream_task in done:
            try:
                stream_task.result()
            except (DaytonaCommandTimeoutError, DaytonaRuntimeError, WebSocketDisconnect):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        stream_task.cancel()
        receive_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
