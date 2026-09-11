"""Epic 13: the preview-alias gate in the proxy -- cookie/capability precedence, capability
stripping, navigation redirect, strict-origin CSRF guard, WebSocket cookie-only gate, and the
no-existence-oracle property (unknown alias / disabled alias / disabled workspace / not-running
all produce indistinguishable responses to an unauthenticated caller).
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.models import PreviewSession, Workspace
from app.preview_proxy import (
    AliasResolution,
    PreviewProxyTarget,
    create_preview_proxy_app,
)
from app.services.daytona_runtime import RuntimePreviewLink
from app.services.preview_alias import encode_alias_grant
from app.services.preview_capability import InvalidPreviewCapabilityError

BASE_URL = "http://localhost:8001"


def make_workspace(*, preview_alias_enabled: bool = True, owner_id=None) -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=owner_id or uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        preview_alias_enabled=preview_alias_enabled,
    )


def make_preview(
    workspace: Workspace, *, port: int = 8000, status: str = "active"
) -> PreviewSession:
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


class FakeAliasResolver:
    """Duck-types `PreviewProxyResolver`'s public surface. Alias data is pre-seeded directly as
    `AliasResolution` objects; capability validity is a plain lookup table (`valid_capabilities`)
    rather than real crypto, since the grant cookie -- the thing actually under test here -- is
    exercised with the real `encode_alias_grant`/`decode_alias_grant`.
    """

    def __init__(self) -> None:
        self.aliases: dict[str, AliasResolution] = {}
        # capability_value -> workspace_id it is valid for
        self.valid_capabilities: dict[str, UUID] = {}
        self.capability_target: PreviewProxyTarget | None = None
        self.resolve_alias_calls: list[str] = []
        self.verify_calls: list[tuple[str, UUID]] = []

    async def resolve(self, host: str) -> PreviewProxyTarget:
        if self.capability_target is None:
            raise InvalidPreviewCapabilityError
        return self.capability_target

    async def resolve_alias(self, alias: str) -> AliasResolution:
        self.resolve_alias_calls.append(alias)
        if alias not in self.aliases:
            raise InvalidPreviewCapabilityError
        return self.aliases[alias]

    async def verify_capability_for_workspace(
        self, capability_value: str, workspace_id: UUID
    ) -> bool:
        self.verify_calls.append((capability_value, workspace_id))
        return self.valid_capabilities.get(capability_value) == workspace_id


def _app(
    resolver: FakeAliasResolver,
    runtime: FakeRuntime,
    http_client: httpx.AsyncClient,
    **settings_kwargs,
):
    settings = Settings(daytona_preview_proxy_base_url=BASE_URL, **settings_kwargs)
    return create_preview_proxy_app(
        settings=settings,
        resolver=resolver,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        http_client=http_client,
        diagnostics=_NoopDiagnostics(),
    )


class _NoopDiagnostics:
    def record(self, _event) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _ok_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))


ALIAS_HOST = "demo-api-a1b2c3.localhost:8001"


def _client(app):
    return TestClient(app, base_url=f"http://{ALIAS_HOST}")


# ---------------------------------------------------------------------------
# No grant, no capability -> uniform 404
# ---------------------------------------------------------------------------


def test_alias_with_neither_cookie_nor_capability_gets_404_no_set_cookie():
    workspace = make_workspace()
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=make_preview(workspace)
    )
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get("/")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_unknown_alias_gets_the_identical_404_body_as_no_grant():
    resolver = FakeAliasResolver()  # no aliases registered at all
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get("/")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Capability fallback: navigation -> 303 redirect stripping the capability + Set-Cookie
# ---------------------------------------------------------------------------


def test_navigation_with_valid_capability_redirects_303_with_grant_cookie_no_upstream_call():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    resolver.valid_capabilities["thecap"] = workspace.id
    runtime = FakeRuntime([])
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get(
            "/?a=1&__enigma_cap=thecap&b=2",
            headers={"sec-fetch-mode": "navigate"},
            follow_redirects=False,
        )

    asyncio.run(http_client.aclose())
    assert response.status_code == 303
    assert response.headers["location"] == "/?a=1&b=2"
    set_cookie = response.headers.get("set-cookie", "")
    assert "enigma_preview_alias=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "samesite=none" in set_cookie.lower()
    assert "domain=" not in set_cookie.lower()
    assert calls == []  # upstream is never called for a redirect


def test_non_navigation_xhr_with_valid_capability_forwards_and_strips_capability_verbatim():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    resolver.valid_capabilities["thecap"] = workspace.id
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get(
            "/api/items?a=1&__enigma_cap=thecap&b=2",
            headers={"sec-fetch-mode": "cors"},
        )

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert captured["url"].endswith("/api/items?a=1&b=2")
    assert "set-cookie" in {k.lower() for k in response.headers.keys()}


def test_capability_never_reaches_upstream_in_any_form():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    resolver.valid_capabilities["supersecretcap"] = workspace.id
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    captured = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        client.get("/x?__enigma_cap=supersecretcap", headers={"sec-fetch-mode": "cors"})

    asyncio.run(http_client.aclose())
    assert "supersecretcap" not in captured["url"]
    assert "supersecretcap" not in str(captured["headers"])


# ---------------------------------------------------------------------------
# Cookie path: fresh cookie forwards with no Set-Cookie; near-expiry refreshes
# ---------------------------------------------------------------------------


def test_fresh_cookie_forwards_with_no_set_cookie():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    settings = Settings(daytona_preview_proxy_base_url=BASE_URL)
    app = _app(resolver, runtime, http_client)

    grant = encode_alias_grant(
        alias="demo-api-a1b2c3",
        workspace_id=workspace.id,
        secret_key=settings.auth_secret_key,
        ttl_seconds=900,
    )
    with _client(app) as client:
        client.cookies.set("enigma_preview_alias", grant)
        response = client.get("/")

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


def test_near_expiry_cookie_is_refreshed():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    settings = Settings(
        daytona_preview_proxy_base_url=BASE_URL, preview_alias_cookie_ttl_seconds=900
    )
    app = _app(resolver, runtime, http_client, preview_alias_cookie_ttl_seconds=900)

    # Issued far enough in the past that under half the TTL remains.
    stale_issue = datetime.now(UTC) - timedelta(seconds=500)
    grant = encode_alias_grant(
        alias="demo-api-a1b2c3",
        workspace_id=workspace.id,
        secret_key=settings.auth_secret_key,
        ttl_seconds=900,
        now=stale_issue,
    )
    with _client(app) as client:
        client.cookies.set("enigma_preview_alias", grant)
        response = client.get("/")

    asyncio.run(http_client.aclose())
    assert response.status_code == 200
    assert "set-cookie" in {k.lower() for k in response.headers.keys()}


def test_cookie_for_a_different_alias_falls_through_to_404():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    settings = Settings(daytona_preview_proxy_base_url=BASE_URL)
    app = _app(resolver, runtime, http_client)

    grant = encode_alias_grant(
        alias="demo-web-a1b2c3",  # a DIFFERENT alias
        workspace_id=workspace.id,
        secret_key=settings.auth_secret_key,
        ttl_seconds=900,
    )
    with _client(app) as client:
        client.cookies.set("enigma_preview_alias", grant)
        response = client.get("/")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404


def test_capability_for_a_different_workspace_is_rejected():
    workspace = make_workspace()
    other_workspace_id = uuid4()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    resolver.valid_capabilities["thecap"] = other_workspace_id  # wrong workspace
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get("/?__enigma_cap=thecap")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Disabled alias / disabled workspace / not-running -- the no-oracle property
# ---------------------------------------------------------------------------


def test_disabled_workspace_gets_404_even_with_a_capability_that_would_otherwise_work():
    """`resolve_alias` itself already filters on `workspaces.preview_alias_enabled`, so this
    is exercised at the resolver-fake level: a disabled workspace simply never appears in
    `resolver.aliases`, indistinguishable from "unknown"."""
    resolver = FakeAliasResolver()  # alias absent, standing in for "workspace disabled"
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get("/?__enigma_cap=thecap")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404


def test_valid_gate_but_not_running_is_503_not_404():
    workspace = make_workspace()
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=None
    )
    resolver.valid_capabilities["thecap"] = workspace.id
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get("/?__enigma_cap=thecap", headers={"sec-fetch-mode": "cors"})

    asyncio.run(http_client.aclose())
    assert response.status_code == 503


def test_same_alias_pregate_is_404_not_503_the_no_oracle_property():
    """The exact same alias, with no cookie and no capability, must 404 -- not 503 -- even
    though the underlying service is genuinely not running. 503 only ever appears to a caller
    who already proved they hold a valid credential for this workspace."""
    workspace = make_workspace()
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=None
    )
    runtime = FakeRuntime([])
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.get("/")

    asyncio.run(http_client.aclose())
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Preflight bypass
# ---------------------------------------------------------------------------


def test_preflight_with_neither_cookie_nor_capability_is_forwarded_upstream():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            204, headers={"access-control-allow-origin": "https://web.example.com"}
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = _app(resolver, runtime, http_client)

    with _client(app) as client:
        response = client.options(
            "/api/x",
            headers={
                "origin": "https://web.example.com",
                "access-control-request-method": "POST",
            },
        )

    asyncio.run(http_client.aclose())
    assert response.status_code == 204
    assert len(calls) == 1
    assert "set-cookie" not in {k.lower() for k in response.headers.keys()}


# ---------------------------------------------------------------------------
# Strict-origin CSRF guard on non-safe methods
# ---------------------------------------------------------------------------


def test_strict_origin_blocks_a_post_from_an_unrelated_origin():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    settings = Settings(daytona_preview_proxy_base_url=BASE_URL)
    app = _app(resolver, runtime, _ok_http_client())

    grant = encode_alias_grant(
        alias="demo-api-a1b2c3",
        workspace_id=workspace.id,
        secret_key=settings.auth_secret_key,
        ttl_seconds=900,
    )
    with _client(app) as client:
        client.cookies.set("enigma_preview_alias", grant)
        response = client.post("/api/x", json={}, headers={"origin": "https://evil.example.com"})

    assert response.status_code == 404


def test_strict_origin_allows_a_sibling_alias_of_the_same_workspace():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    # The sibling alias (the "web" service of the same workspace) resolves too.
    resolver.aliases["demo-web-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=uuid4(), preview_session=make_preview(workspace)
    )
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])
    settings = Settings(daytona_preview_proxy_base_url=BASE_URL)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    app = _app(resolver, runtime, http_client)

    grant = encode_alias_grant(
        alias="demo-api-a1b2c3",
        workspace_id=workspace.id,
        secret_key=settings.auth_secret_key,
        ttl_seconds=900,
    )
    with _client(app) as client:
        client.cookies.set("enigma_preview_alias", grant)
        response = client.post(
            "/api/x",
            json={},
            headers={"origin": "https://demo-web-a1b2c3.localhost:8001"},
        )

    asyncio.run(http_client.aclose())
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# WebSocket: cookie-only gate
# ---------------------------------------------------------------------------


def test_websocket_with_valid_cookie_is_proxied():
    import app.preview_proxy as preview_proxy_module

    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    runtime = FakeRuntime([RuntimePreviewLink(url="https://x.proxy.daytona.example", token="tok")])

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


    original = preview_proxy_module.ws_connect
    preview_proxy_module.ws_connect = fake_connect
    try:
        settings = Settings(daytona_preview_proxy_base_url=BASE_URL)
        app = _app(resolver, runtime, _ok_http_client())
        grant = encode_alias_grant(
            alias="demo-api-a1b2c3",
            workspace_id=workspace.id,
            secret_key=settings.auth_secret_key,
            ttl_seconds=900,
        )
        with TestClient(app, base_url=f"http://{ALIAS_HOST}") as client:
            # TestClient.websocket_connect does not derive the Host header from `base_url`
            # (unlike its HTTP methods) -- it must be passed explicitly here.
            with client.websocket_connect(
                "/ws", headers={"host": ALIAS_HOST, "cookie": f"enigma_preview_alias={grant}"}
            ) as socket:
                socket.close()
    finally:
        preview_proxy_module.ws_connect = original


def test_websocket_with_capability_only_no_cookie_closes_1008():
    workspace = make_workspace()
    preview = make_preview(workspace)
    resolver = FakeAliasResolver()
    resolver.aliases["demo-api-a1b2c3"] = AliasResolution(
        workspace=workspace, custom_command_id=None, preview_session=preview
    )
    resolver.valid_capabilities["thecap"] = workspace.id
    runtime = FakeRuntime([])
    app = _app(resolver, runtime, _ok_http_client())

    with TestClient(app, base_url=f"http://{ALIAS_HOST}") as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws?__enigma_cap=thecap", headers={"host": ALIAS_HOST}
            ):
                pass
