from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import PreviewSession, User
from app.schemas.preview import PreviewDetectResponse, PreviewSessionResponse
from app.services.daytona_runtime import DaytonaRuntimeError, get_daytona_runtime
from app.services.preview import (
    PreviewNotFoundError,
    PreviewService,
    PreviewSessionNotFoundError,
    PreviewWorkspacePathError,
)
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/preview", tags=["preview"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class PreviewServiceProtocol(Protocol):
    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[PreviewSession]:
        ...

    async def detect(self, workspace_id: UUID, user_id: UUID) -> PreviewSession: ...

    async def refresh(
        self, workspace_id: UUID, preview_session_id: UUID, user_id: UUID
    ) -> PreviewSession: ...

    async def stop(
        self, workspace_id: UUID, preview_session_id: UUID, user_id: UUID
    ) -> PreviewSession: ...


async def get_preview_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> PreviewServiceProtocol:
    return PreviewService(session, settings, get_daytona_runtime())


PREVIEW_SERVICE_DEP = Depends(get_preview_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


@router.get("/sessions", response_model=list[PreviewSessionResponse])
async def list_preview_sessions(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: PreviewServiceProtocol = PREVIEW_SERVICE_DEP,
) -> list[PreviewSession]:
    try:
        return await service.list_for_workspace(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None


@router.post("/detect", response_model=PreviewDetectResponse)
async def detect_preview_session(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: PreviewServiceProtocol = PREVIEW_SERVICE_DEP,
) -> PreviewDetectResponse:
    try:
        preview = await service.detect(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except PreviewNotFoundError:
        return PreviewDetectResponse(status="inactive", session=None)
    except PreviewWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except DaytonaRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return PreviewDetectResponse(status="active", session=preview)


@router.post("/sessions/{preview_session_id}/refresh", response_model=PreviewSessionResponse)
async def refresh_preview_session(
    workspace_id: UUID,
    preview_session_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: PreviewServiceProtocol = PREVIEW_SERVICE_DEP,
) -> PreviewSession:
    try:
        return await service.refresh(workspace_id, preview_session_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Preview session not found") from None
    except DaytonaRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None


@router.post("/sessions/{preview_session_id}/stop", response_model=PreviewSessionResponse)
async def stop_preview_session(
    workspace_id: UUID,
    preview_session_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: PreviewServiceProtocol = PREVIEW_SERVICE_DEP,
) -> PreviewSession:
    try:
        return await service.stop(workspace_id, preview_session_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="Preview session not found") from None
