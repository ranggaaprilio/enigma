"""Epic 07: a stateless, horizontally scalable preview proxy.

Covers the replica-independence guarantees (no module-level state, identical signature checks
on independently built apps), the host-gated probes, the drain lifecycle, metrics, the
upstream-pinning (no SSRF / cross-host) invariants and the "waking preview" page.
"""

import ast
import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.websockets import WebSocketDisconnect

from app import preview_proxy as preview_proxy_module
from app.core.config import Settings
from app.core.lifecycle import AppLifecycle, lifecycle
from app.models import PreviewSession, User, Workspace
from app.preview_proxy import (
    AliasResolution,
    PreviewProxyResolver,
    PreviewProxyTarget,
    create_preview_proxy_app,
)
from app.services.preview_alias import encode_alias_grant
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    create_preview_proxy_url,
)
from app.services.sandbox import RuntimePreviewLink, SandboxRuntimeError

BASE_URL = "http://localhost:8001"
PROBE_HOST = "localhost:8001"
ALIAS_HOST = "demo-api-a1b2c3.localhost:8001"
SECRET = "replica-shared-secret"
UPSTREAM = "https://5173-sandbox.proxy.daytona.example"
NAVIGATION_HEADERS = {"accept": "text/html,application/xhtml+xml", "sec-fetch-mode": "navigate"}


def _settings(**overrides) -> Settings:
    return Settings(
        **{
            "preview_proxy_base_url": BASE_URL,
            "auth_secret_key": SECRET,
            "metrics_token": None,
            **overrides,
        }
    )


def make_workspace() -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        preview_alias_enabled=True,
    )


def make_preview(workspace: Workspace, *, port: int = 5173) -> PreviewSession:
    now = datetime.now(UTC)
    return PreviewSession(
        id=uuid4(),
        workspace_id=workspace.id,
        port=port,
        public_url="",
        status="active",
        created_at=now,
        last_seen_at=now,
    )


class _Owner:
    def __init__(self, user_id) -> None:
        self.id = user_id
        self.is_active = True


class FakeSession(AbstractAsyncContextManager):
    """Stands in for the one Postgres every replica shares."""

    def __init__(self, preview: PreviewSession, workspace: Workspace) -> None:
        self.preview = preview
        self.workspace = workspace
        self.executed: list[str] = []

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, model, item_id):
        if model is PreviewSession and item_id == self.preview.id:
            return self.preview
        if model is Workspace and item_id == self.workspace.id:
            return self.workspace
        if model is User and item_id == self.workspace.owner_id:
            return _Owner(item_id)
        return None

    async def execute(self, statement):
        self.executed.append(str(statement))

    def expunge(self, _value) -> None:
        return None


class FakeRuntime:
    configured = True

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.health_calls = 0

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return RuntimePreviewLink(url=UPSTREAM, token="token")

    async def health(self) -> str:
        self.health_calls += 1
        return "ok"

    async def close(self) -> None:
        return None


class NoopDiagnostics:
    def record(self, _event) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class FakeAliasResolver:
    def __init__(self, resolution: AliasResolution | None = None) -> None:
        self.resolution = resolution

    async def resolve(self, host: str) -> PreviewProxyTarget:
        raise InvalidPreviewCapabilityError

    async def resolve_alias(self, alias: str) -> AliasResolution:
        if self.resolution is None:
            raise InvalidPreviewCapabilityError
        return self.resolution

    async def verify_capability_for_workspace(self, capability_value, workspace_id) -> bool:
        return False


def _app(*, resolver, runtime=None, handler=None, settings=None):
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler or (lambda _r: httpx.Response(200, text="ok")))
    )
    app = create_preview_proxy_app(
        settings=settings or _settings(),
        resolver=resolver,
        runtime=runtime or FakeRuntime(),  # type: ignore[arg-type]
        http_client=http_client,
        diagnostics=NoopDiagnostics(),  # type: ignore[arg-type]
    )
    return app, http_client


def _capability_app(session: FakeSession, *, runtime=None, handler=None, settings=None):
    settings = settings or _settings()
    resolver = PreviewProxyResolver(settings, session_factory=lambda: session)
    return _app(resolver=resolver, runtime=runtime, handler=handler, settings=settings)


