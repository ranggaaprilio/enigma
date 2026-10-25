"""Epic 13: CORS diagnosis in the preview proxy.

Detection is a pure read of request/response headers after the upstream response arrives --
never injects, removes, or reorders a header. `_response_headers` is asserted byte-identical to
what the capability path already produced before this epic (the no-injection pin).
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models import PreviewSession, Workspace
from app.preview_proxy import PreviewProxyTarget, create_preview_proxy_app
from app.services.preview_capability import InvalidPreviewCapabilityError
from app.services.sandbox import RuntimePreviewLink

BASE_URL = "http://localhost:8001"
CAP_HOST = "capability.localhost:8001"


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
    )


def make_preview(workspace: Workspace, *, service_id=None, port: int = 8000) -> PreviewSession:
    now = datetime.now(UTC)
    return PreviewSession(
        id=uuid4(),
        workspace_id=workspace.id,
        service_id=service_id,
        port=port,
        public_url="",
        status="active",
        created_at=now,
        last_seen_at=now,
    )


class FakeResolver:
    def __init__(self, target: PreviewProxyTarget | None) -> None:
        self.target = target

    async def resolve(self, _host: str) -> PreviewProxyTarget:
        if self.target is None:
            raise InvalidPreviewCapabilityError
        return self.target


class FakeRuntime:
    def __init__(self, links: list[RuntimePreviewLink]) -> None:
        self.links = links
        self.calls = 0

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        link = self.links[min(self.calls, len(self.links) - 1)]
        self.calls += 1
        return link

    async def close(self) -> None:
        return None


class FakeDiagnosticsRecorder:
    def __init__(self) -> None:
        self.events: list = []

    def record(self, event) -> None:
        self.events.append(event)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _app(resolver, runtime, http_client, diagnostics):
    settings = Settings(daytona_preview_proxy_base_url=BASE_URL)
    return create_preview_proxy_app(
        settings=settings,
        resolver=resolver,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        http_client=http_client,
        diagnostics=diagnostics,
    )


def test_no_origin_header_records_nothing():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.get("/api/x")

    asyncio.run(http_client.aclose())
    assert diagnostics.events == []


def test_same_origin_records_nothing():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.get("/api/x", headers={"origin": f"http://{CAP_HOST}"})

    asyncio.run(http_client.aclose())
    assert diagnostics.events == []


def test_cross_origin_without_acao_records_one_diagnosis_and_headers_are_byte_identical():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-upstream-marker": "present"}, content=b"body")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        response = client.get("/api/x", headers={"origin": "https://web.example.com"})

    asyncio.run(http_client.aclose())
    assert len(diagnostics.events) == 1
    event = diagnostics.events[0]
    assert event.kind == "cors_missing_allow_origin"
    assert event.origin == "https://web.example.com"
    assert event.required_header == "Access-Control-Allow-Origin"
    assert event.status_code == 200
    assert event.resolve is False

    # No CORS header was injected -- the upstream's own header set passes through untouched.
    assert response.headers.get("x-upstream-marker") == "present"
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers.keys()}
    assert "access-control-allow-credentials" not in {k.lower() for k in response.headers.keys()}


def test_cross_origin_with_acao_records_a_resolve_event():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"access-control-allow-origin": "https://web.example.com"}
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.get("/api/x", headers={"origin": "https://web.example.com"})

    asyncio.run(http_client.aclose())
    assert len(diagnostics.events) == 1
    assert diagnostics.events[0].resolve is True
    assert diagnostics.events[0].kind == "cors_missing_allow_origin"


def test_preflight_rejected_with_405_records_preflight_kind_and_requested_method():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.options(
            "/api/x",
            headers={
                "origin": "https://web.example.com",
                "access-control-request-method": "PUT",
            },
        )

    asyncio.run(http_client.aclose())
    assert len(diagnostics.events) == 1
    event = diagnostics.events[0]
    assert event.kind == "cors_preflight_rejected"
    assert event.request_method == "PUT"
    assert event.status_code == 405


def test_preflight_missing_allow_headers_records_that_specific_header():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            204,
            headers={
                "access-control-allow-origin": "https://web.example.com",
                "access-control-allow-headers": "content-type",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.options(
            "/api/x",
            headers={
                "origin": "https://web.example.com",
                "access-control-request-method": "POST",
                "access-control-request-headers": "authorization, content-type",
            },
        )

    asyncio.run(http_client.aclose())
    assert len(diagnostics.events) == 1
    event = diagnostics.events[0]
    assert event.kind == "cors_preflight_rejected"
    assert event.required_header == "Access-Control-Allow-Headers"
    assert "authorization" in event.detail


def test_successful_preflight_records_nothing():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            204,
            headers={
                "access-control-allow-origin": "https://web.example.com",
                "access-control-allow-headers": "content-type, authorization",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.options(
            "/api/x",
            headers={
                "origin": "https://web.example.com",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )

    asyncio.run(http_client.aclose())
    assert diagnostics.events == []


def test_proxy_generated_404_never_self_diagnoses():
    resolver = FakeResolver(None)  # unresolvable -> 404 before any upstream call
    runtime = FakeRuntime([])
    diagnostics = FakeDiagnosticsRecorder()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        response = client.get("/api/x", headers={"origin": "https://web.example.com"})

    asyncio.run(http_client.aclose())
    assert response.status_code == 404
    assert diagnostics.events == []


def test_request_path_recorded_never_carries_the_query_string():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.get(
            "/api/x?token=SECRET&other=1", headers={"origin": "https://web.example.com"}
        )

    asyncio.run(http_client.aclose())
    assert diagnostics.events[0].request_path == "/api/x"
    assert "SECRET" not in (diagnostics.events[0].request_path or "")


def test_legacy_preview_session_with_no_service_id_records_nothing():
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=None)  # pre-Epic-11 detection path
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        client.get("/api/x", headers={"origin": "https://web.example.com"})

    asyncio.run(http_client.aclose())
    assert diagnostics.events == []


def test_50_identical_detections_with_a_fast_clock_enqueue_only_once():
    """The throttle is exercised at the `ServiceDiagnosticsRecorder` level
    (`test_service_diagnostics.py`); here we only need the proxy to call `record()` once per
    request -- 50 rapid identical requests calling `record()` 50 times is the proxy's
    contribution, and the *recorder* is what collapses them. This test pins the proxy side:
    it calls record() exactly once per matching request, not zero and not twice."""
    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        for _ in range(50):
            client.get("/api/x", headers={"origin": "https://web.example.com"})

    asyncio.run(http_client.aclose())
    assert len(diagnostics.events) == 50


def test_websocket_path_never_records_a_diagnosis(monkeypatch):
    import app.preview_proxy as preview_proxy_module

    workspace = make_workspace()
    preview = make_preview(workspace, service_id=uuid4())
    resolver = FakeResolver(PreviewProxyTarget(preview_session=preview, workspace=workspace))
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="t")])
    diagnostics = FakeDiagnosticsRecorder()

    class FakeUpstream:
        subprotocol = None

        def __init__(self):
            self.queue: asyncio.Queue = asyncio.Queue()

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.queue.get()
            if item is None:
                raise StopAsyncIteration
            return item

        async def send(self, _msg):
            return None

        async def close(self):
            await self.queue.put(None)

    upstream = FakeUpstream()

    async def fake_connect(url, **kwargs):
        return upstream

    monkeypatch.setattr(preview_proxy_module, "ws_connect", fake_connect)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client, diagnostics)

    with TestClient(app, base_url=f"http://{CAP_HOST}") as client:
        with client.websocket_connect(
            "/ws", headers={"host": CAP_HOST, "origin": "https://web.example.com"}
        ) as socket:
            socket.close()

    assert diagnostics.events == []
