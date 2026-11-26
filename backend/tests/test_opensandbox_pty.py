"""Wire-protocol tests for the execd PTY transport + the runtime reconnect logic.

The conformance suite proves the contract through fakes; these tests pin the real
transport (frame formats, URLs) with doubles for ``websockets`` and ``httpx``, and
the session-id mapping/reconnect behaviour of ``OpenSandboxRuntime``.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import websockets

from app.services.sandbox import opensandbox_pty as pty
from app.services.sandbox.base import PtySize
from tests.sandbox.fakes import FakePtyHandle, make_workspace, opensandbox_runtime_with


class FakeWs:
    """Scripted WebSocket: preloaded frames, records everything sent."""

    def __init__(self, frames: list[Any]) -> None:
        self._frames = list(frames)
        self.sent: list[Any] = []
        self.closed = False

    async def recv(self) -> Any:
        if not self._frames:
            raise websockets.exceptions.ConnectionClosed(None, None)
        return self._frames.pop(0)

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> "FakeWs":
        return self

    async def __anext__(self) -> Any:
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


def _connected_frame() -> str:
    return json.dumps({"type": "connected", "session_id": "s1", "mode": "pty", "role": "holder"})


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


class FakeClient:
    """Async-context-manager double for httpx.AsyncClient recording requests."""

    responses: list[FakeResponse] = []
    requests: list[tuple[str, str]] = []

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def _next(self) -> FakeResponse:
        return FakeClient.responses.pop(0)

    async def post(self, url: str, json: dict | None = None, **_: Any) -> FakeResponse:
        FakeClient.requests.append(("POST", url))
        return self._next()

    async def get(self, url: str) -> FakeResponse:
        FakeClient.requests.append(("GET", url))
        return self._next()

    async def delete(self, url: str) -> FakeResponse:
        FakeClient.requests.append(("DELETE", url))
        return self._next()


@pytest.fixture
def http(monkeypatch):
    FakeClient.responses = []
    FakeClient.requests = []
    monkeypatch.setattr(pty, "httpx", SimpleNamespace(AsyncClient=FakeClient))
    return FakeClient


async def test_create_session_posts_to_pty(http):
    http.responses.append(FakeResponse(201, {"session_id": "srv-9"}))
    sid = await pty.create_pty_session("http://127.0.0.1:1/proxy/44772", cwd="/workspace")
    assert sid == "srv-9"
    assert http.requests == [("POST", "http://127.0.0.1:1/proxy/44772/pty")]


async def test_status_reports_offset(http):
    http.responses.append(FakeResponse(200, {"output_offset": 42}))
    assert await pty.pty_session_status("http://h:1/pty-base", "s1") == (True, 42)


async def test_missing_session_is_not_found_false(http):
    http.responses.append(FakeResponse(404))
    assert await pty.pty_session_status("http://h:1", "gone") == (False, 0)


async def test_delete_accepts_404_as_idempotent(http):
    http.responses.append(FakeResponse(404))
    await pty.delete_pty_session("http://h:1", "gone")  # no raise
    http.responses.append(FakeResponse(500))
    with pytest.raises(httpx.HTTPStatusError):
        await pty.delete_pty_session("http://h:1", "broken")


async def test_base_url_normalisation():
    assert pty.pty_base_url("127.0.0.1:5/proxy/44772") == "http://127.0.0.1:5/proxy/44772"
    assert pty.pty_base_url("http://h:1/proxy/44772/") == "http://h:1/proxy/44772"


async def test_open_pty_url_frames_and_pump(monkeypatch):
    """URL carries since/takeover; connect is acknowledged; initial resize goes out;
    stdin is 0x00-framed; stdout frames (0x01) reach on_data; the exit frame ends the pump."""
    ws = FakeWs(
        [
            _connected_frame(),
            b"\x01echo hi\r\n",
            json.dumps({"exit_code": 0}),
        ]
    )
    urls: list[str] = []

    async def fake_connect(url, **_: Any):
        urls.append(url)
        return ws

    monkeypatch.setattr(pty.websockets, "connect", fake_connect)

    received: list[bytes] = []

    async def on_data(chunk: bytes) -> None:
        received.append(chunk)

    handle = await pty.open_pty_websocket(
        "http://h:1", "s1", on_data, offset=7, takeover=True, cols=100, rows=30
    )
    assert urls == ["ws://h:1/pty/s1/ws?since=7&takeover=1"]
    # the very first outbound send is the initial resize
    assert json.loads(ws.sent[0]) == {"type": "resize", "cols": 100, "rows": 30}

    await handle.send_input("ls\n")
    assert ws.sent[1] == b"\x00ls\n"

    await handle._reader  # exits on the exit frame

    assert received == [b"echo hi\r\n"]

    await handle.resize(PtySize(cols=80, rows=24))
    assert json.loads(ws.sent[2]) == {"type": "resize", "cols": 80, "rows": 24}

    await handle.disconnect()
    assert ws.closed


async def test_open_pty_rejects_non_holder_frames(monkeypatch):
    ws = FakeWs([json.dumps({"type": "unexpected"})])

    async def fake_connect(url, **_: Any):
        return ws

    monkeypatch.setattr(pty.websockets, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="unexpected execd PTY frame"):
        await pty.open_pty_websocket("http://h:1", "s1", lambda _d: asyncio.sleep(0))


async def test_replay_before_connected_is_pumped(monkeypatch):
    """A reconnect can replay scrollback (0x01 frames) *before* the connected ack;
    that output must reach on_data, and connect must still succeed."""
    ws = FakeWs(
        [
            b"\x01earlier scrollback\r\n",
            _connected_frame(),
            b"\x01later output\r\n",
        ]
    )

    async def fake_connect(url, **_: Any):
        return ws

    monkeypatch.setattr(pty.websockets, "connect", fake_connect)

    received: list[bytes] = []

    async def on_data(chunk: bytes) -> None:
        received.append(chunk)

    handle = await pty.open_pty_websocket("http://h:1", "s1", on_data, offset=0, takeover=True)
    # the pre-ack replay frame is delivered during connect
    assert b"earlier scrollback\r\n" in received

    await handle._reader
    assert b"later output\r\n" in received


# --- runtime reconnect mapping (seams faked by the conformance double) ------------------


async def _collect(_data: bytes) -> None:
    return None


def _workspace():
    return make_workspace("sandbox-1", runtime="opensandbox")


async def test_reconnect_reuses_the_execd_session():
    runtime = opensandbox_runtime_with()
    ws = _workspace()

    handle = await runtime.create_or_connect_pty(ws, "term-1", _collect, env=None, cols=80, rows=24)
    assert isinstance(handle, FakePtyHandle)

    sandbox = runtime._fake_sandbox
    assert len(sandbox.pty_creates) == 1
    assert handle.session_id == sandbox.pty_creates[0]

    again = await runtime.create_or_connect_pty(
        ws, "term-1", _collect, env=None, cols=80, rows=24
    )
    # same execd session: no second creation
    assert len(sandbox.pty_creates) == 1
    assert again.session_id == handle.session_id


async def test_env_set_on_first_connect():
    runtime = opensandbox_runtime_with()
    await runtime.create_or_connect_pty(
        _workspace(), "term-e", _collect, env={"TERM": "xterm-256color"}, cols=80, rows=24
    )
    assert ("TERM", "xterm-256color") in runtime._fake_sandbox.commands.set_env_calls


async def test_kill_pty_deletes_once_and_is_idempotent():
    runtime = opensandbox_runtime_with()
    ws = _workspace()
    await runtime.create_or_connect_pty(ws, "term-k", _collect, env=None, cols=80, rows=24)
    sandbox = runtime._fake_sandbox
    assert len(sandbox.pty_creates) == 1
    assert len(sandbox.pty_deletes) == 0

    await runtime.kill_pty(ws, "term-k")
    assert len(sandbox.pty_deletes) == 1

    await runtime.kill_pty(ws, "term-k")  # unknown session: no execd call, no error
    assert len(sandbox.pty_deletes) == 1


async def test_reconnect_after_execd_lost_session_recreates():
    runtime = opensandbox_runtime_with()
    ws = _workspace()
    await runtime.create_or_connect_pty(ws, "term-r", _collect, env=None, cols=80, rows=24)
    sandbox = runtime._fake_sandbox
    # execd restarted: every session it knew is gone.
    sandbox.ptys.clear()

    await runtime.create_or_connect_pty(ws, "term-r", _collect, env=None, cols=80, rows=24)
    assert len(sandbox.pty_creates) == 2  # a fresh session replaced the lost one
