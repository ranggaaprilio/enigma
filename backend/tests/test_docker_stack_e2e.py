"""Docker workspaces end to end through a running compose stack, the way a browser drives them.

Everything goes over HTTP/WebSocket to the real backend and the real preview proxy; nothing is
imported from the app except a few setting defaults and the container labels. Start the stack:

    ./sandbox/build-local-image.sh          # once
    podman compose -f docker-compose.yml -f docker-compose.sandbox.yml \\
        up -d postgres redis backend preview-proxy
    DOCKER_STACK_E2E=1 uv run pytest tests/test_docker_stack_e2e.py

``BACKEND_URL`` / ``PROXY_URL`` override the defaults below. The proxy picks a preview by its
``Host``, and ``*.localhost`` does not always resolve on the test host, so every proxied request
dials ``PROXY_URL`` and sets ``Host`` itself; cookies are handled by hand for the same reason
(and because the grant cookie is ``Secure``, which an HTTP cookie jar would never send back).

Covers the local-docker-runtime Epic 04 criteria ``tests/test_docker_integration.py`` cannot
reach without the proxy: the stable alias URL and its grant cookie, the editor's prime URL,
workbench and WebSocket on its own origin plus the filesystem it shares with the Files panel,
service restart with live log streaming, and the single-service ``/dev`` path with mise.
Each test registers its own user (one blank workspace per user) and deletes the workspace,
which removes its container; the fixture asserts nothing is left behind.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from os import getenv
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import websockets

from app.core.config import Settings
from app.services.sandbox.docker import MANAGED_LABEL, WORKSPACE_LABEL

pytestmark = pytest.mark.skipif(
    getenv("DOCKER_STACK_E2E") != "1",
    reason="Set DOCKER_STACK_E2E=1 to run against a running compose stack with Docker sandboxes",
)

BACKEND_URL = getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
PROXY_URL = getenv("PROXY_URL", "http://127.0.0.1:8001").rstrip("/")
SESSION_COOKIE = Settings.model_fields["auth_cookie_name"].default
GRANT_COOKIE = Settings.model_fields["preview_alias_cookie_name"].default
CAPABILITY_PARAM = Settings.model_fields["preview_alias_capability_query_param"].default
NAVIGATE = {"sec-fetch-mode": "navigate", "accept": "text/html"}


@dataclass
class Stack:
    api: httpx.AsyncClient
    proxy: httpx.AsyncClient
    session_token: str
    workspace_id: str

    @property
    def ws(self) -> str:
        return f"/workspaces/{self.workspace_id}"


def _cookie_from(response: httpx.Response, name: str) -> str | None:
    for header in response.headers.get_list("set-cookie"):
        key, _, rest = header.partition("=")
        if key.strip() == name:
            return rest.split(";", 1)[0]
    return None


async def _until(
    probe: Callable[[], Awaitable],
    done: Callable[[object], bool],
    *,
    seconds: float,
    what: str,
    interval: float = 1.0,
):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = await probe()
        if done(last):
            return last
        await asyncio.sleep(interval)
    detail = last
    if isinstance(last, httpx.Response):
        detail = f"{last.status_code} {last.text[:500]}"
    raise AssertionError(f"timed out waiting for {what}; last: {detail}")


async def _proxied(
    stack: Stack, url: str, *, grant: str | None = None, headers: dict | None = None
) -> httpx.Response:
    """GET ``url`` through the proxy as a browser on that host would."""
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    request_headers = {"host": parts.netloc, **(headers or {})}
    if grant is not None:
        request_headers["cookie"] = f"{GRANT_COOKIE}={grant}"
    return await stack.proxy.get(target, headers=request_headers)


def _proxy_ws_kwargs(extra_headers: dict) -> dict:
    proxy = urlsplit(PROXY_URL)
    return {
        "host": proxy.hostname,
        "port": proxy.port or 80,
        "additional_headers": extra_headers,
        "open_timeout": 30,
    }


@pytest.fixture
async def stack():
    async with (
        httpx.AsyncClient(base_url=BACKEND_URL, timeout=180) as api,
        httpx.AsyncClient(base_url=PROXY_URL, timeout=60) as proxy,
    ):
        for client in (api, proxy):
            ready = await client.get("/ready")
            if ready.status_code != 200:
                pytest.fail(f"{client.base_url} is not ready: {ready.status_code} {ready.text}")

        registered = await api.post(
            "/auth/register",
            json={"email": f"stack-e2e-{uuid4().hex[:10]}@example.com", "password": "password123"},
        )
        assert registered.status_code == 201, registered.text
        token = _cookie_from(registered, SESSION_COOKIE)
        assert token
        api.headers["cookie"] = f"{SESSION_COOKIE}={token}"

        created = await api.post("/workspaces", json={"name": "Stack e2e", "source": "blank"})
        assert created.status_code == 201, created.text
        workspace = created.json()
        assert workspace["runtime"] == "docker" and workspace["status"] == "ready", workspace
        stack = Stack(api=api, proxy=proxy, session_token=token, workspace_id=workspace["id"])
        try:
            yield stack
        finally:
            deleted = await api.delete(stack.ws)
            assert deleted.status_code == 200, deleted.text
            assert await _containers_for(stack.workspace_id) == []


async def _containers_for(workspace_id: str) -> list:
    import docker

    def list_them() -> list:
        client = docker.from_env()
        try:
            return client.containers.list(
                all=True,
                filters={"label": [f"{MANAGED_LABEL}=1", f"{WORKSPACE_LABEL}={workspace_id}"]},
            )
        finally:
            client.close()

    return await asyncio.to_thread(list_them)


async def _write_file(stack: Stack, path: str, content: str) -> None:
    written = await stack.api.post(
        f"{stack.ws}/files/write", json={"path": path, "content": content}
    )
    assert written.status_code == 200, written.text


async def _read_file(stack: Stack, path: str) -> str:
    read = await stack.api.get(f"{stack.ws}/files/read", params={"path": path})
    assert read.status_code == 200, read.text
    return read.json()["content"]


async def _command(stack: Stack, name: str, command: str, **fields) -> dict:
    created = await stack.api.post(
        f"{stack.ws}/commands", json={"name": name, "command": command, **fields}
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _services(stack: Stack) -> list[dict]:
    listed = await stack.api.get(f"{stack.ws}/services")
    assert listed.status_code == 200, listed.text
    return listed.json()["services"]


async def _service_for(stack: Stack, command_id: str | None) -> dict:
    return next(s for s in await _services(stack) if s["custom_command_id"] == command_id)


async def _start_service(stack: Stack, name: str, command: str, port: int) -> dict:
    cmd = await _command(stack, name, command, kind="service", port=port)
    service = await _service_for(stack, cmd["id"])
    started = await stack.api.post(f"{stack.ws}/services/{service['id']}/start")
    assert started.status_code == 200, started.text
    return await _wait_running(stack, service["id"])


async def _wait_running(stack: Stack, service_id: str) -> dict:
    """Polls the list like the Preview panel does -- each poll re-confirms the preview row,
    which is what relays a loopback-only server onto the sandbox network."""

    async def probe() -> dict:
        service = next(s for s in await _services(stack) if s["id"] == service_id)
        assert service["status"] not in ("error", "exited"), service
        return service

    return await _until(
        probe,
        lambda s: s["status"] == "running" and bool(s["public_url"]),
        seconds=120,
        what=f"service {service_id} running with a preview",
    )


async def _serves(stack: Stack, url: str, marker: str, *, grant: str | None = None) -> None:
    """Waits through the proxy's "Waking preview" 503 (relay not up yet) until the app answers."""
    response = await _until(
        lambda: _proxied(stack, url, grant=grant),
        lambda r: r.status_code == 200 and marker in r.text,
        seconds=60,
        what=f"{url} to serve {marker!r}",
    )
    assert marker in response.text


