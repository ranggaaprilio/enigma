"""The editor branch of the preview proxy.

Covers the properties the embedded code-server surface depends on:

* the editor host family is dispatched *before* the preview alias family (an editor label is
  alias-shaped, so branch order is load-bearing),
* authorisation is grant-only -- cookie first, then the one-shot grant in the query string,
* the cookie is `Partitioned`, without which a cross-site iframe loses it outright,
* frame-blocking response headers are stripped for the editor and **only** for the editor,
* and an unauthorised or unknown editor host is indistinguishable from any other 404.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.models import EditorSession, Workspace
from app.preview_proxy import (
    EditorProxyTarget,
    PreviewProxyTarget,
    create_preview_proxy_app,
)
from app.services.daytona_runtime import DaytonaRuntimeError, RuntimePreviewLink
from app.services.preview_alias import encode_alias_grant
from app.services.preview_capability import InvalidPreviewCapabilityError

BASE_URL = "http://localhost:8001"
EDITOR_ALIAS = "editor-a1b2c3d4e5f6"
EDITOR_HOST = f"{EDITOR_ALIAS}.localhost:8001"
SECRET = "editor-proxy-secret"


def make_workspace(*, owner_id=None) -> Workspace:
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
        editor_alias=EDITOR_ALIAS,
    )


def make_editor_session(workspace: Workspace, *, port: int = 13337) -> EditorSession:
    now = datetime.now(UTC)
    return EditorSession(
        id=uuid4(),
        workspace_id=workspace.id,
        user_id=workspace.owner_id,
        url=f"http://{EDITOR_HOST}/",
        status="active",
        bind_host="daytona",
        bind_port=port,
        folder="/home/daytona/workspace",
        started_at=now,
        last_active_at=now,
    )


class FakeRuntime:
    def __init__(self, link: RuntimePreviewLink | None = None) -> None:
        self.link = link or RuntimePreviewLink(url="https://sandbox.example", token="tok")
        self.ports: list[int] = []

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        self.ports.append(port)
        return self.link

    async def close(self) -> None:
        return None


class FakeEditorResolver:
    def __init__(self, target: EditorProxyTarget | None) -> None:
        self.target = target
        self.capability_target: PreviewProxyTarget | None = None
        self.resolve_editor_calls: list[str] = []

    async def resolve(self, host: str) -> PreviewProxyTarget:
        if self.capability_target is None:
            raise InvalidPreviewCapabilityError
        return self.capability_target

    async def resolve_alias(self, alias: str):
        # Reaching here would mean the editor host was dispatched to the preview alias family.
        raise AssertionError(f"editor host {alias!r} leaked into resolve_alias")

    async def resolve_editor_alias(self, alias: str) -> EditorProxyTarget:
        self.resolve_editor_calls.append(alias)
        if self.target is None or alias != EDITOR_ALIAS:
            raise InvalidPreviewCapabilityError
        return self.target

    async def verify_capability_for_workspace(self, value: str, workspace_id: UUID) -> bool:
        return False


class _NoopDiagnostics:
    def record(self, _event) -> None:
        raise AssertionError("the editor branch must not record CORS diagnoses")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _app(resolver, runtime, http_client, **settings_kwargs):
    settings = Settings(
        daytona_preview_proxy_base_url=BASE_URL,
        auth_secret_key=SECRET,
        **settings_kwargs,
    )
    return create_preview_proxy_app(
        settings=settings,
        resolver=resolver,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        http_client=http_client,
        diagnostics=_NoopDiagnostics(),
    )


def _http_client(handler=None) -> httpx.AsyncClient:
    handler = handler or (lambda _r: httpx.Response(200, text="workbench"))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _client(app):
    return TestClient(app, base_url=f"http://{EDITOR_HOST}")


def _grant(workspace: Workspace, *, alias: str = EDITOR_ALIAS, ttl_seconds: int = 120) -> str:
    return encode_alias_grant(
        alias=alias,
        workspace_id=workspace.id,
        secret_key=SECRET,
        ttl_seconds=ttl_seconds,
    )


def _target(workspace: Workspace, editor_session: EditorSession | None) -> EditorProxyTarget:
    return EditorProxyTarget(workspace=workspace, editor_session=editor_session)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_no_cookie_and_no_grant_is_a_404():
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get("/")

    assert response.status_code == 404
    # Never 401: a browser auth prompt would be both useless and an existence oracle.
    assert "www-authenticate" not in response.headers


def test_an_unknown_editor_host_is_the_same_404_as_an_unauthorised_one():
    resolver = FakeEditorResolver(None)
    app = _app(resolver, FakeRuntime(), _http_client())

    with TestClient(app, base_url="http://editor-ffffffffffff.localhost:8001") as client:
        unknown = client.get("/")

    workspace = make_workspace()
    known = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    with _client(_app(known, FakeRuntime(), _http_client())) as client:
        unauthorised = client.get("/")

    assert unknown.status_code == unauthorised.status_code == 404
    assert unknown.json() == unauthorised.json()


def test_a_valid_grant_in_the_query_string_proxies_and_sets_the_cookie():
    workspace = make_workspace()
    runtime = FakeRuntime()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, runtime, _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert response.status_code == 200
    assert response.text == "workbench"
    assert runtime.ports == [13337]
    assert "enigma_preview_alias=" in response.headers["set-cookie"]


def test_the_editor_cookie_is_partitioned_host_only_and_cross_site_capable():
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    cookie = response.headers["set-cookie"]
    # Partitioned is the load-bearing one: the editor cookie is always set on a cross-site
    # iframe, where an unpartitioned SameSite=None cookie is dropped by browsers that block
    # third-party cookies -- stranding the editor in a 404 loop once the grant is consumed.
    assert "Partitioned" in cookie
    assert "SameSite=None" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "Domain=" not in cookie


def test_the_cookie_alone_authorises_subsequent_requests():
    """Every subresource code-server pulls after the first navigation rides the cookie alone --
    only the very first request carries a grant."""
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        primed = client.get(f"/?__enigma_cap={_grant(workspace)}")
        assert primed.status_code == 200
        # Replayed by hand rather than through the jar: the cookie is `Secure`, which httpx
        # (correctly) refuses to send back over the plain-http base URL this harness uses.
        cookie_value = primed.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        client.cookies.set("enigma_preview_alias", cookie_value, domain=f"{EDITOR_ALIAS}.localhost")
        subsequent = client.get("/static/workbench.js")

    assert subsequent.status_code == 200


def test_the_grant_is_stripped_before_the_request_reaches_code_server():
    workspace = make_workspace()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client(handler))

    with _client(app) as client:
        client.get(f"/?folder=/srv&__enigma_cap={_grant(workspace)}")

    assert "__enigma_cap" not in seen[0]
    assert "folder=/srv" in seen[0]


def test_a_grant_for_another_workspace_is_rejected():
    workspace = make_workspace()
    other = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(other)}")

    assert response.status_code == 404


def test_a_grant_minted_for_a_preview_alias_does_not_authorise_the_editor_host():
    """Grants are signed over the alias name, so the two families cannot borrow each other's."""
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace, alias='demo-api-a1b2c3')}")

    assert response.status_code == 404


