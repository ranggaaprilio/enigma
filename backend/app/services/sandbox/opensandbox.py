"""OpenSandbox provider — the only module in the codebase that imports the ``opensandbox`` SDK.

Maps the neutral ``SandboxRuntime`` contract onto the OpenSandbox Python SDK: ``exec`` and
``stream_command`` are ``commands.run`` (optionally with streaming ``ExecutionHandlers``),
the file system wraps the SDK ``Filesystem`` service (whose method names differ from the
contract's — see ``OpenSandboxFileSystem``), and previews come from ``get_endpoint``.
``find_files`` stays a fixed-string *content* search: the SDK's ``files.search`` is glob-based,
so it is implemented with ``grep -F`` run inside the sandbox.

Interactive terminals are not mapped: the SDK's ``create_session``/``run_in_session`` are
one-shot bash sessions, not resizable, reconnectable PTYs (see the spike report). Until that
lands, ``create_or_connect_pty`` raises.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from posixpath import basename
from typing import Any
from urllib.parse import urlsplit

import structlog
from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxConnectionException,
    SandboxException,
    SandboxReadyTimeoutException,
    SandboxTimeoutException,
    SandboxUnhealthyException,
)
from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts
from opensandbox.models.filesystem import (
    DirectoryListEntry,
    MoveEntry,
    SetPermissionEntry,
    WriteEntry,
)
from opensandbox.models.sandboxes import SandboxFilter

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.base import (
    ProvisioningError,
    PtyHandle,
    RuntimeCommandResult,
    RuntimePreviewLink,
    RuntimeUnavailableError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)

logger = structlog.get_logger(__name__)

# Placeholder image until provisioning (image/snapshot selection per workspace) lands; it must
# carry mise + code-server like the Daytona snapshot (see sandbox/Dockerfile).
DEFAULT_IMAGE = "enigma-sandbox:latest"

# OpenSandbox state -> the contract's four states.
_STATUS = {
    "running": "ready",
    "paused": "stopped",
    "stopping": "stopped",
    "terminated": "stopped",
    "pending": "creating",
    "pausing": "creating",
    "failed": "error",
}


def map_opensandbox_error(error: Exception) -> SandboxRuntimeError:
    """Provider errors -> the neutral taxonomy. Messages never carry the API URL or key."""
    if isinstance(error, SandboxRuntimeError):
        return error
    if isinstance(error, SandboxApiException):
        if error.status_code == 404:
            return SandboxNotFoundError("OpenSandbox sandbox was not found")
        return SandboxRuntimeError(f"OpenSandbox API error (status {error.status_code})")
    if isinstance(
        error,
        (
            SandboxConnectionException,
            SandboxTimeoutException,
            SandboxReadyTimeoutException,
            SandboxUnhealthyException,
        ),
    ):
        return RuntimeUnavailableError("OpenSandbox is unavailable")
    if isinstance(error, SandboxException):
        return SandboxRuntimeError(f"OpenSandbox error ({type(error).__name__})")
    return SandboxRuntimeError(f"OpenSandbox operation failed ({type(error).__name__})")


def _build_connection_config(settings: Settings) -> ConnectionConfig:
    """A ``ConnectionConfig`` from ``OPENSANDBOX_API_URL`` / ``OPENSANDBOX_API_KEY``."""
    url = (settings.opensandbox_api_url or "").strip().rstrip("/")
    if not url:
        raise RuntimeUnavailableError("OPENSANDBOX_API_URL is not configured")
    parsed = urlsplit(url)
    if parsed.scheme:
        protocol, domain = parsed.scheme, parsed.netloc or parsed.path
    else:
        protocol, domain = "http", url
    return ConnectionConfig(
        api_key=settings.opensandbox_api_key,
        domain=domain,
        protocol=protocol,
    )


@dataclass(frozen=True)
class OpenSandboxFileInfo:
    """The duck-typed entry consumers read (``name``/``is_dir``/``size``/``mod_time``)."""

    name: str
    path: str
    is_dir: bool
    size: int
    mod_time: str


@dataclass(frozen=True)
class OpenSandboxMatch:
    file: str
    line: int
    content: str


def _translate_entry(entry: Any) -> OpenSandboxFileInfo:
    path = str(getattr(entry, "path", "") or "")
    modified = getattr(entry, "modified_at", None)
    if isinstance(modified, datetime):
        mod_time = modified.isoformat().replace("+00:00", "Z")
    else:
        mod_time = str(modified) if modified else ""
    return OpenSandboxFileInfo(
        name=basename(path.rstrip("/")) or path,
        path=path,
        is_dir=(str(getattr(entry, "entry_type", "") or "") == "directory"),
        size=int(getattr(entry, "size", 0) or 0),
        mod_time=mod_time,
    )


def parse_grep_matches(text: str) -> list[OpenSandboxMatch]:
    """Parse ``grep --null`` output: ``<file>\\0<line>:<content>\\n`` per match."""
    matches: list[OpenSandboxMatch] = []
    position = 0
    while position < len(text):
        file_end = text.find("\x00", position)
        colon = text.find(":", file_end + 1) if file_end >= 0 else -1
        if colon < 0:
            break
        newline = text.find("\n", colon + 1)
        if newline < 0:
            newline = len(text)
        try:
            line = int(text[file_end + 1 : colon])
        except ValueError:
            position = newline + 1
            continue
        matches.append(
            OpenSandboxMatch(
                file=text[position:file_end],
                line=line,
                content=text[colon + 1 : newline],
            )
        )
        position = newline + 1
    return matches


def _command_opts(
    cwd: str | None, env: dict[str, str] | None, timeout: int | None
) -> RunCommandOpts:
    return RunCommandOpts(
        working_directory=cwd or None,
        envs=env or None,
        timeout=timedelta(seconds=timeout) if timeout else None,
    )


class OpenSandboxFileSystem:
    """``RuntimeFileSystem`` over one SDK sandbox's ``files`` (method names differ — see above)."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    async def list_files(self, path: str) -> Sequence[Any]:
        try:
            entries = await self._sandbox.files.list_directory(DirectoryListEntry(path=path))
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return [_translate_entry(entry) for entry in entries]

    async def get_file_info(self, path: str) -> Any:
        try:
            infos = await self._sandbox.files.get_file_info([path])
        except Exception as error:
            raise map_opensandbox_error(error) from error
        info = infos.get(path)
        if info is None:
            raise FileNotFoundError(path)
        return _translate_entry(info)

    async def download_file(self, path: str) -> bytes:
        try:
            return await self._sandbox.files.read_bytes(path)
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def upload_file(self, content: bytes, path: str) -> None:
        try:
            await self._sandbox.files.write_files([WriteEntry(path=path, data=content)])
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def create_folder(self, path: str, mode: str) -> None:
        try:
            await self._sandbox.files.create_directories([WriteEntry(path=path, mode=int(mode, 8))])
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def move_files(self, source: str, destination: str) -> None:
        try:
            await self._sandbox.files.move_files([MoveEntry(src=source, dest=destination)])
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def delete_file(self, path: str, recursive: bool = False) -> None:
        try:
            if recursive:
                await self._sandbox.files.delete_directories([path])
            else:
                await self._sandbox.files.delete_files([path])
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def find_files(self, path: str, pattern: str) -> Sequence[Any]:
        # A fixed-string content search, as Daytona's is — not the SDK's glob ``files.search``.
        if "\n" in pattern:
            return []
        argv = ["grep", "-r", "-n", "-H", "-I", "-F", "--null", "-e", pattern, "--", path]
        try:
            execution = await self._sandbox.commands.run(argv)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        exit_code = execution.exit_code if execution.exit_code is not None else 1
        if exit_code not in (0, 1):  # grep: 1 = no match, 2 = error
            raise SandboxRuntimeError("OpenSandbox file search failed")
        return parse_grep_matches("".join(msg.text for msg in execution.logs.stdout))

    async def set_file_permissions(self, path: str, *, mode: str) -> None:
        try:
            await self._sandbox.files.set_permissions(
                [SetPermissionEntry(path=path, mode=int(mode, 8))]
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error


class OpenSandboxRuntime:
    provider = "opensandbox"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._config: ConnectionConfig | None = None
        self._manager: SandboxManager | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.opensandbox_api_url)

    def _connection_config(self) -> ConnectionConfig:
        if self._config is None:
            self._config = _build_connection_config(self.settings)
        return self._config

    # The SDK seams (replaced by tests): connect to an existing sandbox, create one, and the
    # management-API client for ``health``/``status``.
    async def _connect(self, sandbox_id: str) -> Sandbox:
        return await Sandbox.connect(
            sandbox_id,
            connection_config=self._connection_config(),
            skip_health_check=True,
        )

    async def _create(self) -> Sandbox:
        return await Sandbox.create(
            DEFAULT_IMAGE,
            timeout=None,  # manual cleanup: Enigma owns the lifecycle, destroy() on delete
            connection_config=self._connection_config(),
        )

    async def _get_manager(self) -> SandboxManager:
        if self._manager is None:
            self._manager = await SandboxManager.create(connection_config=self._connection_config())
        return self._manager

    async def close(self) -> None:
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.close()
        config, self._config = self._config, None
        if config is not None:
            await config.close_transport_if_owned()

    async def health(self) -> str:
        try:
            await (await self._get_manager()).list_sandbox_infos(SandboxFilter())
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return "ok"

    async def provision(self, workspace: Workspace) -> str:
        try:
            sandbox = await self._create()
        except Exception as error:
            raise ProvisioningError(str(map_opensandbox_error(error))) from error
        return str(sandbox.id)

    async def _get_sandbox(self, workspace: Workspace) -> Sandbox:
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no OpenSandbox sandbox")
        try:
            return await self._connect(workspace.sandbox_id)
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def fs(self, workspace: Workspace) -> OpenSandboxFileSystem:
        return OpenSandboxFileSystem(await self._get_sandbox(workspace))

    async def status(self, workspace: Workspace) -> str:
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no OpenSandbox sandbox")
        try:
            info = await (await self._get_manager()).get_sandbox_info(workspace.sandbox_id)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        state = str(getattr(getattr(info, "status", None), "state", "") or "").lower()
        return _STATUS.get(state, "error")

    async def status_detail(self, workspace: Workspace) -> str | None:
        return None

    async def start(self, workspace: Workspace) -> None:
        # Connect resolves the sandbox and verifies it is reachable; resume-on-paused lands
        # with the lifecycle task.
        await self._get_sandbox(workspace)

    async def delete(self, workspace: Workspace) -> None:
        if not workspace.sandbox_id:
            return
        try:
            sandbox = await self._get_sandbox(workspace)
            await sandbox.destroy()
        except SandboxNotFoundError:
            return
        except Exception as error:
            mapped = map_opensandbox_error(error)
            if isinstance(mapped, SandboxNotFoundError):
                return  # gone between connect and destroy: deleting is a no-op
            raise mapped from error

    async def exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> RuntimeCommandResult:
        sandbox = await self._get_sandbox(workspace)
        try:
            execution = await sandbox.commands.run(command, opts=_command_opts(cwd, env, timeout))
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return RuntimeCommandResult(
            exit_code=execution.exit_code if execution.exit_code is not None else 1,
            stdout="".join(msg.text for msg in execution.logs.stdout),
            stderr="".join(msg.text for msg in execution.logs.stderr),
        )

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
    ) -> int:
        # The SDK streams one-shot commands over SSE; no persistent session id is needed.
        sandbox = await self._get_sandbox(workspace)

        async def on_stdout_msg(message: Any) -> None:
            await on_stdout(message.text)

        async def on_stderr_msg(message: Any) -> None:
            await on_stderr(message.text)

        handlers = ExecutionHandlers(on_stdout=on_stdout_msg, on_stderr=on_stderr_msg)
        try:
            execution = await sandbox.commands.run(
                command, opts=_command_opts(cwd, env, timeout), handlers=handlers
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return execution.exit_code if execution.exit_code is not None else 1

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
    ) -> PtyHandle:
        raise SandboxRuntimeError(
            "OpenSandbox does not support interactive terminals yet"
        )

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        # No interactive PTY sessions exist to kill; ``create_or_connect_pty`` never succeeds.
        return None

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        sandbox = await self._get_sandbox(workspace)
        try:
            endpoint = await sandbox.get_endpoint(port)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        url = str(getattr(endpoint, "endpoint", "") or "")
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        return RuntimePreviewLink(url=url, token="")

    async def expose_port(self, workspace: Workspace, port: int) -> None:
        """Requesting the endpoint is enough (the server publishes it on demand). Best effort."""
        try:
            sandbox = await self._get_sandbox(workspace)
            await sandbox.get_endpoint(port)
        except Exception:
            logger.warning("opensandbox_expose_port_failed", port=int(port))


__all__ = [
    "OpenSandboxRuntime",
    "map_opensandbox_error",
    "parse_grep_matches",
    "OpenSandboxFileInfo",
    "OpenSandboxMatch",
]
