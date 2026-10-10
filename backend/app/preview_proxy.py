import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from starlette.background import BackgroundTask
from websockets.asyncio.client import connect as ws_connect

from app.core.config import Settings, get_settings
from app.core.lifecycle import RETRY_AFTER_SECONDS, AppLifecycle, install_sigterm_drain
from app.core.logging import configure_logging
from app.core.metrics import (
    metrics_token_authorized,
    preview_proxy_active_websockets,
    preview_proxy_requests_total,
    preview_proxy_upstream_errors_total,
)
from app.db.session import async_session_maker, engine
from app.models import (
    EditorSession,
    PreviewSession,
    User,
    Workspace,
    WorkspacePreviewAlias,
    WorkspaceService,
)
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    RuntimePreviewLink,
    get_daytona_runtime,
)
from app.services.editor_alias import is_editor_alias_label
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
    # The proxy sets its own; forwarding the client's too would let it poison the host an
    # upstream app builds absolute URLs from (it would see "evil.example, <real host>").
    "x-forwarded-host",
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


class PreviewUpstreamUnavailableError(Exception):
    """The sandbox's preview endpoint could not be reached (connect/read failure) -- typically
    a sandbox still waking from auto-stop."""


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


@dataclass(frozen=True)
class EditorProxyTarget:
    """Epic: embedded editor. The editor host family resolves to a workspace plus whichever
    code-server session is currently active for it -- there is no `PreviewSession` involved,
    because the editor is not a user service and never appears in the preview surface."""

    workspace: Workspace
    editor_session: EditorSession | None

    def require_session(self) -> EditorSession:
        if self.editor_session is None:
            raise PreviewTargetNotRunningError("Editor is not running")
        return self.editor_session


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

    async def resolve_editor_alias(self, alias: str) -> EditorProxyTarget:
        """Editor alias -> workspace + its active code-server session, in one query.

        Mirrors `resolve_alias`'s posture exactly: an unknown alias is indistinguishable from an
        unauthorised one (both end as a 404 at the call site), while a *known* alias whose editor
        simply is not running resolves with `editor_session=None` so the caller can answer 503 --
        and it only ever does so after the gate has already authorised the request, so it is
        never an existence oracle.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(Workspace, EditorSession)
                .outerjoin(
                    EditorSession,
                    (EditorSession.workspace_id == Workspace.id)
                    & (EditorSession.status == "active"),
                )
                .where(Workspace.editor_alias == alias)
                .order_by(EditorSession.last_active_at.desc())
                .limit(1)
            )
            row = result.first()
            if row is None:
                raise InvalidPreviewCapabilityError("Invalid editor alias")
            workspace, editor_session = row
            if (
                workspace is None
                or workspace.owner_id is None
                or not workspace.daytona_sandbox_id
            ):
                raise InvalidPreviewCapabilityError("Editor workspace is unavailable")
            owner = await session.get(User, workspace.owner_id)
            if owner is None or not owner.is_active:
                raise InvalidPreviewCapabilityError("Editor owner is unavailable")
            session.expunge(workspace)
            if editor_session is not None:
                session.expunge(editor_session)
            return EditorProxyTarget(workspace=workspace, editor_session=editor_session)

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
    # Without an injected resolver the app resolves through the shared engine, so it owns it.
    owns_engine = resolver is None
    # Its own drain flag, not the API's `lifecycle` singleton: the proxy is a separate process,
    # and a shared flag would let one proxy app's shutdown mark every app in the process draining.
    proxy_lifecycle = AppLifecycle()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.http_client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=None,
        )
        await proxy_diagnostics.start()
        # SIGTERM flips `/ready` to 503 and keeps serving for the drain delay so the load
        # balancer moves away; uvicorn then closes open WebSockets (1012, clients reconnect
        # through another replica) and waits out in-flight HTTP streams.
        restore_sigterm = install_sigterm_drain(
            asyncio.get_running_loop(),
            proxy_settings.shutdown_drain_delay_seconds,
            state=proxy_lifecycle,
        )
        yield
        proxy_lifecycle.begin_drain()
        try:
            await proxy_diagnostics.stop()
            if owns_http_client:
                await app.state.http_client.aclose()
            await proxy_runtime.close()
            if owns_engine:
                await engine.dispose()
        finally:
            restore_sigterm()

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
    app.state.lifecycle = proxy_lifecycle
    if http_client is not None:
        app.state.http_client = http_client

    # Probes and metrics answer only on a host that is *not* a preview host (the bare proxy
    # hostname, or the pod IP a load balancer probes). Every preview host routes every path to
    # the previewed app, so its own `/health` or `/metrics` is never shadowed.
    async def check_database() -> str:
        try:
            async with proxy_resolver.session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return "error"
        return "ok"

    async def live(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def ready(_request: Request) -> Response:
        if proxy_lifecycle.draining:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "draining", "database": "skipped"},
            )
        database = await check_database()
        if database != "ok":
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "database": database},
            )
        return JSONResponse({"status": "ready", "database": database})

    def unauthorized() -> Response:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    def scrape_authorized(request: Request) -> bool:
        return metrics_token_authorized(
            request.headers.get("authorization", ""), proxy_settings.metrics_token
        )

    async def health(request: Request) -> Response:
        # Token-gated like /metrics: each call is a live Daytona API request, and the bare
        # proxy host is reachable from the internet. /live and /ready stay open for the LB.
        if not scrape_authorized(request):
            return unauthorized()
        database = await check_database()
        try:
            daytona = await proxy_runtime.health()
        except Exception:
            daytona = "error"
        healthy = database == "ok" and daytona == "ok"
        return JSONResponse(
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ok" if healthy else "degraded",
                "database": database,
                "daytona": daytona,
            },
        )

    async def metrics(request: Request) -> Response:
        if not scrape_authorized(request):
            return unauthorized()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    probes = {"live": live, "ready": ready, "health": health, "metrics": metrics}

    async def proxy_http(request: Request, path: str = "") -> Response:
        original_host = request.headers.get("host", "")
        try:
            label = proxy_host_label(
                original_host, base_url=proxy_settings.daytona_preview_proxy_base_url
            )
        except InvalidPreviewCapabilityError:
            probe = probes.get(path) if request.method in ("GET", "HEAD") else None
            if probe is None:
                raise HTTPException(status_code=404, detail="Preview not found") from None
            return await probe(request)
        preview_proxy_requests_total.labels(protocol="http").inc()

        grant_to_set: str | None = None
        editor_grant_to_set: str | None = None
        # `target` is set only by the two preview families -- the editor has no `PreviewSession`
        # to carry, which is why CORS diagnosis is skipped for it. `is_editor` says so
        # explicitly rather than leaving that meaning implicit in `target is None`.
        target: PreviewProxyTarget | None = None
        is_editor = False
        query = request.url.query

        # Dispatch on shape: an alias always contains a hyphen (by construction -- see
        # `preview_alias.is_alias_label`), which no base32 capability label can. Anything that
        # is *not* alias-shaped goes through the exact same capability path as before this
        # epic -- unconditionally, with no separate pre-validation -- so a label's own
        # `resolve()` call (crypto signature + expiry) remains the only thing that decides
        # whether a non-alias-shaped host is a valid capability.
        #
        # The editor family (`editor-<12 hex>`) is alias-shaped too, so it is checked first.
        # The two can never overlap: `preview_alias.validate_alias` refuses to mint anything
        # matching `is_editor_alias_label`.
        if is_editor_alias_label(label):
            is_editor = True
            try:
                editor_target = await proxy_resolver.resolve_editor_alias(label)
            except InvalidPreviewCapabilityError:
                raise HTTPException(status_code=404, detail="Preview not found") from None

            query = _strip_query_param(query, proxy_settings.preview_alias_capability_query_param)

            if not _is_preflight(request):
                gate = _editor_gate(
                    proxy_settings,
                    alias=label,
                    workspace_id=editor_target.workspace.id,
                    cookie_value=request.cookies.get(proxy_settings.preview_alias_cookie_name),
                    grant_value=request.query_params.get(
                        proxy_settings.preview_alias_capability_query_param
                    ),
                )
                if not gate.authorized:
                    raise HTTPException(status_code=404, detail="Preview not found")
                editor_grant_to_set = gate.grant_to_set

                if (
                    proxy_settings.preview_alias_strict_origin
                    and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE")
                ):
                    origin = request.headers.get("origin", "")
                    if not await _origin_belongs_to_workspace(
                        proxy_resolver, proxy_settings, origin, editor_target.workspace.id
                    ):
                        raise HTTPException(status_code=404, detail="Preview not found")

            # Deliberately no 303 redirect here, unlike the preview alias path: that redirect
            # exists only to strip the token out of the address bar, and the editor is loaded in
            # an iframe which has none. Forwarding directly also means the very first navigation
            # already renders code-server.
            try:
                editor_session = editor_target.require_session()
            except PreviewTargetNotRunningError:
                # Carries the cookie even though it failed -- see `_editor_unavailable`.
                return _editor_unavailable(request, proxy_settings, editor_grant_to_set)
            upstream_workspace = editor_target.workspace
            upstream_port = editor_session.bind_port
        elif is_alias_label(label):
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
                return _preview_unavailable(request)
            upstream_workspace = target.workspace
            upstream_port = target.preview_session.port
        else:
            target = await _resolve_target(proxy_resolver, original_host)
            upstream_workspace = target.workspace
            upstream_port = target.preview_session.port

        request_body = await request.body()
        try:
            preview_link, upstream_response = await _send_http_request(
                client=request.app.state.http_client,
                runtime=proxy_runtime,
                workspace=upstream_workspace,
                port=upstream_port,
                request=request,
                path=path,
                body=request_body,
                query=query,
                original_host=original_host,
            )
        except (DaytonaRuntimeError, PreviewUpstreamUnavailableError):
            preview_proxy_upstream_errors_total.labels(protocol="http").inc()
            if is_editor:
                return _editor_unavailable(request, proxy_settings, editor_grant_to_set)
            return _preview_unavailable(request)

        if not is_editor and target is not None:
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
            # Only for the editor: code-server is *meant* to be framed by the dashboard, so an
            # upstream `X-Frame-Options`/`frame-ancestors` would break the surface outright.
            # The preview path is left byte-identical -- a previewed app keeps whatever
            # clickjacking posture its author chose.
            strip_frame_headers=is_editor,
        )
        if grant_to_set is not None:
            _set_alias_cookie(response, proxy_settings, grant_to_set)
        if editor_grant_to_set is not None:
            _set_editor_cookie(response, proxy_settings, editor_grant_to_set)
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
        preview_proxy_requests_total.labels(protocol="websocket").inc()

        if is_editor_alias_label(label):
            # Same cookie-only rule as the preview alias below. In practice this is never the
            # broken case: code-server's socket only opens after the workbench document has
            # loaded over HTTP, and that navigation is what set the cookie.
            try:
                editor_target = await proxy_resolver.resolve_editor_alias(label)
            except InvalidPreviewCapabilityError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            gate = _editor_gate(
                proxy_settings,
                alias=label,
                workspace_id=editor_target.workspace.id,
                cookie_value=websocket.cookies.get(proxy_settings.preview_alias_cookie_name),
                grant_value=None,
            )
            if not gate.authorized:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            try:
                editor_session = editor_target.require_session()
            except PreviewTargetNotRunningError:
                await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
                return
            upstream_workspace = editor_target.workspace
            upstream_port = editor_session.bind_port
        elif is_alias_label(label):
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
            upstream_workspace = target.workspace
            upstream_port = target.preview_session.port
        else:
            try:
                target = await proxy_resolver.resolve(original_host)
            except InvalidPreviewCapabilityError:
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            upstream_workspace = target.workspace
            upstream_port = target.preview_session.port

        try:
            upstream = await _connect_websocket(
                runtime=proxy_runtime,
                workspace=upstream_workspace,
                port=upstream_port,
                websocket=websocket,
                path=path,
                original_host=original_host,
            )
        except Exception:
            preview_proxy_upstream_errors_total.labels(protocol="websocket").inc()
            await websocket.close(code=status.WS_1013_TRY_AGAIN_LATER)
            return

        await websocket.accept(subprotocol=upstream.subprotocol)
        preview_proxy_active_websockets.inc()
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
            preview_proxy_active_websockets.dec()
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


def _editor_gate(
    settings: Settings,
    *,
    alias: str,
    workspace_id: UUID,
    cookie_value: str | None,
    grant_value: str | None,
) -> _AliasGateResult:
    """The editor's gate. Structurally the same as `_alias_gate`, with one deliberate
    difference: both the cookie *and* the query-param credential are alias **grants**, not
    capabilities.

    A preview alias is primed by a capability, which is bound to a running `PreviewSession`.
    The editor has no such session to bind to, so the backend mints the grant directly (see
    `EditorService.prime_url`) with a very short TTL and hands it to the browser exactly once,
    in the iframe's `src`. Because `decode_alias_grant` binds every grant to a specific
    `(alias, workspace_id)` pair and is domain-separated from the platform session token, a
    grant minted for one workspace's editor cannot authorise another's -- nor can a preview
    alias's grant authorise an editor host, since the alias in the signature would not match.
    """
    for candidate, from_cookie in ((cookie_value, True), (grant_value, False)):
        if not candidate:
            continue
        try:
            grant = decode_alias_grant(
                candidate,
                alias=alias,
                workspace_id=workspace_id,
                secret_key=settings.auth_secret_key,
                max_lifetime_seconds=settings.preview_alias_grant_max_lifetime_seconds,
            )
        except InvalidPreviewAliasGrantError:
            # A stale cookie must never veto an otherwise-valid grant in the query string --
            # that is the exact case a reloaded iframe hits after the cookie has aged out.
            continue
        if from_cookie and not should_refresh_grant(
            grant, ttl_seconds=settings.preview_alias_cookie_ttl_seconds
        ):
            return _AliasGateResult(authorized=True)
        fresh = encode_alias_grant(
            alias=alias,
            workspace_id=workspace_id,
            secret_key=settings.auth_secret_key,
            ttl_seconds=settings.preview_alias_cookie_ttl_seconds,
        )
        return _AliasGateResult(
            authorized=True, grant_to_set=fresh, via_capability=not from_cookie
        )
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


def _set_editor_cookie(response: Response, settings: Settings, value: str) -> None:
    """Same cookie as `_set_alias_cookie`, plus `Partitioned`.

    Starlette's `set_cookie` has no `partitioned=` argument, hence the raw header. The attribute
    is not optional here: the editor cookie is always set on a **cross-site iframe**, and an
    unpartitioned `SameSite=None` cookie is dropped outright by browsers that block third-party
    cookies -- which would strand the editor in a permanent 404 loop the moment the one-shot
    grant in the URL was consumed. Partitioned (CHIPS) scopes the cookie to the embedding
    top-level site, which is exactly the lifetime the editor wants. The trade-off is that a
    first-party tab is a different partition, so "Open in new tab" must load the prime URL too.
    """
    parts = [
        f"{settings.preview_alias_cookie_name}={value}",
        "Path=/",
        f"Max-Age={settings.preview_alias_cookie_ttl_seconds}",
        "HttpOnly",
        "Secure",
        "SameSite=None",
        "Partitioned",
    ]
    # No `Domain=` -- host-only, so one editor host's grant is never sent to a sibling, on top
    # of the grant's own signature binding.
    response.raw_headers.append((b"set-cookie", "; ".join(parts).encode("latin-1")))


def _editor_unavailable(
    request: Request, settings: Settings, grant_to_set: str | None
) -> Response:
    """A 503 for the editor that still sets the grant cookie.

    The grant in the URL is one-shot: the dashboard mints it per response and the iframe's `src`
    is fixed for the life of the frame. If a transient upstream failure on the very first
    (priming) navigation returned a bare 503, the browser would have spent its grant without
    getting a cookie, and every retry inside the frame would 404 until the panel was reloaded.
    Attaching the cookie here means a retry succeeds as soon as the sandbox comes back -- which
    the waking page's own reload now does on its own.
    """
    response = _unavailable(request, "Editor is unavailable")
    if grant_to_set is not None:
        _set_editor_cookie(response, settings, grant_to_set)
    return response


def _preview_unavailable(request: Request) -> Response:
    return _unavailable(request, "Preview is unavailable")


# Served in place of a 503 to a navigation: the iframe (or tab) cannot see a status code, so a
# bare JSON body would strand it on an error. The page reloads itself with backoff (2s doubling
# to 30s, reset after a quiet minute) until the sandbox answers.
_WAKING_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Waking preview…</title>
<noscript><meta http-equiv="refresh" content="5"></noscript>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f7f7f4;color:#26251e;font:14px/1.5 Inter,system-ui,sans-serif;text-align:center}
p{margin:0}p+p{color:#6b6a63;margin-top:4px}
</style>
</head>
<body>
<main><p>Waking preview…</p><p>The sandbox is starting. This page retries on its own.</p></main>
<script>
(function () {
  var key = "enigma-preview-waking", attempt = 0;
  try {
    var last = JSON.parse(sessionStorage.getItem(key) || "null");
    if (last && Date.now() - last.at < 60000) attempt = last.attempt + 1;
    sessionStorage.setItem(key, JSON.stringify({ attempt: attempt, at: Date.now() }));
  } catch (error) {}
  setTimeout(function () { location.reload(); }, Math.min(2000 * Math.pow(2, attempt), 30000));
})();
</script>
</body>
</html>
"""


