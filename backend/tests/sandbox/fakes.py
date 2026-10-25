"""Shared test doubles for the sandbox runtime interface.

Two layers:

* ``FakeFileSystem`` / ``FakePtyHandle`` / ``FakeSandboxRuntime`` implement the
  provider-neutral protocols in memory. Consumer tests inject them; the conformance suite
  runs them so a test double can never drift from the contract real providers meet.
* ``FakeDaytona*`` mimic the Daytona SDK object graph (client → sandbox → fs/process) so the
  real ``DaytonaRuntime`` adapter can be exercised without the network.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from daytona import DaytonaNotFoundError

from app.models import Workspace
from app.services.sandbox.base import (
    PtySize,
    RuntimeCommandResult,
    RuntimePreviewLink,
    SandboxNotFoundError,
)

ExecHandler = Callable[[str], RuntimeCommandResult]


def make_workspace(sandbox_id: str | None = "sandbox-1", runtime: str = "daytona") -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime=runtime,
        daytona_sandbox_id=sandbox_id,
        workspace_path="/workspace",
        status="ready",
    )


# --- interface-level doubles -----------------------------------------------------------


@dataclass
class FakeFileInfo:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mod_time: str = "1970-01-01T00:00:00Z"


class FakeFileSystem:
    """In-memory ``RuntimeFileSystem``. Paths are POSIX strings; a directory exists when it
    was created explicitly or when any entry lives below it."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        folders: set[str] | None = None,
    ) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.folders: set[str] = set(folders or ())
        self.permissions: dict[str, str] = {}
        self.uploads: list[tuple[str, bytes]] = []
        self.created_folders: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, bool]] = []

    def _children(self, path: str) -> set[str]:
        prefix = path.rstrip("/") + "/"
        return {key for key in (*self.files, *self.folders) if key.startswith(prefix)}

    def _is_dir(self, path: str) -> bool:
        return path in self.folders or bool(self._children(path))

    def _info(self, path: str) -> FakeFileInfo:
        if path not in self.files and not self._is_dir(path):
            raise FileNotFoundError(path)
        return FakeFileInfo(
            name=path.rstrip("/").rsplit("/", 1)[-1],
            path=path,
            is_dir=self._is_dir(path),
            size=len(self.files.get(path, b"")),
        )

    async def list_files(self, path: str) -> list[FakeFileInfo]:
        if not self._is_dir(path):
            raise FileNotFoundError(path)
        prefix = path.rstrip("/") + "/"
        names = sorted({key[len(prefix) :].split("/", 1)[0] for key in self._children(path)})
        return [self._info(prefix + name) for name in names]

    async def get_file_info(self, path: str) -> FakeFileInfo:
        return self._info(path)

    async def download_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def upload_file(self, content: bytes, path: str) -> None:
        self.uploads.append((path, content))
        self.files[path] = content

    async def create_folder(self, path: str, mode: str) -> None:
        self.created_folders.append((path, mode))
        self.folders.add(path)
        self.permissions[path] = mode

    async def move_files(self, source: str, destination: str) -> None:
        if source not in self.files and not self._is_dir(source):
            raise FileNotFoundError(source)
        self.moves.append((source, destination))
        for store in (self.files, self.permissions):
            for key in [k for k in store if k == source or k.startswith(source + "/")]:
                store[destination + key[len(source) :]] = store.pop(key)
        for key in [k for k in self.folders if k == source or k.startswith(source + "/")]:
            self.folders.remove(key)
            self.folders.add(destination + key[len(source) :])

    async def delete_file(self, path: str, recursive: bool = False) -> None:
        self.deletes.append((path, recursive))
        matched = False
        for store in (self.files, self.permissions, self.folders):
            for key in [k for k in store if k == path or (recursive and k.startswith(path + "/"))]:
                matched = True
                store.discard(key) if isinstance(store, set) else store.pop(key)
        if not matched:
            raise FileNotFoundError(path)

    async def find_files(self, path: str, pattern: str) -> list[Any]:
        return [
            SimpleNamespace(file=key)
            for key in sorted(self._children(path))
            if key in self.files and fnmatch(key.rsplit("/", 1)[-1], pattern)
        ]

    async def set_file_permissions(self, path: str, *, mode: str) -> None:
        if path not in self.files and not self._is_dir(path):
            raise FileNotFoundError(path)
        self.permissions[path] = mode