def _fixture() -> tuple[Workspace, PreviewSession, FakeSession]:
    workspace = make_workspace()
    preview = make_preview(workspace)
    return workspace, preview, FakeSession(preview, workspace)


def _capability_host(preview: PreviewSession, *, now: datetime | None = None) -> str:
    url = create_preview_proxy_url(
        preview.id,
        preview.port,
        base_url=BASE_URL,
        secret_key=SECRET,
        ttl_seconds=3600,
        now=now,
    )
    return httpx.URL(url).netloc.decode()


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# ---------------------------------------------------------------------------
# Slice 1: zero process-local state; any replica validates identically
# ---------------------------------------------------------------------------

_MUTATING_METHODS = {
    "append", "add", "update", "setdefault", "pop", "popitem", "clear", "extend",
    "insert", "remove", "discard",
}  # fmt: skip


def test_preview_proxy_never_mutates_module_level_state():
    """A replica must hold nothing a sibling replica would need: no function may write to a
    module-level binding (a connection registry, a per-target cache, a `global` counter)."""
    tree = ast.parse(Path(preview_proxy_module.__file__).read_text())
    module_names = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    offenders: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Global | ast.Nonlocal):
                offenders.append(f"{node.lineno}: {type(node).__name__.lower()} statement")
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Store | ast.Del)
                and isinstance(node.value, ast.Name)
                and node.value.id in module_names
            ):
                offenders.append(f"{node.lineno}: item write to {node.value.id}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATING_METHODS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_names
            ):
                offenders.append(f"{node.lineno}: {node.func.value.id}.{node.func.attr}()")
    assert not offenders, "\n".join(offenders)


def test_two_independent_replicas_accept_the_same_capability():
    _workspace, preview, session = _fixture()
    host = _capability_host(preview)

    for _replica in range(2):
        app, http_client = _capability_app(session)
        with TestClient(app, base_url=f"http://{host}") as client:
            assert client.get("/").status_code == 200
        asyncio.run(http_client.aclose())


@pytest.mark.parametrize("tamper", ["expired", "flipped"])
def test_two_independent_replicas_reject_expired_and_tampered_capabilities(tamper):
    _workspace, preview, session = _fixture()
    if tamper == "expired":
        host = _capability_host(preview, now=datetime.now(UTC) - timedelta(hours=2))
    else:
        label, rest = _capability_host(preview).split(".", 1)
        flipped = "b" if label[10] == "a" else "a"
        host = f"{label[:10]}{flipped}{label[11:]}.{rest}"

    for _replica in range(2):
        app, http_client = _capability_app(session)
        with TestClient(app, base_url=f"http://{host}") as client:
            assert client.get("/").status_code == 404
        asyncio.run(http_client.aclose())


def test_an_alias_grant_minted_by_one_replica_is_honoured_by_another():
    workspace = make_workspace()
    resolution = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=make_preview(workspace)
    )
    cookie_name = _settings().preview_alias_cookie_name
    capability_param = _settings().preview_alias_capability_query_param

    class CapabilityAcceptingResolver(FakeAliasResolver):
        async def verify_capability_for_workspace(self, capability_value, workspace_id):
            return capability_value == "cap" and workspace_id == workspace.id

    replica_a, http_a = _app(resolver=CapabilityAcceptingResolver(resolution))
    replica_b, http_b = _app(resolver=FakeAliasResolver(resolution))

    with TestClient(replica_a, base_url=f"http://{ALIAS_HOST}") as client:
        primed = client.get(f"/?{capability_param}=cap", follow_redirects=False)
    grant = primed.cookies.get(cookie_name)
    assert grant

    with TestClient(replica_b, base_url=f"http://{ALIAS_HOST}") as client:
        client.cookies.set(cookie_name, grant)
        assert client.get("/").status_code == 200

    asyncio.run(http_a.aclose())
    asyncio.run(http_b.aclose())


# ---------------------------------------------------------------------------
# Slice 3: host-gated probes
# ---------------------------------------------------------------------------


def test_live_on_the_probe_host_is_dependency_free():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/live")

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert session.executed == []


def test_ready_on_the_probe_host_checks_postgres():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/ready")

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok"}
    assert session.executed


def test_ready_reports_not_ready_when_postgres_is_down():
    class DownSession(FakeSession):
        async def execute(self, statement):
            raise OSError("connection refused")

    workspace = make_workspace()
    app, http_client = _capability_app(DownSession(make_preview(workspace), workspace))

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/ready")

    asyncio.run(http_client.aclose())
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "error"}


