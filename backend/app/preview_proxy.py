import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as ws_connect

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import async_session_maker
from app.models import PreviewSession, User, Workspace, WorkspacePreviewAlias, WorkspaceService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    RuntimePreviewLink,
    get_daytona_runtime,
)
from app.services.preview_alias import (
    InvalidPreviewAliasGrantError,
    classify_proxy_host,
    decode_alias_grant,
    encode_alias_grant,
    is_alias_label,
    should_refresh_grant,
)
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    capability_from_host,
    decode_preview_capability,
    proxy_host_label,
)
from app.services.service_diagnostics import DiagnosticEvent, ServiceDiagnosticsRecorder

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
    # Epic 13: which host form resolved this target, and (for an alias) which alias label --
    # defaulted so every pre-Epic-13 construction site (including every existing test) keeps
    # working unmodified.
    kind: str = "capability"
    alias: str | None = None


class PreviewTargetNotRunningError(Exception):
    """The alias is valid (workspace has aliases enabled, the row exists and is enabled) but
    the service it names has no active preview session right now -- distinct from "unknown",
    and only ever raised *after* the caller has already passed the alias gate, so it is never
    an existence oracle for an unauthenticated caller."""


@dataclass(frozen=True)
class AliasResolution:
    workspace: Workspace
    custom_command_id: UUID | None
    preview_session: PreviewSession | None

    def require_target(self) -> PreviewProxyTarget:
        if self.preview_session is None:
            raise PreviewTargetNotRunningError("Preview service is not running")
        return PreviewProxyTarget(
            preview_session=self.preview_session,
            workspace=self.workspace,
            kind="alias",
        )


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

    async def resolve_alias(self, alias: str) -> AliasResolution:
        """Alias -> workspace + (if currently running) active preview session, in one query.

        `workspace_services` is joined with `IS NOT DISTINCT FROM` (not `=`) because the
        built-in row's `custom_command_id` is `NULL`, and plain `=` never matches `NULL`.
        Both the service and preview-session joins are outer joins: a workspace that enabled
        aliases before ever starting the service (no `workspace_services` row exists yet, or
        one exists but nothing is running) still resolves to a known workspace -- just with
        `preview_session=None`, which the caller maps to a 503 *after* the gate, not a 404.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(WorkspacePreviewAlias, Workspace, PreviewSession)
                .join(Workspace, Workspace.id == WorkspacePreviewAlias.workspace_id)
                .outerjoin(
                    WorkspaceService,
                    (WorkspaceService.workspace_id == WorkspacePreviewAlias.workspace_id)
                    & (
                        WorkspaceService.custom_command_id.is_not_distinct_from(
                            WorkspacePreviewAlias.custom_command_id
                        )
                    ),
                )
                .outerjoin(
                    PreviewSession,
                    (PreviewSession.service_id == WorkspaceService.id)
                    & (PreviewSession.status == "active"),
                )
                .where(
                    WorkspacePreviewAlias.alias == alias,
                    WorkspacePreviewAlias.enabled.is_(True),
                    Workspace.preview_alias_enabled.is_(True),
                )
                .order_by(PreviewSession.last_seen_at.desc())
                .limit(1)
            )
            row = result.first()
            if row is None:
                raise InvalidPreviewCapabilityError("Invalid preview alias")
            alias_row, workspace, preview = row
            if (
                workspace is None
                or workspace.owner_id is None
                or not workspace.daytona_sandbox_id
            ):
                raise InvalidPreviewCapabilityError("Preview workspace is unavailable")
            owner = await session.get(User, workspace.owner_id)
            if owner is None or not owner.is_active:
                raise InvalidPreviewCapabilityError("Preview owner is unavailable")
            session.expunge(workspace)
            if preview is not None:
                session.expunge(preview)
            return AliasResolution(
                workspace=workspace,
                custom_command_id=alias_row.custom_command_id,
                preview_session=preview,
            )

    async def verify_capability_for_workspace(
        self, capability_value: str, workspace_id: UUID
    ) -> bool:
        """Used by the alias gate's capability fallback: a capability is valid on its own
        crypto terms (signature, not expired) *and* names an active preview session that
        belongs to the alias's own workspace. Deliberately permissive at this granularity --
        any active session of the workspace authorises any alias of that workspace, which is
        what lets the browser holding the frontend's capability prime the API alias too."""
        try:
            capability = decode_preview_capability(
                capability_value, secret_key=self.settings.auth_secret_key
            )
        except InvalidPreviewCapabilityError:
            return False
        async with self.session_factory() as session:
            preview = await session.get(PreviewSession, capability.preview_session_id)
            if preview is None or preview.status != "active" or preview.port != capability.port:
                return False
            return preview.workspace_id == workspace_id