def _unavailable(request: Request, detail: str) -> Response:
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS), "Cache-Control": "no-store"}
    if _is_navigation(request):
        return HTMLResponse(
            _WAKING_PAGE, status_code=status.HTTP_503_SERVICE_UNAVAILABLE, headers=headers
        )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": detail},
        headers=headers,
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
        if proxy_host.kind == "editor":
            editor_target = await resolver.resolve_editor_alias(proxy_host.label)
            return editor_target.workspace.id == workspace_id
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
    workspace: Workspace,
    port: int,
    request: Request,
    path: str,
    body: bytes,
    query: str,
    original_host: str,
) -> tuple[RuntimePreviewLink, httpx.Response]:
    # Takes `(workspace, port)` rather than a target so the preview families and the editor --
    # which has no `PreviewSession` to carry a port on -- share one forwarding path.
    preview_link = await runtime.preview_link(workspace, port)
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
    preview_link = await runtime.preview_link(workspace, port)
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
    except httpx.HTTPError as error:
        raise PreviewUpstreamUnavailableError from error


async def _connect_websocket(
    *,
    runtime: DaytonaRuntime,
    workspace: Workspace,
    port: int,
    websocket: WebSocket,
    path: str,
    original_host: str,
):
    last_error: Exception | None = None
    for _attempt in range(2):
        preview_link = await runtime.preview_link(workspace, port)
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


# Matches one `frame-ancestors ...` directive of a CSP, with or without a trailing `;`.
_FRAME_ANCESTORS_PATTERN = re.compile(r"\s*frame-ancestors\b[^;]*;?", re.IGNORECASE)
_CSP_HEADERS = {"content-security-policy", "content-security-policy-report-only"}


def _without_frame_ancestors(value: str) -> str:
    return _FRAME_ANCESTORS_PATTERN.sub("", value).strip().strip(";").strip()


def _response_headers(
    response: httpx.Response,
    *,
    preview_link: RuntimePreviewLink,
    proxy_origin: str,
    strip_frame_headers: bool = False,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    upstream = urlsplit(preview_link.url)
    upstream_origin = f"{upstream.scheme}://{upstream.netloc}"
    for key, value in response.headers.multi_items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if strip_frame_headers:
            # Editor only. Everything else about the header set is left exactly as the upstream
            # sent it -- only the two headers that would refuse the embed are touched, and only
            # their framing directive, so a CSP's other directives survive.
            if lower_key == "x-frame-options":
                continue
            if lower_key in _CSP_HEADERS:
                value = _without_frame_ancestors(value)
                if not value:
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