class FakePtyHandle:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.inputs: list[bytes | str] = []
        self.sizes: list[PtySize] = []
        self.disconnected = False

    async def send_input(self, data: bytes | str) -> None:
        self.inputs.append(data)

    async def resize(self, size: PtySize) -> None:
        self.sizes.append(size)

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeSandboxRuntime:
    """Interface-level ``SandboxRuntime`` double keyed off ``workspace.sandbox_id``."""

    provider = "fake"

    def __init__(
        self,
        filesystem: FakeFileSystem | None = None,
        *,
        exec_handler: ExecHandler | None = None,
        status_value: str = "ready",
        settings: Any = None,
    ) -> None:
        self.filesystem = filesystem or FakeFileSystem()
        self.exec_handler = exec_handler
        self.status_value = status_value
        self.settings = settings
        self.configured = True
        self.exec_calls: list[dict[str, Any]] = []
        self.provisioned: list[Workspace] = []
        self.deleted: list[Workspace] = []
        self.started: list[Workspace] = []
        self.ptys: dict[str, FakePtyHandle] = {}
        self.closed = False

    def _require(self, workspace: Workspace) -> None:
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no sandbox")

    async def health(self) -> str:
        return "ok"

    async def provision(self, workspace: Workspace) -> str:
        self.provisioned.append(workspace)
        return f"sandbox-{workspace.id}"

    async def status(self, workspace: Workspace) -> str:
        self._require(workspace)
        return self.status_value

    async def start(self, workspace: Workspace) -> None:
        self._require(workspace)
        self.started.append(workspace)

    async def delete(self, workspace: Workspace) -> None:
        self.deleted.append(workspace)

    def _run(self, command: str) -> RuntimeCommandResult:
        if self.exec_handler is not None:
            return self.exec_handler(command)
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")

    async def exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> RuntimeCommandResult:
        self._require(workspace)
        self.exec_calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
        return self._run(command)

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
        self._require(workspace)
        self.exec_calls.append(
            {"command": command, "cwd": cwd, "env": env, "timeout": timeout, "session": session_id}
        )
        result = self._run(command)
        if result.stdout:
            await on_stdout(result.stdout)
        if result.stderr:
            await on_stderr(result.stderr)
        return result.exit_code

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
    ) -> FakePtyHandle:
        self._require(workspace)
        handle = self.ptys.get(session_id)
        if handle is None:
            handle = self.ptys[session_id] = FakePtyHandle(session_id)
            handle.sizes.append(PtySize(cols=cols, rows=rows))
        handle.disconnected = False
        return handle

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        self._require(workspace)
        self.ptys.pop(session_id, None)

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        self._require(workspace)
        return RuntimePreviewLink(url=f"http://{workspace.sandbox_id}:{port}", token="")

    async def fs(self, workspace: Workspace) -> FakeFileSystem:
        self._require(workspace)
        return self.filesystem

    async def close(self) -> None:
        self.closed = True


# --- Daytona SDK-level doubles ---------------------------------------------------------