def create_preview_proxy_app(
    *,
    settings: Settings | None = None,
    resolver: PreviewProxyResolver | None = None,
    runtime: DaytonaRuntime | None = None,
    http_client: httpx.AsyncClient | None = None,
    diagnostics: ServiceDiagnosticsRecorder | None = None,
) -> FastAPI:
    proxy_settings = settings or get_settings()
    proxy_runtime = runtime or get_daytona_runtime()
    proxy_resolver = resolver or PreviewProxyResolver(proxy_settings)
    proxy_diagnostics = diagnostics or ServiceDiagnosticsRecorder(proxy_settings)
    owns_http_client = http_client is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.http_client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=None,
        )
        await proxy_diagnostics.start()
        yield
        await proxy_diagnostics.stop()
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
    app.state.diagnostics = proxy_diagnostics
    if http_client is not None:
        app.state.http_client = http_client

    async def proxy_http(request: Request, path: str = "") -> Response:
        original_host = request.headers.get("host", "")
        try:
            label = proxy_host_label(
                original_host, base_url=proxy_settings.daytona_preview_proxy_base_url
            )
        except InvalidPreviewCapabilityError:
            raise HTTPException(status_code=404, detail="Preview not found") from None

        grant_to_set: str | None = None
        query = request.url.query

        # Dispatch on shape: an alias always contains a hyphen (by construction -- see
        # `preview_alias.is_alias_label`), which no base32 capability label can. Anything that
        # is *not* alias-shaped goes through the exact same capability path as before this
        # epic -- unconditionally, with no separate pre-validation -- so a label's own
        # `resolve()` call (crypto signature + expiry) remains the only thing that decides
        # whether a non-alias-shaped host is a valid capability.
        if is_alias_label(label):
            alias = label
            try:
                resolution = await proxy_resolver.resolve_alias(alias)
            except InvalidPreviewCapabilityError:
                raise HTTPException(status_code=404, detail="Preview not found") from None

            query = _strip_query_param(query, proxy_settings.preview_alias_capability_query_param)
            is_preflight = _is_preflight(request)

            if not is_preflight:
                cookie_value = request.cookies.get(proxy_settings.preview_alias_cookie_name)
                capability_value = request.query_params.get(
                    proxy_settings.preview_alias_capability_query_param
                )
                gate = await _alias_gate(
                    proxy_resolver,
                    proxy_settings,
                    alias=alias,
                    workspace_id=resolution.workspace.id,
                    cookie_value=cookie_value,
                    capability_value=capability_value,
                )
                if not gate.authorized:
                    raise HTTPException(status_code=404, detail="Preview not found")
                grant_to_set = gate.grant_to_set

                if (
                    proxy_settings.preview_alias_strict_origin
                    and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE")
                ):
                    origin = request.headers.get("origin", "")
                    if not await _origin_belongs_to_workspace(
                        proxy_resolver, proxy_settings, origin, resolution.workspace.id
                    ):
                        raise HTTPException(status_code=404, detail="Preview not found")

                if grant_to_set is not None and gate.via_capability and _is_navigation(request):
                    location = request.url.path
                    if query:
                        location = f"{location}?{query}"
                    redirect = Response(status_code=303, headers={"location": location})
                    _set_alias_cookie(redirect, proxy_settings, grant_to_set)
                    return redirect

            try:
                target = resolution.require_target()
            except PreviewTargetNotRunningError:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Preview is unavailable",
                ) from None
        else:
            target = await _resolve_target(proxy_resolver, original_host)

        request_body = await request.body()
        try:
            preview_link, upstream_response = await _send_http_request(
                client=request.app.state.http_client,
                runtime=proxy_runtime,
                target=target,
                request=request,
                path=path,
                body=request_body,
                query=query,
                original_host=original_host,
            )
        except DaytonaRuntimeError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Preview is unavailable",
            ) from None

        _maybe_record_cors_diagnosis(
            proxy_diagnostics,
            request=request,
            target=target,
            upstream_response=upstream_response,
            self_origin=_proxy_origin(proxy_settings, original_host),
        )

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
        if grant_to_set is not None:
            _set_alias_cookie(response, proxy_settings, grant_to_set)
        return response

    app.add_api_route("/", proxy_http, methods=HTTP_METHODS)
    app.add_api_route("/{path:path}", proxy_http, methods=HTTP_METHODS)

    async def proxy_websocket(websocket: WebSocket, path: str = "") -> None:
        original_host = websocket.headers.get("host", "")
        try:
            label = proxy_host_label(
                original_host, base_url=proxy_settings.daytona_preview_proxy_base_url
            )
        except InvalidPreviewCapabilityError:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        if is_alias_label(label):
            # Alias WebSockets are cookie-only: Starlette cannot attach a Set-Cookie to a
            # pre-accept() close, and browsers do not reliably honour one from a handshake
            # response either. A capability-only handshake (no prior HTTP visit to this alias
            # to have set the cookie) closes 1008 -- documented as the one broken case
            # `alias_prime_url` exists to avoid.
            try:
                resolution = await proxy_resolver.resolve_alias(label)
            except InvalidPreviewCapabilityError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            cookie_value = websocket.cookies.get(proxy_settings.preview_alias_cookie_name)
            authorized = False
            if cookie_value:
                try:
                    decode_alias_grant(
                        cookie_value,
                        alias=label,
                        workspace_id=resolution.workspace.id,
                        secret_key=proxy_settings.auth_secret_key,
                        max_lifetime_seconds=proxy_settings.preview_alias_grant_max_lifetime_seconds,
                    )
                    authorized = True
                except InvalidPreviewAliasGrantError:
                    authorized = False
            if not authorized:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            try:
                target = resolution.require_target()
            except PreviewTargetNotRunningError:
                await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
                return
        else:
            try:
                target = await proxy_resolver.resolve(original_host)
            except InvalidPreviewCapabilityError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

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


