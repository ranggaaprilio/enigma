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
from app.models import EditorSession, User
from app.schemas.editor import EditorSessionResponse, EditorStatusResponse
from app.services.editor import (
    EditorRuntimeError,
    EditorService,
    EditorSessionNotFoundError,
    EditorTokenError,
    EditorWorkspacePathError,
)
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/editor", tags=["editor"])

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
    "host",
    "cookie",
}


class EditorServiceProtocol(Protocol):
    async def start_or_reuse(self, workspace_id: UUID, user_id: UUID) -> EditorSession: ...

    async def status(self, workspace_id: UUID, user_id: UUID) -> EditorSession | None: ...

    async def stop(self, workspace_id: UUID, user_id: UUID) -> EditorSession: ...

    async def get_proxy_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        token: str,
    ) -> EditorSession: ...

    def upstream_http_url(self, editor_session: EditorSession, token: str, path: str) -> str: ...

    def upstream_ws_url(self, editor_session: EditorSession, token: str, path: str) -> str: ...


async def get_editor_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> EditorServiceProtocol:
    return EditorService(
        session=session,
        workspace_root=settings.workspace_root,
        public_url=settings.editor_public_url,
        container_workspace_root=settings.editor_container_workspace_root,
        token_ttl_seconds=settings.editor_token_ttl_seconds,
        secret_key=settings.auth_secret_key,
    )


EDITOR_SERVICE_DEP = Depends(get_editor_service)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def editor_error(error: Exception) -> HTTPException:
    if isinstance(error, EditorWorkspacePathError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace directory is not ready",
        )
    if isinstance(error, EditorRuntimeError):
        return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error))
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


@router.post("/start", response_model=EditorSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_editor_session(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: EditorServiceProtocol = EDITOR_SERVICE_DEP,
) -> EditorSession:
    try:
        return await service.start_or_reuse(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except (EditorWorkspacePathError, EditorRuntimeError) as error:
        raise editor_error(error) from None


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
    if editor_session is None:
        return EditorStatusResponse(status="inactive", session=None)
    return EditorStatusResponse(status=editor_session.status, session=editor_session)


@router.post("/stop", response_model=EditorSessionResponse)
async def stop_editor_session(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: EditorServiceProtocol = EDITOR_SERVICE_DEP,
) -> EditorSession:
    try:
        return await service.stop(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise not_found() from None
    except EditorSessionNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Editor session not found",
        ) from None


@router.api_route("/proxy/{editor_session_id}/{token}", methods=HTTP_PROXY_METHODS)
@router.api_route("/proxy/{editor_session_id}/{token}/{path:path}", methods=HTTP_PROXY_METHODS)
async def proxy_editor_http(
    workspace_id: UUID,
    editor_session_id: UUID,
    token: str,
    request: Request,
    path: str = "",
    current_user: User = CURRENT_USER_DEP,
    service: EditorServiceProtocol = EDITOR_SERVICE_DEP,
) -> Response:
    try:
        editor_session = await service.get_proxy_session(
            workspace_id,
            editor_session_id,
            current_user.id,
            token,
        )
    except (WorkspaceNotFoundError, EditorSessionNotFoundError, EditorTokenError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Editor not found",
        ) from None

    upstream_url = service.upstream_http_url(editor_session, token, path)
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
                detail="Editor is starting",
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


@router.websocket("/proxy/{editor_session_id}/{token}")
@router.websocket("/proxy/{editor_session_id}/{token}/{path:path}")
async def proxy_editor_websocket(
    websocket: WebSocket,
    workspace_id: UUID,
    editor_session_id: UUID,
    token: str,
    path: str = "",
) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        service = EditorService(
            session=session,
            workspace_root=settings.workspace_root,
            public_url=settings.editor_public_url,
            container_workspace_root=settings.editor_container_workspace_root,
            token_ttl_seconds=settings.editor_token_ttl_seconds,
            secret_key=settings.auth_secret_key,
        )
        try:
            editor_session = await service.get_proxy_session(
                workspace_id,
                editor_session_id,
                user.id,
                token,
            )
        except (WorkspaceNotFoundError, EditorSessionNotFoundError, EditorTokenError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        upstream_url = service.upstream_ws_url(editor_session, token, path)
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