def test_ready_answers_503_while_draining_without_probing():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        app.state.lifecycle.begin_drain()
        response = client.get("/ready")

    asyncio.run(http_client.aclose())
    assert response.status_code == 503
    assert response.json() == {"status": "draining", "database": "skipped"}
    assert session.executed == []


def test_health_on_the_probe_host_checks_postgres_and_daytona():
    _workspace, _preview, session = _fixture()
    runtime = FakeRuntime()
    app, http_client = _capability_app(
        session, runtime=runtime, settings=_settings(sandbox_runtime_default="daytona")
    )

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/health")

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "daytona": "ok",
        "runtimes": {
            "daytona": {"configured": True, "status": "ok", "reason": None},
            # Only its default provider is probed (Epic 06).
            "docker": {"configured": None, "status": "skipped", "reason": None},
        },
    }
    assert runtime.health_calls == 1


def test_health_on_a_docker_install_reports_docker_without_a_daemon_call():
    """local-docker-runtime Epic 04: the proxy holds no daemon socket (it dials sandboxes by
    name on the sandbox network), so it reports the Docker provider without probing it,
    and a Docker-only install is not "degraded" for lacking a Daytona key."""
    _workspace, _preview, session = _fixture()
    runtime = FakeRuntime()
    app, http_client = _capability_app(
        session,
        runtime=runtime,
        settings=_settings(sandbox_runtime_default="docker", metrics_token="scrape-secret"),
    )

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/health", headers={"authorization": "Bearer scrape-secret"})

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "docker": "skipped",
        "runtimes": {
            "daytona": {"configured": None, "status": "skipped", "reason": None},
            "docker": {"configured": None, "status": "skipped", "reason": None},
        },
    }
    assert runtime.health_calls == 0


def test_the_proxy_refuses_to_start_on_docker_without_a_metrics_token():
    """It sits on every sandbox network, so a tenant can reach its /health and /metrics."""
    _workspace, _preview, session = _fixture()
    settings = _settings(sandbox_runtime_default="docker")
    app, http_client = _capability_app(session, settings=settings)

    with pytest.raises(RuntimeError, match="METRICS_TOKEN"), TestClient(app):
        pass

    asyncio.run(http_client.aclose())


@pytest.mark.parametrize("path", ["/live", "/ready", "/health", "/metrics"])
def test_probe_paths_on_a_preview_host_are_proxied_to_the_previewed_app(path):
    _workspace, preview, session = _fixture()
    upstream_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        upstream_paths.append(request.url.path)
        return httpx.Response(200, text="app's own endpoint")

    app, http_client = _capability_app(session, handler=handle)

    with TestClient(app, base_url=f"http://{_capability_host(preview)}") as client:
        response = client.get(path)

    asyncio.run(http_client.aclose())
    assert response.text == "app's own endpoint"
    assert upstream_paths == [path]


def test_unknown_paths_on_the_probe_host_still_404():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/anything-else")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Slice 4: graceful drain
# ---------------------------------------------------------------------------


def test_each_proxy_app_owns_its_lifecycle_and_never_drains_the_api_singleton():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)
    api_draining_before = lifecycle.draining

    with TestClient(app, base_url=f"http://{PROBE_HOST}"):
        assert app.state.lifecycle is not lifecycle
        assert not app.state.lifecycle.draining

    asyncio.run(http_client.aclose())
    assert app.state.lifecycle.draining
    assert lifecycle.draining is api_draining_before


