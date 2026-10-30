"""Terminal sessions for the local Docker provider: tmux inside the workspace container.

Docker exec has no named, reconnectable PTY, so each terminal is a tmux session: a
``docker exec -it tmux new-session -A -s <name>`` attaches to it (creating it the first
time), ``disconnect`` closes only that exec — the session and its scrollback live on in
the container's tmux server — and the next connect with the same id reattaches. That is
the contract ``terminal_sessions.pty_session_id`` relies on with Daytona. Two browser tabs
on one session mirror each other, as tmux clients on one session do.

The daemon does not end an exec when its socket closes, so the attach runs under a
wrapper that records the tmux client's pid; ``disconnect`` sends it SIGHUP, which detaches
that client only. The wrapper also starts the tmux server on its own first, so the
long-lived server's argv never carries the session's ``-e`` environment.

Each open terminal holds one thread of the provider's ``pty`` pool for its reader.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from app.services.sandbox.base import PtySize

if TYPE_CHECKING:
    from app.services.sandbox.docker import DockerRuntime

logger = structlog.get_logger(__name__)

_PTY_DIR = "/tmp/.enigma-pty"
# $0 = pidfile, "$@" = the tmux client argv. ``exit-empty off`` keeps the server (and so
# every detached session) alive; status bar off and no Escape delay make it feel like a
# plain terminal.
_ATTACH = (
    f'mkdir -p {_PTY_DIR}; echo $$ > "$0"; '
    "tmux start-server \\; set-option -s exit-empty off \\; "
    "set-option -s escape-time 0 \\; set-option -g status off; "
    'exec "$@"'
)
_DETACH = 'kill -s HUP "$(cat "$0")" 2>/dev/null; rm -f "$0"'
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
_READ_SIZE = 64 * 1024
TERM = "xterm-256color"


def tmux_session_name(session_id: str) -> str:
    """tmux forbids ``:`` and ``.`` in names (worktree terminals use
    ``execute-<workspace>:<worktree>``); map every unusual character to ``_``."""
    return _UNSAFE_NAME.sub("_", session_id) or "_"


def close_exec_socket(sock: Any) -> None:
    """Close a hijacked exec socket (``exec_start(socket=True)``) and its HTTP response, in
    that order. docker-py hangs the response on the socket wrapper; left to the garbage
    collector, the wrapper may close first and the response's finalizer then raises
    "I/O operation on closed file"."""
    raw = getattr(sock, "_sock", sock)
    with contextlib.suppress(OSError):
        raw.shutdown(socket.SHUT_RDWR)
    response = getattr(sock, "_response", None)
    with contextlib.suppress(Exception):
        if response is not None:
            response.close()
        sock.close()


def deliver_threadsafe(
    loop: asyncio.AbstractEventLoop,
    callback: Callable[[Any], Awaitable[None]],
    value: Any,
    abandoned: threading.Event,
) -> bool:
    """From a pool thread, hand ``value`` to an async consumer and wait for it
    (backpressure). False once the caller has gone, so the pump stops instead of blocking
    on a dead consumer."""
    if abandoned.is_set():
        return False
    future = asyncio.run_coroutine_threadsafe(callback(value), loop)
    while True:
        try:
            future.result(timeout=0.5)
            return True
        except TimeoutError:
            if abandoned.is_set() or loop.is_closed():
                future.cancel()
                return False


def new_pidfile() -> str:
    return f"{_PTY_DIR}/{uuid4().hex}.pid"


def attach_argv(
    name: str,
    pidfile: str,
    *,
    cols: int,
    rows: int,
    cwd: str,
    environment: list[str] | None,
) -> list[str]:
    argv = ["/bin/sh", "-c", _ATTACH, pidfile, "tmux", "new-session", "-A", "-s", name]
    argv += ["-x", str(cols), "-y", str(rows), "-c", cwd]
    for pair in environment or []:
        argv += ["-e", pair]
    # The sandbox user's login shell is /bin/sh; Daytona terminals are bash.
    return [*argv, "/bin/bash", "-l"]


def detach_argv(pidfile: str) -> list[str]:
    return ["/bin/sh", "-c", _DETACH, pidfile]


class DockerPtyHandle:
    def __init__(
        self, runtime: DockerRuntime, container: Any, exec_id: str, pidfile: str, sock: Any
    ) -> None:
        self._runtime = runtime
        self._container = container
        self._exec_id = exec_id
        self._pidfile = pidfile
        # docker-py hands back a SocketIO wrapper for unix sockets; the raw socket is
        # what supports sendall/recv/shutdown.
        self._sock = sock
        self._raw = getattr(sock, "_sock", sock)
        self._abandoned = threading.Event()
        self._reader: asyncio.Future | None = None

    def start_reader(self, on_data: Callable[[bytes], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()

        def pump() -> None:
            while not self._abandoned.is_set():
                try:
                    chunk = self._raw.recv(_READ_SIZE)
                except OSError:
                    return
                if not chunk or not deliver_threadsafe(loop, on_data, chunk, self._abandoned):
                    return

        # Readers live as long as the terminal, so they get their own pool: open terminals
        # can neither starve commands and file access nor their own input (exec pool).
        self._reader = asyncio.ensure_future(self._runtime._run("pty", pump))

    async def send_input(self, data: bytes | str) -> None:
        payload = data.encode("utf-8") if isinstance(data, str) else data
        # The exec pool: a terminal that stops reading can block sendall, and that must
        # never hold up a kill on the control pool.
        await self._runtime._run("exec", self._raw.sendall, payload)

    async def resize(self, size: PtySize) -> None:
        client, _ = await self._runtime._clients()
        await self._runtime._call(
            client.api.exec_resize, self._exec_id, height=size.rows, width=size.cols
        )

    async def disconnect(self) -> None:
        """Detach this client (the tmux session keeps running) and close its socket."""
        if self._abandoned.is_set():
            return
        self._abandoned.set()
        try:
            await self._runtime._call(self._container.exec_run, detach_argv(self._pidfile))
        except Exception as error:  # the container may already be gone
            logger.warning("docker_pty_detach_failed", error_type=type(error).__name__)
        with contextlib.suppress(OSError):
            self._raw.shutdown(socket.SHUT_RDWR)  # wakes the reader
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self._reader), timeout=5)
        await self._runtime._call(close_exec_socket, self._sock)


__all__ = [
    "DockerPtyHandle",
    "close_exec_socket",
    "attach_argv",
    "deliver_threadsafe",
    "detach_argv",
    "new_pidfile",
    "tmux_session_name",
]