# ---------------------------------------------------------------------------
# Epic 13: the alias gate. Runs only for an alias-classified host; the capability path above
# is completely untouched.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AliasGateResult:
    authorized: bool
    # A fresh grant to Set-Cookie with, or None when the existing cookie is still fresh enough
    # that no Set-Cookie is needed on this response.
    grant_to_set: str | None = None
    # True only when authorization came from a *freshly presented* capability (not a still-valid
    # cookie) -- that is the one case a navigation should 303-redirect to strip the capability
    # from the address bar, rather than just forwarding with a Set-Cookie attached.
    via_capability: bool = False


async def _alias_gate(
    resolver: PreviewProxyResolver,
    settings: Settings,
    *,
    alias: str,
    workspace_id: UUID,
    cookie_value: str | None,
    capability_value: str | None,
) -> _AliasGateResult:
    """Cookie first (a malformed/expired/mismatched cookie never invalidates an otherwise-valid
    capability -- it just falls through), then the capability fallback. Neither present or
    valid -> not authorized, and the caller must respond with the exact same 404 an unknown
    alias would get, never a 401 (no browser auth prompt, no existence oracle)."""
    if cookie_value:
        try:
            grant = decode_alias_grant(
                cookie_value,
                alias=alias,
                workspace_id=workspace_id,
                secret_key=settings.auth_secret_key,
                max_lifetime_seconds=settings.preview_alias_grant_max_lifetime_seconds,
            )
        except InvalidPreviewAliasGrantError:
            grant = None
        if grant is not None:
            if should_refresh_grant(grant, ttl_seconds=settings.preview_alias_cookie_ttl_seconds):
                fresh = encode_alias_grant(
                    alias=alias,
                    workspace_id=workspace_id,
                    secret_key=settings.auth_secret_key,
                    ttl_seconds=settings.preview_alias_cookie_ttl_seconds,
                )
                return _AliasGateResult(authorized=True, grant_to_set=fresh)
            return _AliasGateResult(authorized=True)

    if capability_value:
        if await resolver.verify_capability_for_workspace(capability_value, workspace_id):
            fresh = encode_alias_grant(
                alias=alias,
                workspace_id=workspace_id,
                secret_key=settings.auth_secret_key,
                ttl_seconds=settings.preview_alias_cookie_ttl_seconds,
            )
            return _AliasGateResult(authorized=True, grant_to_set=fresh, via_capability=True)

    return _AliasGateResult(authorized=False)