async def test_lifespan_installs_the_drain_and_closes_pools_in_order(monkeypatch):
    calls: list[str] = []

    class Diagnostics(NoopDiagnostics):
        async def start(self) -> None:
            calls.append("diagnostics.start")

        async def stop(self) -> None:
            calls.append("diagnostics.stop")

    class Runtime(FakeRuntime):
        async def close(self) -> None:
            calls.append("daytona.close")

    class FakeEngine:
        async def dispose(self) -> None:
            calls.append("engine.dispose")

    def install(loop, delay_seconds, state):
        assert isinstance(state, AppLifecycle)
        calls.append(f"sigterm:{delay_seconds}")
        return lambda: calls.append("sigterm.restore")

    monkeypatch.setattr(preview_proxy_module, "engine", FakeEngine())
    monkeypatch.setattr(preview_proxy_module, "install_sigterm_drain", install)
    # No injected resolver: the app uses -- and therefore owns -- the shared engine.
    app = create_preview_proxy_app(
        settings=_settings(shutdown_drain_delay_seconds=2.0),
        runtime=Runtime(),  # type: ignore[arg-type]
        http_client=httpx.AsyncClient(),
        diagnostics=Diagnostics(),  # type: ignore[arg-type]
    )

    async with app.router.lifespan_context(app):
        calls.append("serving")
        assert not app.state.lifecycle.draining

    await app.state.http_client.aclose()
    assert app.state.lifecycle.draining
    assert calls == [
        "diagnostics.start",
        "sigterm:2.0",
        "serving",
        "diagnostics.stop",
        "daytona.close",
        "engine.dispose",
        "sigterm.restore",
    ]


def test_an_app_with_an_injected_resolver_does_not_dispose_the_shared_engine(monkeypatch):
    disposed: list[bool] = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    monkeypatch.setattr(preview_proxy_module, "engine", FakeEngine())
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)

    with TestClient(app, base_url=f"http://{PROBE_HOST}"):
        pass

    asyncio.run(http_client.aclose())
    assert disposed == []


# ---------------------------------------------------------------------------
# Slice 5: metrics
# ---------------------------------------------------------------------------


def test_metrics_on_the_probe_host_exposes_proxy_series():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session)

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/metrics")

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert "preview_proxy_requests_total" in response.text
    assert "preview_proxy_active_websockets" in response.text
    assert "preview_proxy_upstream_errors_total" in response.text


def test_metrics_requires_the_bearer_token_when_one_is_configured():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session, settings=_settings(metrics_token="scrape-secret"))

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        anonymous = client.get("/metrics")
        wrong = client.get("/metrics", headers={"authorization": "Bearer nope"})
        authorized = client.get("/metrics", headers={"authorization": "Bearer scrape-secret"})

    asyncio.run(http_client.aclose())
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert authorized.status_code == 200


def test_http_requests_and_upstream_errors_are_counted():
    _workspace, preview, session = _fixture()
    app, http_client = _capability_app(
        session, runtime=FakeRuntime(error=SandboxRuntimeError("stopped"))
    )
    requests_before = _sample("preview_proxy_requests_total", {"protocol": "http"})
    errors_before = _sample("preview_proxy_upstream_errors_total", {"protocol": "http"})

    with TestClient(app, base_url=f"http://{_capability_host(preview)}") as client:
        assert client.get("/api").status_code == 503

    asyncio.run(http_client.aclose())
    assert _sample("preview_proxy_requests_total", {"protocol": "http"}) == requests_before + 1
    assert (
        _sample("preview_proxy_upstream_errors_total", {"protocol": "http"}) == errors_before + 1
    )


class EchoUpstream:
    subprotocol = None

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.queue.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message) -> None:
        await self.queue.put(f"echo:{message}")

    async def close(self) -> None:
        await self.queue.put(None)


def test_websocket_gauge_tracks_open_bridges_and_returns_to_baseline(monkeypatch):
    _workspace, preview, session = _fixture()

    async def fake_connect(url: str, **kwargs):
        return EchoUpstream()

    monkeypatch.setattr(preview_proxy_module, "ws_connect", fake_connect)
    app, http_client = _capability_app(session)
    baseline = _sample("preview_proxy_active_websockets")
    requests_before = _sample("preview_proxy_requests_total", {"protocol": "websocket"})

    host = _capability_host(preview)
    with TestClient(app, base_url=f"http://{host}") as client:
        with client.websocket_connect("/ws", headers={"host": host}) as socket:
            socket.send_text("hi")
            assert socket.receive_text() == "echo:hi"
            assert _sample("preview_proxy_active_websockets") == baseline + 1

    asyncio.run(http_client.aclose())
    assert _sample("preview_proxy_active_websockets") == baseline
    assert (
        _sample("preview_proxy_requests_total", {"protocol": "websocket"}) == requests_before + 1
    )


