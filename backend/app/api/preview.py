from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import CustomCommand, PreviewSession, User, Workspace
from app.schemas.preview import (
    PreviewAliasRegenerateRequest,
    PreviewAliasRegenerateResponse,
    PreviewAliasToggleResponse,
    PreviewCompositionResponse,
    PreviewDetectResponse,
    PreviewSessionResponse,
)
from app.services.preview import (
    PreviewNotFoundError,
    PreviewService,
    PreviewSessionNotFoundError,
    PreviewWorkspacePathError,
)
from app.services.preview_alias import (
    InvalidPreviewAliasError,
    PreviewAliasMintError,
    PreviewAliasService,
)
from app.services.preview_alias import alias_url as build_alias_url
from app.services.preview_composition import PreviewCompositionService
from app.services.sandbox import SandboxRuntimeError, get_sandbox_runtime
from app.services.workspace_services import WorkspaceServiceWorkspacePathError
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
    return PreviewService(session, settings, get_sandbox_runtime())


PREVIEW_SERVICE_DEP = Depends(get_preview_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


async def _get_owned_workspace(
    session: AsyncSession, workspace_id: UUID, user_id: UUID
) -> Workspace:
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None or workspace.owner_id != user_id:
        raise WorkspaceNotFoundError
    return workspace


async def get_preview_composition_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> PreviewCompositionService:
    return PreviewCompositionService(session, settings)


PREVIEW_COMPOSITION_SERVICE_DEP = Depends(get_preview_composition_service)


async def get_preview_alias_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> PreviewAliasService:
    return PreviewAliasService(session, settings)


PREVIEW_ALIAS_SERVICE_DEP = Depends(get_preview_alias_service)


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
    except SandboxRuntimeError as error:
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
    except SandboxRuntimeError as error:
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


# ---------------------------------------------------------------------------
# Epic 13: the composed preview descriptor + stable alias lifecycle.
# ---------------------------------------------------------------------------


@router.get("/composition", response_model=PreviewCompositionResponse)
async def get_preview_composition(
    workspace_id: UUID,
    check_proxy: bool = False,
    current_user: User = CURRENT_USER_DEP,
    service: PreviewCompositionService = PREVIEW_COMPOSITION_SERVICE_DEP,
) -> PreviewCompositionResponse:
    """`check_proxy=false` (the default, and every poll-loop call) performs **zero** sandbox
    round-trips. Only pass `check_proxy=true` on mount and on an explicit "Re-check" -- it reads
    each service's own repository files to look for a dev-proxy configuration.
    """
    try:
        composition = await service.compose(workspace_id, current_user.id, check_proxy=check_proxy)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except WorkspaceServiceWorkspacePathError:
        raise HTTPException(status_code=409, detail="Workspace sandbox is not ready") from None
    except SandboxRuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from None
    return PreviewCompositionResponse.model_validate(composition)


@router.post("/aliases/enable", response_model=PreviewAliasToggleResponse)
async def enable_preview_aliases(
    workspace_id: UUID,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    alias_service: PreviewAliasService = PREVIEW_ALIAS_SERVICE_DEP,
) -> PreviewAliasToggleResponse:
    try:
        workspace = await _get_owned_workspace(session, workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    try:
        workspace = await alias_service.enable(workspace)
    except PreviewAliasMintError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return PreviewAliasToggleResponse(preview_alias_enabled=workspace.preview_alias_enabled)


@router.post("/aliases/disable", response_model=PreviewAliasToggleResponse)
async def disable_preview_aliases(
    workspace_id: UUID,
    session: AsyncSession = DB_SESSION_DEP,
    current_user: User = CURRENT_USER_DEP,
    alias_service: PreviewAliasService = PREVIEW_ALIAS_SERVICE_DEP,
) -> PreviewAliasToggleResponse:
    try:
        workspace = await _get_owned_workspace(session, workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    workspace = await alias_service.disable(workspace)
    return PreviewAliasToggleResponse(preview_alias_enabled=workspace.preview_alias_enabled)


@router.post("/aliases/regenerate", response_model=PreviewAliasRegenerateResponse)
async def regenerate_preview_alias(
    workspace_id: UUID,
    payload: PreviewAliasRegenerateRequest,
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
    current_user: User = CURRENT_USER_DEP,
    alias_service: PreviewAliasService = PREVIEW_ALIAS_SERVICE_DEP,
) -> PreviewAliasRegenerateResponse:
    try:
        workspace = await _get_owned_workspace(session, workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    if not workspace.preview_alias_enabled:
        raise HTTPException(
            status_code=409, detail="Enable stable preview aliases for this workspace first"
        )

    name = "dev"
    if payload.custom_command_id is not None:
        command = await session.get(CustomCommand, payload.custom_command_id)
        if command is None or command.workspace_id != workspace.id:
            raise HTTPException(status_code=404, detail="Command not found")
        name = command.name

    try:
        row = await alias_service.regenerate(
            workspace, custom_command_id=payload.custom_command_id, name=name
        )
    except (InvalidPreviewAliasError, PreviewAliasMintError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from None

    return PreviewAliasRegenerateResponse(
        alias=row.alias,
        alias_url=build_alias_url(row.alias, base_url=settings.preview_proxy_base_url),
    )
