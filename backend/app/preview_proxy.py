import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as ws_connect

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import async_session_maker
from app.models import PreviewSession, User, Workspace
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    RuntimePreviewLink,
    get_daytona_runtime,
)
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    capability_from_host,
    decode_preview_capability,
)

HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_HEADERS_TO_STRIP = HOP_BY_HOP_HEADERS | {
    "accept-encoding",
    "host",
    "x-daytona-preview-token",
    "x-daytona-skip-preview-warning",
}
WEBSOCKET_HEADERS_TO_STRIP = REQUEST_HEADERS_TO_STRIP | {
    "origin",
    "sec-websocket-accept",
    "sec-websocket-extensions",
    "sec-websocket-key",
    "sec-websocket-protocol",
    "sec-websocket-version",
}


@dataclass(frozen=True)
class PreviewProxyTarget:
    preview_session: PreviewSession
    workspace: Workspace


class PreviewProxyResolver:
    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Any] = async_session_maker,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def resolve(self, host: str) -> PreviewProxyTarget:
        capability_value = capability_from_host(
            host,
            base_url=self.settings.daytona_preview_proxy_base_url,
        )
        capability = decode_preview_capability(
            capability_value,
            secret_key=self.settings.auth_secret_key,
        )
        async with self.session_factory() as session:
            preview = await session.get(PreviewSession, capability.preview_session_id)
            if (
                preview is None
                or preview.status != "active"
                or preview.port != capability.port
            ):
                raise InvalidPreviewCapabilityError("Preview session is unavailable")
            workspace = await session.get(Workspace, preview.workspace_id)
            if (
                workspace is None
                or workspace.owner_id is None
                or not workspace.daytona_sandbox_id
            ):
                raise InvalidPreviewCapabilityError("Preview workspace is unavailable")
            owner = await session.get(User, workspace.owner_id)
            if owner is None or not owner.is_active:
                raise InvalidPreviewCapabilityError("Preview owner is unavailable")
            session.expunge(preview)
            session.expunge(workspace)
        return PreviewProxyTarget(preview_session=preview, workspace=workspace)


def create_preview_proxy_app(
    *,
    settings: Settings | None = None,
    resolver: PreviewProxyResolver | None = None,
    runtime: DaytonaRuntime | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    proxy_settings = settings or get_settings()
    proxy_runtime = runtime or get_daytona_runtime()
    proxy_resolver = resolver or PreviewProxyResolver(proxy_settings)
    owns_http_client = http_client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.http_client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=None,
        )
        yield
        if owns_http_client:
            await app.state.http_client.aclose()
        await proxy_runtime.close()

    configure_logging()
    app = FastAPI(
        title=f"{proxy_settings.app_name} Preview Proxy",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = proxy_settings
    app.state.resolver = proxy_resolver
    app.state.runtime = proxy_runtime
    if http_client is not None:
        app.state.http_client = http_client

    async def proxy_http(request: Request, path: str = "") -> StreamingResponse:
        target = await _resolve_target(proxy_resolver, request.headers.get("host", ""))
        original_host = request.headers.get("host", "")
        request_body = await request.body()
        try:
            preview_link, upstream_response = await _send_http_request(
                client=request.app.state.http_client,
                runtime=proxy_runtime,
                target=target,
                request=request,
                path=path,
                body=request_body,
                original_host=original_host,
            )
        except DaytonaRuntimeError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Preview is unavailable",
            ) from None
        response = StreamingResponse(
            _response_body(upstream_response),
            status_code=upstream_response.status_code,
            background=BackgroundTask(upstream_response.aclose),
        )
        response.raw_headers = _response_headers(
            upstream_response,
            preview_link=preview_link,
            proxy_origin=_proxy_origin(proxy_settings, original_host),
        )
        return response

    app.add_api_route("/", proxy_http, methods=HTTP_METHODS)
    app.add_api_route("/{path:path}", proxy_http, methods=HTTP_METHODS)

    async def proxy_websocket(websocket: WebSocket, path: str = "") -> None:
        try:
            target = await proxy_resolver.resolve(websocket.headers.get("host", ""))
        except InvalidPreviewCapabilityError:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        original_host = websocket.headers.get("host", "")
        try:
            upstream = await _connect_websocket(
                runtime=proxy_runtime,
                target=target,
                websocket=websocket,
                path=path,
                original_host=original_host,
            )
        except Exception:
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return

        await websocket.accept(subprotocol=upstream.subprotocol)
        try:
            browser_to_upstream = asyncio.create_task(
                _browser_to_upstream(websocket, upstream)
            )
            upstream_to_browser = asyncio.create_task(
                _upstream_to_browser(websocket, upstream)
            )
            done, pending = await asyncio.wait(
                {browser_to_upstream, upstream_to_browser},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except Exception:
            pass
        finally:
            await upstream.close()
            try:
                await websocket.close()
            except RuntimeError:
                pass

    app.add_api_websocket_route("/", proxy_websocket)
    app.add_api_websocket_route("/{path:path}", proxy_websocket)
    return app


async def _resolve_target(
    resolver: PreviewProxyResolver,
    host: str,
) -> PreviewProxyTarget:
    try:
        return await resolver.resolve(host)
    except InvalidPreviewCapabilityError:
        raise HTTPException(status_code=404, detail="Preview not found") from None


async def _send_http_request(
    *,
    client: httpx.AsyncClient,
    runtime: DaytonaRuntime,
    target: PreviewProxyTarget,
    request: Request,
    path: str,
    body: bytes,
    original_host: str,
) -> tuple[RuntimePreviewLink, httpx.Response]:
    preview_link = await runtime.preview_link(
        target.workspace,
        target.preview_session.port,
    )
    response = await _send_http_attempt(
        client,
        preview_link,
        request=request,
        path=path,
        body=body,
        original_host=original_host,
    )
    if response.status_code not in {401, 403}:
        return preview_link, response

    await response.aclose()
    preview_link = await runtime.preview_link(
        target.workspace,
        target.preview_session.port,
    )
    response = await _send_http_attempt(
        client,
        preview_link,
        request=request,
        path=path,
        body=body,
        original_host=original_host,
    )
    return preview_link, response


async def _send_http_attempt(
    client: httpx.AsyncClient,
    preview_link: RuntimePreviewLink,
    *,
    request: Request,
    path: str,
    body: bytes,
    original_host: str,
) -> httpx.Response:
    headers = _forward_headers(request.headers, REQUEST_HEADERS_TO_STRIP)
    headers["Accept-Encoding"] = "identity"
    headers["X-Daytona-Preview-Token"] = preview_link.token
    headers["X-Daytona-Skip-Preview-Warning"] = "true"
    headers["X-Forwarded-Host"] = original_host
    upstream_request = client.build_request(
        request.method,
        _upstream_url(preview_link.url, path, request.url.query),
        headers=headers,
        content=body,
    )
    try:
        return await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Preview is unavailable",
        ) from None