def _is_preflight(request: Request) -> bool:
    return (
        request.method == "OPTIONS"
        and request.headers.get("access-control-request-method") is not None
    )


def _is_navigation(request: Request) -> bool:
    if request.method not in ("GET", "HEAD"):
        return False
    if request.headers.get("sec-fetch-mode", "").lower() == "navigate":
        return True
    return "text/html" in request.headers.get("accept", "").lower()


def _strip_query_param(query: str, param_name: str) -> str:
    """Removes every ``param_name`` (or ``param_name=value``) segment from a raw query string
    by splitting on ``&`` alone -- deliberately not `urllib.parse.parse_qsl`/`urlencode`, which
    would renormalise percent-encoding and break an app with a non-standard query string."""
    if not query:
        return query
    kept = [
        segment
        for segment in query.split("&")
        if segment != param_name and not segment.startswith(f"{param_name}=")
    ]
    return "&".join(kept)


def _set_alias_cookie(response: Response, settings: Settings, value: str) -> None:
    response.set_cookie(
        key=settings.preview_alias_cookie_name,
        value=value,
        max_age=settings.preview_alias_cookie_ttl_seconds,
        httponly=True,
        # Always Secure: SameSite=None requires it, and every non-local deployment is required
        # (by `enforce_secure_defaults`) to be https or localhost, which modern browsers treat
        # as a secure context for the Secure cookie flag too.
        secure=True,
        samesite="none",
        path="/",
        # No `domain=` -- a host-only cookie, so the browser itself never sends one alias's
        # grant to a sibling alias, on top of the grant's own signature binding.
    )


async def _origin_belongs_to_workspace(
    resolver: PreviewProxyResolver,
    settings: Settings,
    origin: str,
    workspace_id: UUID,
) -> bool:
    """The strict-origin CSRF guard for non-safe methods on an alias host: the ``Origin`` must
    be another host under the proxy suffix that resolves to the *same* workspace (an alias or a
    capability of that workspace) -- never an arbitrary site, even though the grant cookie is
    `SameSite=None`."""
    if not origin or origin == "null":
        return False
    netloc = urlsplit(origin).netloc
    if not netloc:
        return False
    try:
        proxy_host = classify_proxy_host(netloc, base_url=settings.daytona_preview_proxy_base_url)
    except InvalidPreviewCapabilityError:
        return False
    try:
        if proxy_host.kind == "alias":
            resolution = await resolver.resolve_alias(proxy_host.label)
            return resolution.workspace.id == workspace_id
        target = await resolver.resolve(netloc)
        return target.workspace.id == workspace_id
    except InvalidPreviewCapabilityError:
        return False


