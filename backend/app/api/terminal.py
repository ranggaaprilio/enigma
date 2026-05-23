from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.security import verify_session_token
from app.db.session import async_session_maker, get_db_session
from app.models import TerminalSession, User
from app.schemas.terminal import TerminalSessionCreateRequest, TerminalSessionResponse
from app.schemas.workspaces import DeleteResponse
from app.services.terminal import (
    TerminalPtyBridge,
    TerminalRuntimeError,
    TerminalService,
    TerminalSessionNotFoundError,
    TerminalWorkspacePathError,
)
from app.services.workspaces import WorkspaceNotFoundError

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
        workspace_root=settings.workspace_root,
        tmux_binary=settings.terminal_tmux_binary,
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
            workspace_root=settings.workspace_root,
            tmux_binary=settings.terminal_tmux_binary,
        )
        try:
            _workspace, terminal_session = await service.get_for_workspace(
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

    await websocket.accept()
    bridge = TerminalPtyBridge(
        tmux_binary=settings.terminal_tmux_binary,
        default_cols=settings.terminal_default_cols,
        default_rows=settings.terminal_default_rows,
    )
    await bridge.bridge(websocket, terminal_session)


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
