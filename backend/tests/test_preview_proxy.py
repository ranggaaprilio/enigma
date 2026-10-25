import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import preview_proxy as preview_proxy_module
from app.core.config import Settings
from app.models import PreviewSession, User, Workspace
from app.preview_proxy import (
    PreviewProxyResolver,
    PreviewProxyTarget,
    create_preview_proxy_app,
)
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    create_preview_proxy_url,
)
from app.services.sandbox import RuntimePreviewLink


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


def make_preview(workspace: Workspace, *, port: int = 5173, status: str = "active"):
    now = datetime.now(UTC)
    return PreviewSession(
        id=uuid4(),
        workspace_id=workspace.id,
        port=port,
        public_url="",
        status=status,
        created_at=now,
        last_seen_at=now,
    )


class FakeSession(AbstractAsyncContextManager):
    def __init__(
        self,
        preview: PreviewSession | None,
        workspace: Workspace | None,
        owner: User | None = None,
    ) -> None:
        self.preview = preview
        self.workspace = workspace
        self.owner = owner or (
            SimpleUser(workspace.owner_id) if workspace is not None else None
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def get(self, model, item_id):
        if model is PreviewSession:
            return self.preview if self.preview and self.preview.id == item_id else None
        if model is Workspace:
            return self.workspace if self.workspace and self.workspace.id == item_id else None
        if model is User:
            return self.owner if self.owner and self.owner.id == item_id else None
        return None

    def expunge(self, _value) -> None:
        return None


class SimpleUser:
    def __init__(self, user_id, *, is_active: bool = True) -> None:
        self.id = user_id
        self.is_active = is_active


class FakeResolver:
    def __init__(self, target: PreviewProxyTarget | None) -> None:
        self.target = target
        self.hosts: list[str] = []

    async def resolve(self, host: str) -> PreviewProxyTarget:
        self.hosts.append(host)
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


@pytest.mark.parametrize(
    ("preview_status", "preview_port"),
    [("stopped", 5173), ("active", 3000)],
)
async def test_resolver_rejects_inactive_sessions_and_port_mismatches(
    preview_status: str,
    preview_port: int,
) -> None:
    settings = Settings(
        auth_secret_key="secret",
        daytona_preview_proxy_base_url="http://localhost:8001",
    )
    workspace = make_workspace()
    preview = make_preview(workspace, port=preview_port, status=preview_status)
    public_url = create_preview_proxy_url(
        preview.id,
        5173,
        base_url=settings.daytona_preview_proxy_base_url,
        secret_key=settings.auth_secret_key,
        ttl_seconds=3600,
    )
    resolver = PreviewProxyResolver(
        settings,
        session_factory=lambda: FakeSession(preview, workspace),
    )

    with pytest.raises(InvalidPreviewCapabilityError):
        await resolver.resolve(urlsplit(public_url).netloc)


async def test_resolver_rejects_an_inactive_workspace_owner() -> None:
    settings = Settings(
        auth_secret_key="secret",
        daytona_preview_proxy_base_url="http://localhost:8001",
    )
    workspace = make_workspace()
    preview = make_preview(workspace)
    public_url = create_preview_proxy_url(
        preview.id,
        preview.port,
        base_url=settings.daytona_preview_proxy_base_url,
        secret_key=settings.auth_secret_key,
        ttl_seconds=3600,
    )
    resolver = PreviewProxyResolver(
        settings,
        session_factory=lambda: FakeSession(
            preview,
            workspace,
            owner=SimpleUser(workspace.owner_id, is_active=False),  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(InvalidPreviewCapabilityError):
        await resolver.resolve(urlsplit(public_url).netloc)


def test_http_proxy_injects_daytona_headers_retries_auth_and_rewrites_redirects() -> None:
    workspace = make_workspace()
    preview = make_preview(workspace)
    target = PreviewProxyTarget(preview_session=preview, workspace=workspace)
    resolver = FakeResolver(target)
    runtime = FakeRuntime(
        [
            RuntimePreviewLink(
                url="https://5173-sandbox.proxy.daytona.example",
                token="stale-token",
            ),
            RuntimePreviewLink(
                url="https://5173-sandbox.proxy.daytona.example",
                token="fresh-token",
            ),
        ]
    )
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(
            302,
            content=b"redirect",
            headers={
                "location": "https://5173-sandbox.proxy.daytona.example/login",
                "x-upstream": "ok",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    settings = Settings(daytona_preview_proxy_base_url="http://localhost:8001")
    proxy_app = create_preview_proxy_app(
        settings=settings,
        resolver=resolver,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        http_client=http_client,
    )

    with TestClient(proxy_app, base_url="http://capability.localhost:8001") as client:
        response = client.post(
            "/api/items?draft=1",
            content=b'{"name":"demo"}',
            headers={"content-type": "application/json"},
            follow_redirects=False,
        )

    asyncio.run(http_client.aclose())
    assert response.status_code == 302
    assert response.headers["location"] == "http://capability.localhost:8001/login"
    assert response.headers["x-upstream"] == "ok"
    assert len(requests) == 2
    assert runtime.calls == 2
    assert requests[1].url == "https://5173-sandbox.proxy.daytona.example/api/items?draft=1"
    assert requests[1].content == b'{"name":"demo"}'
    assert requests[0].headers["x-daytona-preview-token"] == "stale-token"
    assert requests[1].headers["x-daytona-preview-token"] == "fresh-token"
    assert requests[1].headers["x-daytona-skip-preview-warning"] == "true"
    assert requests[1].headers["x-forwarded-host"] == "capability.localhost:8001"
    assert requests[1].headers["accept-encoding"] == "identity"


class FakeUpstream:
    subprotocol = "vite"

    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.queue: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.queue.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        await self.queue.put(f"echo:{message}")

    async def close(self) -> None:
        self.closed = True
        await self.queue.put(None)


def test_websocket_proxy_relays_messages_and_daytona_headers(monkeypatch) -> None:
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeResolver(
        PreviewProxyTarget(preview_session=preview, workspace=workspace)
    )
    runtime = FakeRuntime(
        [
            RuntimePreviewLink(
                url="https://5173-sandbox.proxy.daytona.example",
                token="ws-token",
            )
        ]
    )
    upstream = FakeUpstream()
    captured: dict[str, object] = {}

    async def fake_connect(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return upstream

    monkeypatch.setattr(preview_proxy_module, "ws_connect", fake_connect)
    proxy_app = create_preview_proxy_app(
        settings=Settings(daytona_preview_proxy_base_url="http://localhost:8001"),
        resolver=resolver,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None)),
    )

    with TestClient(proxy_app, base_url="http://capability.localhost:8001") as client:
        with client.websocket_connect(
            "/@vite/client?token=1",
            subprotocols=["vite"],
            headers={"host": "capability.localhost:8001"},
        ) as socket:
            socket.send_text("hello")
            assert socket.receive_text() == "echo:hello"

    assert upstream.sent == ["hello"]
    assert captured["url"] == (
        "wss://5173-sandbox.proxy.daytona.example/@vite/client?token=1"
    )
    assert captured["subprotocols"] == ["vite"]
    headers = captured["additional_headers"]
    assert isinstance(headers, dict)
    assert headers["X-Daytona-Preview-Token"] == "ws-token"
    assert headers["X-Daytona-Skip-Preview-Warning"] == "true"
    assert headers["X-Forwarded-Host"] == "capability.localhost:8001"


def test_websocket_proxy_rejects_invalid_capability() -> None:
    proxy_app = create_preview_proxy_app(
        settings=Settings(daytona_preview_proxy_base_url="http://localhost:8001"),
        resolver=FakeResolver(None),  # type: ignore[arg-type]
        runtime=FakeRuntime(  # type: ignore[arg-type]
            [RuntimePreviewLink(url="https://example.com", token="token")]
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None)),
    )

    with TestClient(proxy_app, base_url="http://invalid.localhost:8001") as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/@vite/client"):
                pass
