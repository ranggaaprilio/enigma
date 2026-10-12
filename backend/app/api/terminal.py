from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.api.rate_limits import SANDBOX_RATE_LIMIT_DEP
from app.core.config import Settings, get_settings
from app.core.middleware import websocket_context
from app.core.security import verify_session_token
from app.db.session import async_session_maker, get_db_session
from app.models import TerminalSession, User
from app.schemas.terminal import TerminalSessionCreateRequest, TerminalSessionResponse
from app.schemas.workspaces import DeleteResponse
from app.services.daytona_runtime import get_daytona_runtime
from app.services.run_paths import join_subpath, validate_relative_subpath
from app.services.terminal import (
    TerminalPtyBridge,
    TerminalRuntimeError,
    TerminalService,
    TerminalSessionNotFoundError,
    TerminalWorkspacePathError,
)
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError, WorktreeService

router = APIRouter(prefix="/workspaces/{workspace_id}/terminal", tags=["terminal"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class TerminalServiceProtocol(Protocol):
    async def create_or_reuse(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
    ) -> TerminalSession: ...

    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[TerminalSession]:
        ...

    async def stop(self, workspace_id: UUID, terminal_session_id: UUID, user_id: UUID) -> None: ...


async def get_terminal_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> TerminalServiceProtocol:
    return TerminalService(
        session=session,
        settings=settings,
        runtime=get_daytona_runtime(),
    )


TERMINAL_SERVICE_DEP = Depends(get_terminal_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def terminal_error(error: Exception) -> HTTPException:
    if isinstance(error, TerminalWorkspacePathError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace directory is not ready",
        )
    if isinstance(error, TerminalRuntimeError):
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


@router.post(
    "/sessions",
    dependencies=[SANDBOX_RATE_LIMIT_DEP],
    response_model=TerminalSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal_session(
    workspace_id: UUID,
    payload: TerminalSessionCreateRequest | None = None,
    current_user: User = CURRENT_USER_DEP,
    service: TerminalServiceProtocol = TERMINAL_SERVICE_DEP,
) -> TerminalSession:
    try:
        return await service.create_or_reuse(
            workspace_id,
            current_user.id,
            payload.name if payload else "Terminal",
        )
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (TerminalWorkspacePathError, TerminalRuntimeError) as error:
        raise terminal_error(error) from None


@router.get("/sessions", response_model=list[TerminalSessionResponse])
async def list_terminal_sessions(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: TerminalServiceProtocol = TERMINAL_SERVICE_DEP,
) -> list[TerminalSession]:
    try:
        return await service.list_for_workspace(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None


@router.delete("/sessions/{terminal_session_id}", response_model=DeleteResponse)
async def delete_terminal_session(
    workspace_id: UUID,
    terminal_session_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: TerminalServiceProtocol = TERMINAL_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.stop(workspace_id, terminal_session_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except TerminalSessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Terminal session not found",
        ) from None
    return DeleteResponse(ok=True)


@router.websocket("/sessions/{terminal_session_id}")
@websocket_context("terminal")
async def connect_terminal_session(
    websocket: WebSocket,
    workspace_id: UUID,
    terminal_session_id: UUID,
) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        service = TerminalService(
            session=session,
            settings=settings,
            runtime=get_daytona_runtime(),
        )
        try:
            workspace, terminal_session = await service.get_for_workspace(
                workspace_id,
                terminal_session_id,
                user.id,
            )
        except (WorkspaceNotFoundError, TerminalSessionNotFoundError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if terminal_session.status != "active":
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await service.touch(terminal_session)
        env, template_problems = await service.environment(workspace.id, workspace)

        # An optional ``worktree_id`` query param points the terminal at a feature worktree.
        # Each non-base worktree gets its own PTY (keyed by worktree) so terminals are isolated.
        cwd: str | None = None
        pty_session_id: str | None = None
        worktree_raw = websocket.query_params.get("worktree_id")
        if worktree_raw:
            try:
                worktree_uuid = UUID(worktree_raw)
            except ValueError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            worktree_service = WorktreeService(
                session=session, runtime=get_daytona_runtime(), settings=settings
            )
            try:
                resolved = await worktree_service.resolve_path(workspace, worktree_uuid)
            except WorktreeNotFoundError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            if resolved != workspace.workspace_path:
                cwd = resolved
                pty_session_id = f"{terminal_session.pty_session_id}:{worktree_uuid.hex}"

        # A configured app root drops the terminal straight into the directory the workspace's
        # commands run in, so what the user types by hand matches what the Run buttons do.
        app_root = validate_relative_subpath(workspace.app_root)
        if app_root:
            cwd = join_subpath(cwd or workspace.workspace_path, app_root)

    await websocket.accept()
    # Epic 13: a terminal is the one place unresolvable ${ENIGMA_*} templates are non-fatal --
    # the offending keys were already dropped from `env` above; this just tells the user why.
    for problem in template_problems:
        try:
            await websocket.send_json({"type": "notice", "message": problem.message})
        except RuntimeError:
            break
    bridge = TerminalPtyBridge(
        runtime=get_daytona_runtime(),
        default_cols=settings.terminal_default_cols,
        default_rows=settings.terminal_default_rows,
    )
    await bridge.bridge(
        websocket,
        workspace,
        terminal_session,
        env,
        cwd=cwd,
        pty_session_id=pty_session_id,
    )


async def websocket_user(
    websocket: WebSocket,
    session: AsyncSession,
    settings: Settings,
) -> User | None:
    token = websocket.cookies.get(settings.auth_cookie_name)
    if token is None:
        return None
    payload = verify_session_token(token, settings.auth_secret_key)
    if payload is None:
        return None
    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user