def test_websocket_upstream_connect_failures_are_counted(monkeypatch):
    _workspace, preview, session = _fixture()

    async def failing_connect(url: str, **kwargs):
        raise OSError("sandbox is asleep")

    monkeypatch.setattr(preview_proxy_module, "ws_connect", failing_connect)
    app, http_client = _capability_app(session)
    before = _sample("preview_proxy_upstream_errors_total", {"protocol": "websocket"})

    host = _capability_host(preview)
    with TestClient(app, base_url=f"http://{host}") as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"host": host}):
                pass

    asyncio.run(http_client.aclose())
    assert _sample("preview_proxy_upstream_errors_total", {"protocol": "websocket"}) == before + 1


# ---------------------------------------------------------------------------
# Slice 6: the upstream is pinned -- no path, header or redirect escapes the sandbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/steal",
        "/@evil.example/steal",
        "/%40evil.example/steal",
        "/..%2F..%2Fevil",
        "/a%3Fhost=evil.example",
        "/%5C%5Cevil.example/steal",
    ],
)
def test_no_request_path_can_change_the_upstream_host(path):
    _workspace, preview, session = _fixture()
    upstream_hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        upstream_hosts.append(request.url.host)
        return httpx.Response(200)

    app, http_client = _capability_app(session, handler=handle)

    with TestClient(app, base_url=f"http://{_capability_host(preview)}") as client:
        client.get(path)

    asyncio.run(http_client.aclose())
    assert upstream_hosts == ["5173-sandbox.proxy.daytona.example"]


def test_spoofed_forwarding_headers_cannot_change_the_upstream_or_its_token():
    _workspace, preview, session = _fixture()
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    app, http_client = _capability_app(session, handler=handle)
    host = _capability_host(preview)

    with TestClient(app, base_url=f"http://{host}") as client:
        client.get(
            "/",
            headers={"x-forwarded-host": "evil.example", "x-daytona-preview-token": "forged"},
        )

    asyncio.run(http_client.aclose())
    assert seen[0].url.host == "5173-sandbox.proxy.daytona.example"
    assert seen[0].headers["x-forwarded-host"] == host
    assert seen[0].headers["x-daytona-preview-token"] == "token"


def test_upstream_redirects_to_internal_hosts_are_never_followed_or_rewritten():
    _workspace, preview, session = _fixture()
    upstream_urls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        upstream_urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    app, http_client = _capability_app(session, handler=handle)

    with TestClient(app, base_url=f"http://{_capability_host(preview)}") as client:
        response = client.get("/login", follow_redirects=False)

    asyncio.run(http_client.aclose())
    assert upstream_urls == [f"{UPSTREAM}/login"]
    assert response.status_code == 302
    assert response.headers["location"] == "http://169.254.169.254/latest/"


# ---------------------------------------------------------------------------
# Slice 7: the "waking preview" page for navigations; JSON 503 for everything else
# ---------------------------------------------------------------------------

_WAKING_KINDS = [
    "capability-sandbox-asleep",
    "capability-upstream-unreachable",
    "alias-not-running",
]


def _waking_case(kind: str):
    """(app, http_client, host, cookies) for a request the proxy must answer with a 503."""
    workspace, preview, session = _fixture()
    if kind == "capability-sandbox-asleep":
        app, http_client = _capability_app(
            session, runtime=FakeRuntime(error=SandboxRuntimeError("stopped"))
        )
        return app, http_client, _capability_host(preview), {}
    if kind == "capability-upstream-unreachable":

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        app, http_client = _capability_app(session, handler=refuse)
        return app, http_client, _capability_host(preview), {}
    # An authorised alias whose service is not running.
    resolution = AliasResolution(workspace=workspace, custom_command_id=None, preview_session=None)
    app, http_client = _app(resolver=FakeAliasResolver(resolution))
    settings = _settings()
    grant = encode_alias_grant(
        alias=ALIAS_HOST.split(".", 1)[0],
        workspace_id=workspace.id,
        secret_key=SECRET,
        ttl_seconds=settings.preview_alias_cookie_ttl_seconds,
    )
    return app, http_client, ALIAS_HOST, {settings.preview_alias_cookie_name: grant}


@pytest.mark.parametrize("kind", _WAKING_KINDS)
def test_a_navigation_that_would_503_gets_the_self_retrying_waking_page(kind):
    app, http_client, host, cookies = _waking_case(kind)

    with TestClient(app, base_url=f"http://{host}", cookies=cookies) as client:
        response = client.get("/", headers=NAVIGATION_HEADERS)

    asyncio.run(http_client.aclose())
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"]
    assert "Waking preview" in response.text
    assert "location.reload" in response.text


