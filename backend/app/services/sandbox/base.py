"""Provider-neutral sandbox runtime interface.

Every consumer (agent tools, files, git, terminal, editor, preview, dev server, Spec Kit,
the worker, the preview proxy) talks to these protocols only. The ``daytona`` SDK is
imported by ``app.services.sandbox.daytona`` alone; a future provider gets its own module
next to it and is selected per workspace through ``Workspace.runtime`` (``factory.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings
from app.models import Workspace


class SandboxRuntimeError(Exception):
    """Base class for every provider error surfaced to consumers."""


class RuntimeUnavailableError(SandboxRuntimeError):
    """The provider is not configured or cannot be reached."""


class SandboxNotFoundError(SandboxRuntimeError):
    """The workspace has no sandbox, or the provider no longer knows it."""


class ProvisioningError(SandboxRuntimeError):
    """Creating or starting a sandbox failed."""


class CommandTimeoutError(SandboxRuntimeError):
    """A command exceeded its timeout."""


@dataclass(frozen=True)
class RuntimeCommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RuntimePreviewLink:
    url: str
    token: str


@dataclass(frozen=True)
class PtySize:
    cols: int
    rows: int


class RuntimeFileSystem(Protocol):
    """File operations on one workspace's sandbox.

    Signatures mirror the Daytona SDK's ``sandbox.fs`` so that adapter is a passthrough.
    Entries and infos are duck-typed (``name``/``path``, ``is_dir``, ``size``,
    ``mod_time``); consumers read them with ``getattr`` and treat any exception from
    ``get_file_info`` as "does not exist".
    """

    async def list_files(self, path: str) -> Sequence[Any]: ...

    async def get_file_info(self, path: str) -> Any: ...

    async def download_file(self, path: str) -> bytes: ...

    async def upload_file(self, content: bytes, path: str) -> None: ...

    async def create_folder(self, path: str, mode: str) -> None: ...

    async def move_files(self, source: str, destination: str) -> None: ...

    async def delete_file(self, path: str, recursive: bool = False) -> None: ...

    async def find_files(self, path: str, pattern: str) -> Sequence[Any]: ...

    async def set_file_permissions(self, path: str, *, mode: str) -> None: ...


class PtyHandle(Protocol):
    """A live terminal session; what ``TerminalPtyBridge`` drives."""

    async def send_input(self, data: bytes | str) -> None: ...

    async def resize(self, size: PtySize) -> None: ...

    async def disconnect(self) -> None: ...


class SandboxRuntime(Protocol):
    """One sandbox provider. Every method is keyed off the ``Workspace`` row.

    ``status`` returns one of ``ready`` | ``stopped`` | ``creating`` | ``error``.
    Failures are raised as ``SandboxRuntimeError`` subclasses; messages must not embed
    provider URLs or credentials because API routes surface them verbatim.
    """

    provider: str
    settings: Settings

    @property
    def configured(self) -> bool: ...

    async def health(self) -> str: ...

    async def provision(self, workspace: Workspace) -> str:
        """Create (or reuse and start) the workspace's sandbox; returns its id."""
        ...

    async def status(self, workspace: Workspace) -> str: ...

    async def start(self, workspace: Workspace) -> None: ...

    async def delete(self, workspace: Workspace) -> None: ...

    async def exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> RuntimeCommandResult: ...

    async def stream_command(
        self,
        workspace: Workspace,
        session_id: str,
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int | None,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> int: ...

    async def create_or_connect_pty(
        self,
        workspace: Workspace,
        session_id: str,
        on_data: Callable[[bytes], Awaitable[None]],
        *,
        env: dict[str, str] | None,
        cols: int,
        rows: int,
        cwd: str | None = None,
    ) -> PtyHandle: ...

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None: ...

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink: ...

    async def expose_port(self, workspace: Workspace, port: int) -> None:
        """Make a port that is listening in the sandbox reachable by ``preview_link``.

        Called when a preview session is created or re-confirmed (the app is already
        listening then), so it must be idempotent and cheap. Best effort: it logs and
        never raises for a port it cannot expose, so the preview session still records.
        """
        ...

    async def fs(self, workspace: Workspace) -> RuntimeFileSystem:
        """Resolve (and start) the sandbox and return a file-system handle on it.

        Resolution is eager so a provider outage surfaces here as a
        ``SandboxRuntimeError``, not inside a consumer's per-file ``except Exception``.
        """
        ...

    async def close(self) -> None: ...