class FakeDaytonaPtySession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id
        self.inputs: list[bytes | str] = []
        self.sizes: list[Any] = []
        self.disconnected = False

    async def send_input(self, data: bytes | str) -> None:
        self.inputs.append(data)

    async def resize(self, size: Any) -> None:
        self.sizes.append(size)

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeDaytonaProcess:
    def __init__(self, exec_handler: ExecHandler | None = None) -> None:
        self.exec_handler = exec_handler
        self.request = None
        self.deleted: list[str] = []
        self.exec_calls: list[dict[str, Any]] = []
        self.pty_sessions: dict[str, FakeDaytonaPtySession] = {}
        self.pty_creates: list[dict[str, Any]] = []

    def _run(self, command: str) -> RuntimeCommandResult:
        if self.exec_handler is not None:
            return self.exec_handler(command)
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")

    async def exec(self, command: str, *, cwd=None, env=None, timeout=None):
        self.exec_calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
        result = self._run(command)
        return SimpleNamespace(exit_code=result.exit_code, result=result.stdout)

    async def create_session(self, session_id: str) -> None:
        return None

    async def execute_session_command(self, session_id: str, request):
        self.request = request
        return SimpleNamespace(cmd_id="cmd-1", exit_code=None)

    async def get_session_command_logs_async(self, session_id, command_id, on_stdout, on_stderr):
        result = self._run(self.request.command)
        if result.stdout:
            await on_stdout(result.stdout)
        if result.stderr:
            await on_stderr(result.stderr)

    async def get_session_command(self, session_id: str, command_id: str):
        return SimpleNamespace(exit_code=self._run(self.request.command).exit_code)

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)

    async def list_pty_sessions(self):
        return list(self.pty_sessions.values())

    async def connect_pty_session(self, session_id: str, on_data):
        return self.pty_sessions[session_id]

    async def create_pty_session(self, session_id, on_data, *, cwd, envs, pty_size):
        self.pty_creates.append({"cwd": cwd, "envs": envs, "pty_size": pty_size})
        session = self.pty_sessions[session_id] = FakeDaytonaPtySession(session_id)
        return session

    async def kill_pty_session(self, session_id: str) -> None:
        if session_id not in self.pty_sessions:
            raise DaytonaNotFoundError("missing")
        del self.pty_sessions[session_id]


class FakeDaytonaSandbox:
    def __init__(
        self,
        sandbox_id: str = "sandbox-1",
        state: str = "started",
        name: str = "sandbox",
        network_block_all: bool | None = None,
        network_allow_list: str | None = None,
        exec_handler: ExecHandler | None = None,
    ) -> None:
        self.id = sandbox_id
        self.name = name
        self.state = state
        self.fs = FakeFileSystem()
        self.process = FakeDaytonaProcess(exec_handler)
        self.started = False
        self.network_block_all = network_block_all
        self.network_allow_list = network_allow_list
        self.network_updates: list[dict] = []

    async def refresh_data(self) -> None:
        return None

    async def start(self, timeout: int) -> None:
        self.state = "started"
        self.started = True

    async def update_network_settings(
        self,
        *,
        network_block_all: bool | None = None,
        network_allow_list: str | None = None,
    ) -> None:
        self.network_updates.append(
            {"network_block_all": network_block_all, "network_allow_list": network_allow_list}
        )
        self.network_block_all = network_block_all
        self.network_allow_list = network_allow_list

    async def get_preview_link(self, port: int):
        return SimpleNamespace(
            url=f"https://{port}-{self.id}.proxy.daytona.example",
            token="preview-token",
        )


class FakeDaytonaClient:
    def __init__(self, existing: list[FakeDaytonaSandbox] | None = None) -> None:
        self.existing = existing or []
        self.created_params = None
        self.created_sandbox = FakeDaytonaSandbox()
        self.by_id = {sandbox.id: sandbox for sandbox in self.existing}

    async def list(self, query):
        for sandbox in self.existing:
            yield sandbox

    async def create(self, params, timeout: int):
        self.created_params = params
        self.by_id[self.created_sandbox.id] = self.created_sandbox
        return self.created_sandbox

    async def get(self, sandbox_id: str):
        if sandbox_id not in self.by_id:
            raise DaytonaNotFoundError("missing")
        return self.by_id[sandbox_id]

    async def close(self) -> None:
        return None