async def test_stable_alias_url_serves_the_app_only_with_a_grant_cookie(stack) -> None:
    marker = f"alias-app-{uuid4().hex[:8]}"
    await _write_file(stack, "index.html", f"<h1>{marker}</h1>\n")
    # Loopback-only, so the alias path also goes through the proxy-only relay.
    service = await _start_service(
        stack, "Web", "python3 -m http.server 5173 --bind 127.0.0.1", 5173
    )

    enabled = await stack.api.post(f"{stack.ws}/preview/aliases/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["preview_alias_enabled"] is True

    async def composed() -> dict:
        got = await stack.api.get(f"{stack.ws}/preview/composition")
        assert got.status_code == 200, got.text
        return next(s for s in got.json()["services"] if s["service_id"] == service["id"])

    entry = await _until(
        composed, lambda s: bool(s["alias_prime_url"]), seconds=30, what="alias prime url"
    )
    alias_url, prime_url = entry["alias_url"], entry["alias_prime_url"]
    assert urlsplit(alias_url).netloc.startswith(f"{entry['alias']}.")
    assert f"{CAPABILITY_PARAM}=" in prime_url and CAPABILITY_PARAM not in alias_url

    # No credential: the stable URL alone is refused.
    assert (await _proxied(stack, alias_url)).status_code == 404

    # A navigation to the prime URL trades the capability for a host-only grant cookie and
    # redirects to the same URL without it, so the capability never stays in the address bar.
    primed = await _proxied(stack, prime_url, headers=NAVIGATE)
    assert primed.status_code == 303, primed.text
    assert CAPABILITY_PARAM not in primed.headers["location"]
    grant = _cookie_from(primed, GRANT_COOKIE)
    assert grant
    set_cookie = next(
        h for h in primed.headers.get_list("set-cookie") if h.startswith(f"{GRANT_COOKIE}=")
    ).lower()
    assert "httponly" in set_cookie and "domain=" not in set_cookie

    await _serves(stack, alias_url, marker, grant=grant)
    assert (await _proxied(stack, alias_url, grant=grant + "x")).status_code == 404
    assert (await _proxied(stack, alias_url)).status_code == 404

    # A fetch() (not a navigation) on the prime URL is served directly and sets the cookie too.
    fetched = await _proxied(stack, prime_url, headers={"accept": "*/*"})
    assert fetched.status_code == 200 and marker in fetched.text
    assert _cookie_from(fetched, GRANT_COOKIE)


async def test_editor_opens_through_its_origin_and_shares_the_files_panel_filesystem(
    stack,
) -> None:
    started = await stack.api.post(f"{stack.ws}/editor/start")
    assert started.status_code == 201, started.text
    session = started.json()
    assert session["status"] == "active" and session["prime_url"], session
    editor_host = urlsplit(session["prime_url"]).netloc
    assert editor_host.startswith("editor-")
    editor_origin = f"http://{editor_host}/"

    # The prime URL carries a short-lived grant; a 503 while code-server boots still sets the
    # cookie, but each attempt takes a fresh prime URL as the panel would on a reload.
    async def prime() -> httpx.Response:
        status = await stack.api.get(f"{stack.ws}/editor/status")
        assert status.status_code == 200, status.text
        return await _proxied(stack, status.json()["session"]["prime_url"], headers=NAVIGATE)

    primed = await _until(
        prime, lambda r: r.status_code == 200, seconds=90, what="editor prime url"
    )
    grant = _cookie_from(primed, GRANT_COOKIE)
    assert grant
    assert "partitioned" in next(
        h for h in primed.headers.get_list("set-cookie") if h.startswith(f"{GRANT_COOKIE}=")
    ).lower()

    assert "workbench" in primed.text.lower()
    # Bare "/" is a 302 to the last folder; the workbench is served on "?folder=".
    workbench_url = f"{editor_origin}?folder={session['folder']}"
    workbench = await _proxied(stack, workbench_url, grant=grant, headers=NAVIGATE)
    assert workbench.status_code == 200, workbench.text[:500]
    assert "workbench" in workbench.text.lower()
    assert "x-frame-options" not in workbench.headers
    assert (await _proxied(stack, workbench_url)).status_code == 404

    # code-server's remote-extension-host socket, on the editor origin, through the proxy.
    ws_path = (
        f"/?reconnectionToken={uuid4()}&reconnection=false&skipWebSocketFrames=false"
    )
    async with websockets.connect(
        f"ws://{editor_host}{ws_path}",
        **_proxy_ws_kwargs({"cookie": f"{GRANT_COOKIE}={grant}", "origin": editor_origin}),
    ) as socket:
        assert socket.response.status_code == 101
    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(f"ws://{editor_host}{ws_path}", **_proxy_ws_kwargs({})):
            pass

    # Shared filesystem. The edit is made as the sandbox user code-server runs as, inside the
    # folder it opened (driving code-server's own file protocol is out of reach here), and the
    # Files panel's API reads it back; the reverse direction too.
    await _write_file(stack, "from-files-panel.txt", "written by the Files panel\n")
    note = f"edited-{uuid4().hex[:8]}"
    check = await _command(
        stack,
        "Editor edit",
        f"printf '{note}\\n' > editor-note.txt && cat from-files-panel.txt && pwd && "
        "echo me=$(id -un) && ps -eo user=,args= | grep '[c]ode-server' | head -1",
    )
    run = await stack.api.post(f"{stack.ws}/commands/{check['id']}/run", json={})
    assert run.status_code == 201, run.text

    async def finished() -> dict:
        runs = await stack.api.get(f"{stack.ws}/commands/{check['id']}/runs")
        return runs.json()[0]

    result = await _until(
        finished, lambda r: r["status"] != "running", seconds=60, what="command run"
    )
    assert result["status"] == "completed", result
    output = result["output"]
    assert "written by the Files panel" in output
    assert session["folder"] in output.splitlines()
    user = output.split("me=", 1)[1].split()[0]
    code_server_line = output.split("me=", 1)[1].splitlines()[1]
    assert code_server_line.split()[0] == user, output
    assert await _read_file(stack, "editor-note.txt") == f"{note}\n"

    stopped = await stack.api.post(f"{stack.ws}/editor/stop")
    assert stopped.status_code == 200, stopped.text


async def test_service_logs_stream_live_and_restart_keeps_the_preview(stack) -> None:
    marker = f"logs-app-{uuid4().hex[:8]}"
    await _write_file(stack, "index.html", f"<h1>{marker}</h1>\n")
    service = await _start_service(
        stack, "Web", "python3 -u -m http.server 5173 --bind 127.0.0.1", 5173
    )
    await _serves(stack, service["public_url"], marker)

    frames: list[str] = []
    stream_url = (
        BACKEND_URL.replace("http", "ws", 1)
        + f"{stack.ws}/services/{service['id']}/logs/stream"
    )

    async def collect(socket) -> None:
        async for message in socket:
            frame = json.loads(message)
            assert frame["type"] == "output"
            frames.append(frame["content"])

    async def seen(needle: str, since: int = 0) -> None:
        await _until(
            lambda: asyncio.sleep(0),
            lambda _: any(needle in f for f in frames[since:]),
            seconds=30,
            interval=0.25,
            what=f"a log frame with {needle!r}",
        )

    async with websockets.connect(
        stream_url,
        additional_headers={"cookie": f"{SESSION_COOKIE}={stack.session_token}"},
        open_timeout=30,
    ) as socket:
        reader = asyncio.create_task(collect(socket))
        try:
            await seen("GET / ")  # backfill: the request _serves made before we connected
            before = len(frames)
            live = f"/live-{uuid4().hex[:8]}"
            await _proxied(stack, service["public_url"] + live.lstrip("/"))
            await seen(live, since=before)  # a new frame, after the socket was open

            before = len(frames)
            restarted = await stack.api.post(f"{stack.ws}/services/{service['id']}/restart")
            assert restarted.status_code == 200, restarted.text
            service = await _wait_running(stack, service["id"])
            await _serves(stack, service["public_url"], marker)

            after = f"/after-restart-{uuid4().hex[:8]}"
            await _proxied(stack, service["public_url"] + after.lstrip("/"))
            # The same socket follows the restarted process from its first line on.
            await seen("Serving HTTP on", since=before)
            await seen(after, since=before)
        finally:
            reader.cancel()

    with pytest.raises(websockets.InvalidStatus):
        async with websockets.connect(stream_url, open_timeout=30):
            pass


async def test_single_service_dev_server_runs_with_mise(stack) -> None:
    marker = f"dev-app-{uuid4().hex[:8]}"
    await _write_file(stack, "index.html", f"<h1>{marker}</h1>\n")
    # The built-in service needs a mise config with a `dev` task and nothing else; no tools,
    # so `mise install` needs no network.
    await _write_file(
        stack, "mise.toml", '[tasks.dev]\nrun = "python3 -m http.server 3000"\n'
    )

    run = await stack.api.post(f"{stack.ws}/dev/run", json={})
    assert run.status_code == 200, run.text
    assert run.json()["ok"] is True, run.json()

    async def status() -> dict:
        got = await stack.api.get(f"{stack.ws}/dev/status")
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["status"] not in ("error", "no-config", "no-dev-task"), body
        return body

    running = await _until(
        status, lambda s: s["status"] == "running" and s["port"] == 3000,
        seconds=120, what="dev server running",
    )
    assert running["branch"] == "main"

    builtin = await _wait_running(stack, (await _service_for(stack, None))["id"])
    await _serves(stack, builtin["public_url"], marker)

    stopped = await stack.api.post(f"{stack.ws}/dev/stop")
    assert stopped.status_code == 200, stopped.text
    final = await _until(
        status, lambda s: s["status"] != "running", seconds=30, what="dev server stopped"
    )
    assert final["status"] in ("stopped", "idle") and final["port"] is None
