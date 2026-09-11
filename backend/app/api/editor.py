from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import EditorSession, User
from app.schemas.editor import (
    EditorSessionResponse,
    EditorStartRequest,
    EditorStatusResponse,
)
from app.services.daytona_runtime import get_daytona_runtime
from app.services.editor import (
    EditorRuntimeError,
    EditorService,
    EditorSessionNotFoundError,
    EditorWorkspacePathError,
    EditorWorktreeError,
)
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/editor", tags=["editor"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class EditorServiceProtocol(Protocol):
    async def start_or_reuse(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> EditorSession: ...

    async def status(self, workspace_id: UUID, user_id: UUID) -> EditorSession | None: ...

    async def stop(self, workspace_id: UUID, user_id: UUID) -> EditorSession: ...

    async def prime_url(self, editor_session: EditorSession) -> str: ...


async def get_editor_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> EditorServiceProtocol:
    return EditorService(session, settings, get_daytona_runtime())


EDITOR_SERVICE_DEP = Depends(get_editor_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


async def _to_response(
    service: EditorServiceProtocol, editor_session: EditorSession
) -> EditorSessionResponse:
    """`prime_url` is minted per response rather than stored, so the response model is built
    here instead of straight off the ORM row."""
    prime_url = ""
    if editor_session.status == "active":
        prime_url = await service.prime_url(editor_session)
    return EditorSessionResponse(
        id=editor_session.id,
        workspace_id=editor_session.workspace_id,
        user_id=editor_session.user_id,
        url=editor_session.url,
        prime_url=prime_url,
        status=editor_session.status,
        worktree_id=editor_session.worktree_id,
        folder=editor_session.folder,
        started_at=editor_session.started_at,
        last_active_at=editor_session.last_active_at,
    )


@router.post("/start", response_model=EditorSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_editor_session(
    workspace_id: UUID,
    payload: EditorStartRequest | None = None,
    current_user: User = CURRENT_USER_DEP,
    service: EditorServiceProtocol = EDITOR_SERVICE_DEP,
) -> EditorSessionResponse:
    worktree_id = payload.worktree_id if payload else None
    try:
        editor_session = await service.start_or_reuse(workspace_id, current_user.id, worktree_id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except EditorWorktreeError:
        raise HTTPException(status_code=404, detail="Worktree not found") from None
    except EditorWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except EditorRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return await _to_response(service, editor_session)


@router.get("/status", response_model=EditorStatusResponse)
async def get_editor_status(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: EditorServiceProtocol = EDITOR_SERVICE_DEP,
) -> EditorStatusResponse:
    try:
        editor_session = await service.status(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except EditorRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    if editor_session is None:
        return EditorStatusResponse(status="inactive", session=None)
    return EditorStatusResponse(
        status=editor_session.status,
        session=await _to_response(service, editor_session),
    )


@router.post("/stop", response_model=EditorSessionResponse)
async def stop_editor_session(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: EditorServiceProtocol = EDITOR_SERVICE_DEP,
) -> EditorSessionResponse:
    try:
        editor_session = await service.stop(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except EditorSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Editor session not found") from None
    return await _to_response(service, editor_session)