async def _connect_websocket(
    *,
    runtime: DaytonaRuntime,
    target: PreviewProxyTarget,
    websocket: WebSocket,
    path: str,
    original_host: str,
):
    last_error: Exception | None = None
    for _attempt in range(2):
        preview_link = await runtime.preview_link(
            target.workspace,
            target.preview_session.port,
        )
        headers = _forward_headers(websocket.headers, WEBSOCKET_HEADERS_TO_STRIP)
        headers["X-Daytona-Preview-Token"] = preview_link.token
        headers["X-Daytona-Skip-Preview-Warning"] = "true"
        headers["X-Forwarded-Host"] = original_host
        try:
            return await ws_connect(
                _upstream_url(
                    preview_link.url,
                    path,
                    websocket.url.query,
                    websocket=True,
                ),
                origin=websocket.headers.get("origin"),
                subprotocols=websocket.scope.get("subprotocols") or None,
                additional_headers=headers,
                proxy=None,
            )
        except Exception as error:
            last_error = error
    assert last_error is not None
    raise last_error


def _forward_headers(headers, blocked: set[str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in blocked
    }


def _upstream_url(
    base_url: str,
    path: str,
    query: str,
    *,
    websocket: bool = False,
) -> str:
    parsed = urlsplit(base_url)
    scheme = parsed.scheme
    if websocket:
        scheme = "wss" if scheme == "https" else "ws"
    request_path = f"/{path}" if path else "/"
    base_path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, f"{base_path}{request_path}", query, ""))


def _response_headers(
    response: httpx.Response,
    *,
    preview_link: RuntimePreviewLink,
    proxy_origin: str,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    upstream = urlsplit(preview_link.url)
    upstream_origin = f"{upstream.scheme}://{upstream.netloc}"
    for key, value in response.headers.multi_items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if lower_key == "location" and value.startswith(upstream_origin):
            value = f"{proxy_origin}{value[len(upstream_origin):]}"
        headers.append((key.encode("latin-1"), value.encode("latin-1")))
    return headers


async def _response_body(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.is_stream_consumed:
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


def _proxy_origin(settings: Settings, host: str) -> str:
    return f"{urlsplit(settings.daytona_preview_proxy_base_url).scheme}://{host}"


async def _browser_to_upstream(websocket: WebSocket, upstream) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("bytes") is not None:
            await upstream.send(message["bytes"])
        elif message.get("text") is not None:
            await upstream.send(message["text"])


async def _upstream_to_browser(websocket: WebSocket, upstream) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await websocket.send_bytes(message)
        else:
            await websocket.send_text(message)


app = create_preview_proxy_app()