async def _send_http_request(
    *,
    client: httpx.AsyncClient,
    runtime: DaytonaRuntime,
    target: PreviewProxyTarget,
    request: Request,
    path: str,
    body: bytes,
    query: str,
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
        query=query,
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
        query=query,
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
    query: str,
    original_host: str,
) -> httpx.Response:
    headers = _forward_headers(request.headers, REQUEST_HEADERS_TO_STRIP)
    headers["Accept-Encoding"] = "identity"
    headers["X-Daytona-Preview-Token"] = preview_link.token
    headers["X-Daytona-Skip-Preview-Warning"] = "true"
    headers["X-Forwarded-Host"] = original_host
    upstream_request = client.build_request(
        request.method,
        _upstream_url(preview_link.url, path, query),
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


# Epic 13: CORS diagnosis. Pure read of request/response headers -- never injects, removes, or
# reorders a header; `_response_headers` above is completely untouched.
_MAX_DIAGNOSTIC_PATH_LENGTH = 512


def _maybe_record_cors_diagnosis(
    diagnostics: ServiceDiagnosticsRecorder,
    *,
    request: Request,
    target: PreviewProxyTarget,
    upstream_response: httpx.Response,
    self_origin: str,
) -> None:
    service_id = target.preview_session.service_id
    if service_id is None:
        # A legacy (pre-Epic-11) preview session detected via the old single-port scan has no
        # owning service row to attribute a diagnosis to.
        return
    origin = request.headers.get("origin")
    if not origin:
        return
    if origin.rstrip("/").lower() == self_origin.rstrip("/").lower():
        return

    request_path = request.url.path[:_MAX_DIAGNOSTIC_PATH_LENGTH]
    base_kwargs = dict(
        workspace_id=target.workspace.id,
        service_id=service_id,
        preview_session_id=target.preview_session.id,
        origin=origin,
        request_path=request_path,
    )

    if _is_preflight(request):
        requested_method = request.headers.get("access-control-request-method")
        acao = upstream_response.headers.get("access-control-allow-origin")
        if not (200 <= upstream_response.status_code < 300):
            diagnostics.record(
                DiagnosticEvent(
                    **base_kwargs,
                    kind="cors_preflight_rejected",
                    required_header="Access-Control-Allow-Origin",
                    request_method=requested_method,
                    status_code=upstream_response.status_code,
                )
            )
            return
        if not acao:
            diagnostics.record(
                DiagnosticEvent(
                    **base_kwargs,
                    kind="cors_preflight_rejected",
                    required_header="Access-Control-Allow-Origin",
                    request_method=requested_method,
                    status_code=upstream_response.status_code,
                )
            )
            return
        requested_headers = {
            token.strip().lower()
            for token in request.headers.get("access-control-request-headers", "").split(",")
            if token.strip()
        }
        allowed_headers = {
            token.strip().lower()
            for token in upstream_response.headers.get(
                "access-control-allow-headers", ""
            ).split(",")
            if token.strip()
        }
        missing = requested_headers - allowed_headers
        if missing:
            diagnostics.record(
                DiagnosticEvent(
                    **base_kwargs,
                    kind="cors_preflight_rejected",
                    required_header="Access-Control-Allow-Headers",
                    request_method=requested_method,
                    status_code=upstream_response.status_code,
                    detail=f"missing: {', '.join(sorted(missing))}",
                )
            )
        return

    acao = upstream_response.headers.get("access-control-allow-origin")
    if not acao:
        diagnostics.record(
            DiagnosticEvent(
                **base_kwargs,
                kind="cors_missing_allow_origin",
                required_header="Access-Control-Allow-Origin",
                request_method=request.method,
                status_code=upstream_response.status_code,
            )
        )
    else:
        diagnostics.record(
            DiagnosticEvent(
                **base_kwargs,
                kind="cors_missing_allow_origin",
                resolve=True,
            )
        )


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
