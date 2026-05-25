import asyncio
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.asyncio.client import connect as ws_connect

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.security import verify_session_token
from app.db.session import async_session_maker, get_db_session
from app.models import PreviewSession, User
from app.schemas.preview import PreviewDetectResponse, PreviewSessionResponse
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

HTTP_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
HOP_BY_HOP_REQUEST_HEADERS = HOP_BY_HOP_RESPONSE_HEADERS | {
    "accept-encoding",
    "cookie",
    "host",
}


class PreviewServiceProtocol(Protocol):
    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[PreviewSession]:
        ...

    async def detect(self, workspace_id: UUID, user_id: UUID) -> PreviewSession: ...

    async def refresh(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession: ...

    async def stop(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession: ...

    async def get_proxy_session(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession: ...

    def upstream_http_url(self, preview_session: PreviewSession, path: str) -> str: ...

    def upstream_ws_url(self, preview_session: PreviewSession, path: str) -> str: ...


async def get_preview_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> PreviewServiceProtocol:
    return PreviewService(session=session, workspace_root=settings.workspace_root)


PREVIEW_SERVICE_DEP = Depends(get_preview_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def preview_error(error: Exception) -> HTTPException:
    if isinstance(error, PreviewWorkspacePathError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace directory is not ready",
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


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
        preview_session = await service.detect(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except PreviewNotFoundError:
        return PreviewDetectResponse(status="inactive", session=None)
    except PreviewWorkspacePathError as error:
        raise preview_error(error) from None
    return PreviewDetectResponse(status="active", session=preview_session)


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preview session not found",
        ) from None


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preview session not found",
        ) from None


@router.api_route("/proxy/{preview_session_id}", methods=HTTP_PROXY_METHODS)
@router.api_route("/proxy/{preview_session_id}/{path:path}", methods=HTTP_PROXY_METHODS)
async def proxy_preview_http(
    workspace_id: UUID,
    preview_session_id: UUID,
    request: Request,
    path: str = "",
    current_user: User = CURRENT_USER_DEP,
    service: PreviewServiceProtocol = PREVIEW_SERVICE_DEP,
) -> Response:
    try:
        preview_session = await service.get_proxy_session(
            workspace_id,
            preview_session_id,
            current_user.id,
        )
    except (WorkspaceNotFoundError, PreviewSessionNotFoundError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preview not found",
        ) from None

    upstream_url = service.upstream_http_url(preview_session, path)
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_REQUEST_HEADERS
    }
    headers["Accept-Encoding"] = "identity"
    async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
        try:
            upstream_response = await client.request(
                request.method,
                upstream_url,
                content=await request.body(),
                headers=headers,
            )
        except httpx.ConnectError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Preview is unavailable",
            ) from None

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in HOP_BY_HOP_RESPONSE_HEADERS
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


@router.websocket("/proxy/{preview_session_id}")
@router.websocket("/proxy/{preview_session_id}/{path:path}")
async def proxy_preview_websocket(
    websocket: WebSocket,
    workspace_id: UUID,
    preview_session_id: UUID,
    path: str = "",
) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        service = PreviewService(session=session, workspace_root=settings.workspace_root)
        try:
            preview_session = await service.get_proxy_session(
                workspace_id,
                preview_session_id,
                user.id,
            )
        except (WorkspaceNotFoundError, PreviewSessionNotFoundError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        upstream_url = service.upstream_ws_url(preview_session, path)
        if websocket.url.query:
            upstream_url = f"{upstream_url}?{websocket.url.query}"

    await websocket.accept()
    try:
        async with ws_connect(upstream_url) as upstream:
            browser_to_upstream = asyncio.create_task(_browser_to_upstream(websocket, upstream))
            upstream_to_browser = asyncio.create_task(_upstream_to_browser(websocket, upstream))
            done, pending = await asyncio.wait(
                {browser_to_upstream, upstream_to_browser},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except Exception:
        await websocket.close()


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


async def _browser_to_upstream(websocket: WebSocket, upstream) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if data := message.get("bytes"):
            await upstream.send(data)
            continue
        if text := message.get("text"):
            await upstream.send(text)


async def _upstream_to_browser(websocket: WebSocket, upstream) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)