def test_an_expired_grant_is_rejected():
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())
    expired = encode_alias_grant(
        alias=EDITOR_ALIAS,
        workspace_id=workspace.id,
        secret_key=SECRET,
        ttl_seconds=120,
        now=datetime.now(UTC) - timedelta(hours=1),
    )

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={expired}")

    assert response.status_code == 404


def test_a_stale_cookie_does_not_veto_a_valid_grant_in_the_query_string():
    """The exact case a long-idle iframe hits when it reloads: the cookie has aged out, but the
    frontend has just handed it a fresh grant."""
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())
    stale = encode_alias_grant(
        alias=EDITOR_ALIAS,
        workspace_id=workspace.id,
        secret_key=SECRET,
        ttl_seconds=60,
        now=datetime.now(UTC) - timedelta(days=1),
    )

    with _client(app) as client:
        client.cookies.set("enigma_preview_alias", stale, domain=EDITOR_ALIAS + ".localhost")
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert response.status_code == 200


def test_a_known_editor_host_with_no_running_session_is_503_not_404():
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, None))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    # 503 only ever appears *after* the gate has authorised, so it is not an existence oracle.
    assert response.status_code == 503


def test_a_503_still_sets_the_cookie_so_the_one_shot_grant_is_not_wasted():
    """The grant is single-use in practice -- the iframe's `src` is fixed, so a retry inside the
    frame presents the same (eventually expired) grant. Banking the cookie on the failure means
    the retry works the moment the sandbox is back."""
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, None))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert response.status_code == 503
    assert "enigma_preview_alias=" in response.headers["set-cookie"]
    assert "Partitioned" in response.headers["set-cookie"]


def test_an_unreachable_sandbox_is_a_503_that_also_sets_the_cookie():
    workspace = make_workspace()

    class FailingRuntime(FakeRuntime):
        async def preview_link(self, workspace, port):
            raise DaytonaRuntimeError("sandbox is asleep")

    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FailingRuntime(), _http_client())

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert response.status_code == 503
    assert "enigma_preview_alias=" in response.headers["set-cookie"]


def test_an_unauthorised_request_to_a_stopped_editor_is_still_404():
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, None))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client:
        response = client.get("/")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Frame headers
# ---------------------------------------------------------------------------


def test_frame_blocking_headers_are_stripped_for_the_editor():
    workspace = make_workspace()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "x-frame-options": "SAMEORIGIN",
                "content-security-policy": "default-src 'self'; frame-ancestors 'self'",
            },
        )

    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client(handler))

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert "x-frame-options" not in response.headers
    # Only the framing directive goes; the rest of the policy is preserved verbatim.
    assert response.headers["content-security-policy"] == "default-src 'self'"


def test_a_csp_that_is_only_frame_ancestors_is_dropped_entirely():
    workspace = make_workspace()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-security-policy": "frame-ancestors 'none'"})

    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client(handler))

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert "content-security-policy" not in response.headers


def test_other_response_headers_are_forwarded_untouched():
    workspace = make_workspace()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "x-custom": "kept",
                "cache-control": "no-store",
                "content-security-policy": "default-src 'self'",
            },
        )

    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client(handler))

    with _client(app) as client:
        response = client.get(f"/?__enigma_cap={_grant(workspace)}")

    assert response.headers["x-custom"] == "kept"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "default-src 'self'"


# ---------------------------------------------------------------------------
# WebSockets -- code-server's main channel
# ---------------------------------------------------------------------------


def test_a_websocket_without_the_cookie_is_closed():
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/stable-abc/socket"):
            pass


def test_a_websocket_with_only_a_query_grant_is_closed():
    """Cookie-only by design: Starlette cannot attach a Set-Cookie to a pre-accept() close, so
    the query-string path would authorise without ever being able to persist the grant."""
    workspace = make_workspace()
    resolver = FakeEditorResolver(_target(workspace, make_editor_session(workspace)))
    app = _app(resolver, FakeRuntime(), _http_client())

    with _client(app) as client, pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/socket?__enigma_cap={_grant(workspace)}"):
            pass
