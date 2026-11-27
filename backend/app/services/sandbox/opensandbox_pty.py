"""Interactive PTY sessions for the OpenSandbox provider: the execd ``/pty`` API.

The OpenSandbox Python SDK does not expose the execd PTY routes (they are a
reference-implementation extension, not in the published API spec), so this module
talks to execd directly: ``POST /pty`` creates a session, ``GET /pty/{id}`` reports
its status and replay offset, ``GET /pty/{id}/ws`` is the WebSocket attach point, and
``DELETE /pty/{id}`` tears it down.

Wire format (``components/execd/PTY.md``):

- stdin frame: binary ``\\x00`` + raw bytes
- stdout frame: binary ``\\x01`` + bytes (stderr: ``\\x02`` in pipe mode only)
- resize: text JSON ``{"type":"resize","cols":N,"rows":N}``
- maybe: text JSON ``{"type":"ping"}`` -> ``{"type":"pong"}``
- connect: text JSON ``{"type":"connected","mode":...,"role":"holder"}``
- exit: text JSON ``{"exit_code":N}`` then close
- reconnect: ``?since=<output_offset>`` replays scrollback; ``?takeover=1`` evicts the
  current holder and attaches to the same shell (the contract relies on it: a terminal
  survives a page reload by reconnecting to the same session id)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog
import websockets

from app.services.sandbox.base import PtyHandle, PtySize

logger = structlog.get_logger(__name__)

# execd's port inside the sandbox; resolved through the lifecycle server's endpoint API.
EXECD_PORT = 44772
_READ_TIMEOUT_SECONDS = 30.0


class OpenSandboxPtyHandle(PtyHandle):
    """A connected execd PTY WebSocket plus its reader task."""

    def __init__(self, ws: Any, reader: asyncio.Task | None = None) -> None:
        self._ws = ws
        self._reader = reader

    async def send_input(self, data: bytes | str) -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        await self._ws.send(b"\x00" + payload)

    async def resize(self, size: PtySize) -> None:
        await self._ws.send(
            json.dumps({"type": "resize", "cols": size.cols, "rows": size.rows})
        )

    async def disconnect(self) -> None:
        # The shell session stays alive server-side; this only detaches this client.
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()


async def _pty_reader(ws: Any, on_data: Callable[[bytes], Awaitable[None]]) -> None:
    """Pump execd frames to ``on_data`` until the shell exits or the socket closes."""
    try:
        async for message in ws:
            if isinstance(message, bytes):
                if message and message[0] == 0x01:
                    await on_data(message[1:])
                continue
            try:
                frame = json.loads(message)
            except (ValueError, TypeError):
                continue
            if "exit_code" in frame:
                return
    except websockets.exceptions.ConnectionClosed:
        return
    except asyncio.CancelledError:
        raise


async def open_pty_websocket(
    base_url: str,
    server_session_id: str,
    on_data: Callable[[bytes], Awaitable[None]],
    *,
    offset: int = 0,
    takeover: bool = False,
    cols: int = 80,
    rows: int = 24,
    headers: dict[str, str] | None = None,
) -> PtyHandle:
    """Connect to an execd PTY session and return a live handle."""
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url += f"/pty/{server_session_id}/ws?since={offset}"
    if takeover:
        ws_url += "&takeover=1"
    ws = await websockets.connect(ws_url, max_size=2**22, additional_headers=headers or None)
    # Wait for the ``connected`` acknowledgment; a reconnect with ``since=`` can replay
    # scrollback (binary 0x01 frames) *before* it, and that output belongs to the terminal.
    # Pump it through, then fail loudly on an unexpected role so a refusal (409-style)
    # surfaces as an error here instead of a silently dead socket.
    try:
        while True:
            first = await asyncio.wait_for(ws.recv(), timeout=10)
            if isinstance(first, bytes):
                if first and first[0] == 0x01:
                    await on_data(first[1:])
                continue
            frame = json.loads(first)
            if frame.get("role") not in ("holder", "viewer"):
                await ws.close()
                raise RuntimeError(f"unexpected execd PTY frame: {frame!r}")
            break
    except TimeoutError as error:
        await ws.close()
        raise RuntimeError("execd PTY connect timed out") from error
    # Establish the terminal geometry before pumping: execd starts the shell at a default
    # size, and the browser's is authoritative.
    handle = OpenSandboxPtyHandle(ws)
    await handle.resize(PtySize(cols=cols, rows=rows))
    reader = asyncio.ensure_future(_pty_reader(ws, on_data))
    handle._reader = reader
    return handle


def pty_base_url(endpoint: str) -> str:
    """Normalise a lifecycle-server endpoint into the execd HTTP base URL.

    ``get_endpoint(EXECD_PORT)`` returns e.g. ``http://127.0.0.1:41923/proxy/44772``
    — the execd API is served at that exact URL.
    """
    url = endpoint if endpoint.startswith(("http://", "https://")) else f"http://{endpoint}"
    return url.rstrip("/")


async def create_pty_session(
    base_url: str,
    *,
    cwd: str | None = None,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> str:
    """POST /pty and return the server-assigned session id (201 Created)."""
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        response = await client.post(f"{base_url}/pty", json={"cwd": cwd or "/"})
        response.raise_for_status()
        return response.json()["session_id"]


async def pty_session_status(
    base_url: str, server_session_id: str, *, headers: dict[str, str] | None = None
) -> tuple[bool, int]:
    """(exists, output_offset) for a session. A missing session is (False, 0)."""
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        response = await client.get(f"{base_url}/pty/{server_session_id}")
    if response.status_code == 404:
        return False, 0
    response.raise_for_status()
    return True, int(response.json().get("output_offset", 0))


async def delete_pty_session(
    base_url: str, server_session_id: str, *, headers: dict[str, str] | None = None
) -> None:
    """DELETE /pty/{id}; idempotent (a missing session is a no-op)."""
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        response = await client.delete(f"{base_url}/pty/{server_session_id}")
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()