@pytest.mark.parametrize("kind", _WAKING_KINDS)
def test_a_non_navigation_request_that_would_503_keeps_the_json_body(kind):
    app, http_client, host, cookies = _waking_case(kind)

    with TestClient(app, base_url=f"http://{host}", cookies=cookies) as client:
        response = client.get("/api/items", headers={"accept": "application/json"})

    asyncio.run(http_client.aclose())
    assert response.status_code == 503
    assert response.json() == {"detail": "Preview is unavailable"}


def test_an_unauthorised_navigation_still_gets_the_uniform_404_not_the_waking_page():
    app, http_client, host, _cookies = _waking_case("alias-not-running")

    with TestClient(app, base_url=f"http://{host}") as client:
        response = client.get("/", headers=NAVIGATION_HEADERS)

    asyncio.run(http_client.aclose())
    assert response.status_code == 404
    assert "Waking preview" not in response.text


def test_an_editor_navigation_that_would_503_gets_the_waking_page_and_keeps_its_grant_cookie():
    from app.preview_proxy import EditorProxyTarget

    workspace = make_workspace()
    alias = "editor-0123456789ab"

    class EditorResolver(FakeAliasResolver):
        async def resolve_editor_alias(self, label: str) -> EditorProxyTarget:
            return EditorProxyTarget(workspace=workspace, editor_session=None)

    settings = _settings()
    grant = encode_alias_grant(
        alias=alias,
        workspace_id=workspace.id,
        secret_key=SECRET,
        ttl_seconds=settings.preview_alias_cookie_ttl_seconds,
    )
    app, http_client = _app(resolver=EditorResolver())

    with TestClient(app, base_url=f"http://{alias}.localhost:8001") as client:
        page = client.get(
            "/",
            params={settings.preview_alias_capability_query_param: grant},
            headers=NAVIGATION_HEADERS,
        )
        # The grant cookie is `Secure`, which the test client will not send back over http.
        api = client.get(
            "/api",
            params={settings.preview_alias_capability_query_param: grant},
            headers={"accept": "application/json"},
        )

    asyncio.run(http_client.aclose())
    assert page.status_code == 503
    assert "Waking preview" in page.text
    assert settings.preview_alias_cookie_name in page.headers["set-cookie"]
    assert api.status_code == 503
    assert api.json() == {"detail": "Editor is unavailable"}


def test_health_requires_the_bearer_token_when_one_is_configured():
    """`/health` makes a live Daytona call, so an anonymous flood must not reach it."""
    _workspace, _preview, session = _fixture()
    runtime = FakeRuntime()
    app, http_client = _capability_app(
        session, runtime=runtime, settings=_settings(metrics_token="scrape-secret")
    )

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        anonymous = client.get("/health")
        authorized = client.get("/health", headers={"authorization": "Bearer scrape-secret"})
        ready = client.get("/ready")

    asyncio.run(http_client.aclose())
    assert anonymous.status_code == 401
    assert authorized.status_code == 200
    assert runtime.health_calls == 1
    assert ready.status_code == 200


def test_a_non_ascii_bearer_token_is_a_401_not_a_500():
    _workspace, _preview, session = _fixture()
    app, http_client = _capability_app(session, settings=_settings(metrics_token="scrape-secret"))

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/metrics", headers={"authorization": "Bearer é".encode("latin-1")})

    asyncio.run(http_client.aclose())
    assert response.status_code == 401


def test_a_host_with_a_non_numeric_port_is_not_a_preview_host():
    """Only the part before `:` names the preview; the rest must not ride along into the
    `X-Forwarded-Host` the upstream app sees."""
    _workspace, preview, session = _fixture()
    upstream_calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        return httpx.Response(200)

    app, http_client = _capability_app(session, handler=handle)
    label = _capability_host(preview).split(".", 1)[0]

    with TestClient(app, base_url=f"http://{PROBE_HOST}") as client:
        response = client.get("/", headers={"host": f"{label}.localhost:8001:x.evil.example"})

    asyncio.run(http_client.aclose())
    assert response.status_code == 404
    assert upstream_calls == []
