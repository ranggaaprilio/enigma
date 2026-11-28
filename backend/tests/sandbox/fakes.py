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
from posixpath import dirname
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
        sandbox_id=sandbox_id,
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
        # Daytona's find_files is a content search (grep), not a filename glob.
        return [
            SimpleNamespace(file=key, line=number, content=text)
            for key in sorted(self._children(path))
            if key in self.files
            for number, text in enumerate(self.files[key].decode(errors="replace").split("\n"), 1)
            if pattern in text
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
        self.exposed_ports: list[int] = []
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

    async def expose_port(self, workspace: Workspace, port: int) -> None:
        self._require(workspace)
        self.exposed_ports.append(port)

    async def status_detail(self, workspace: Workspace) -> str | None:
        self._require(workspace)
        return None

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


# --- OpenSandbox SDK-level doubles ----------------------------------------------------
#
# Mirror the slice of the OpenSandbox Python SDK the adapter uses: a ``Sandbox`` object with
# ``.files`` (a Filesystem service) and ``.commands`` (a Commands service), plus the
# management-API ``SandboxManager`` for ``health``/``status``. ``commands.run`` is called with
# either the user's command string (routed through ``exec_handler``) or a ``grep`` argv —
# ``find_files`` — which is emulated against the in-memory store.


class FakeOpenSandboxStore:
    """In-memory files shared by the fake ``files`` and ``commands`` services."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.modes: dict[str, int] = {}

    def _subtree(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [key for key in (*self.files, *self.dirs) if key.startswith(prefix)]

    def _children(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            key
            for key in (*self.files, *self.dirs)
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        )

    def is_dir(self, path: str) -> bool:
        return path in self.dirs or bool(self._subtree(path))

    def exists(self, path: str) -> bool:
        return path in self.files or self.is_dir(path)

    def info(self, path: str) -> SimpleNamespace:
        if not self.exists(path):
            raise FileNotFoundError(path)
        return SimpleNamespace(
            path=path,
            entry_type="directory" if self.is_dir(path) else "file",
            size=len(self.files.get(path, b"")),
            modified_at="1970-01-01T00:00:00Z",
        )

    def grep(self, pattern: str, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        matches: list[str] = []
        for key in sorted(self.files):
            if key == path or key.startswith(prefix):
                for number, line in enumerate(self.files[key].split(b"\n"), 1):
                    if pattern.encode() in line:
                        matches.append(
                            key + "\0" + str(number) + ":" + line.decode(errors="replace") + "\n"
                        )
        return matches


class FakeOpenSandboxFiles:
    """In-memory OpenSandbox SDK ``Filesystem`` service (SDK method names, not Enigma's)."""

    def __init__(self, store: FakeOpenSandboxStore) -> None:
        self._store = store

    async def write_files(self, entries: list[Any]) -> None:
        for entry in entries:
            data = entry.data
            self._store.files[entry.path] = data if isinstance(data, bytes) else str(data).encode()

    async def read_file(self, path: str, **_: Any) -> str:
        if path not in self._store.files:
            raise FileNotFoundError(path)
        return self._store.files[path].decode()

    async def read_bytes(self, path: str, **_: Any) -> bytes:
        if path not in self._store.files:
            raise FileNotFoundError(path)
        return self._store.files[path]

    async def create_directories(self, entries: list[Any]) -> None:
        for entry in entries:
            path = entry.path
            while path not in self._store.dirs:
                self._store.dirs.add(path)
                self._store.modes.setdefault(path, entry.mode)
                path = dirname(path)
                if not path or path == "/":
                    break

    async def list_directory(self, entry: Any) -> list[SimpleNamespace]:
        return [self._store.info(child) for child in self._store._children(entry.path)]

    async def get_file_info(self, paths: list[str]) -> dict[str, SimpleNamespace]:
        return {path: self._store.info(path) for path in paths if self._store.exists(path)}

    async def search(self, entry: Any) -> list[SimpleNamespace]:
        raise NotImplementedError  # glob-based; the adapter never uses it (find_files is grep)

    async def move_files(self, entries: list[Any]) -> None:
        for entry in entries:
            source, destination = entry.src, entry.dest
            if not self._store.exists(source):
                raise FileNotFoundError(source)
            for store in (self._store.files, self._store.modes):
                for key in [k for k in store if k == source or k.startswith(source + "/")]:
                    store[destination + key[len(source) :]] = store.pop(key)
            for key in [k for k in self._store.dirs if k == source or k.startswith(source + "/")]:
                self._store.dirs.remove(key)
                self._store.dirs.add(destination + key[len(source) :])

    async def delete_files(self, paths: list[str]) -> None:
        for path in paths:
            if path not in self._store.files:
                raise FileNotFoundError(path)
            del self._store.files[path]

    async def delete_directories(self, paths: list[str]) -> None:
        for path in paths:
            if not self._store.is_dir(path):
                raise FileNotFoundError(path)
            for key in [path, *self._store._subtree(path)]:
                self._store.files.pop(key, None)
                self._store.dirs.discard(key)

    async def set_permissions(self, entries: list[Any]) -> None:
        for entry in entries:
            if not self._store.exists(entry.path):
                raise FileNotFoundError(entry.path)
            self._store.modes[entry.path] = entry.mode


class FakeOpenSandboxCommands:
    """OpenSandbox SDK ``Commands`` service: strings route to ``exec_handler``, a ``grep``
    argv is emulated against the store (``find_files``)."""

    def __init__(self, sandbox: FakeOpenSandboxSandbox, exec_handler: ExecHandler | None) -> None:
        self._sandbox = sandbox
        self.exec_handler = exec_handler
        self.run_calls: list[Any] = []
        self.set_env_calls: list[tuple[str, str]] = []

    def _run_result(self, command: str) -> RuntimeCommandResult:
        if self.exec_handler is not None:
            return self.exec_handler(command)
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")

    @staticmethod
    def _messages(text: str) -> list[SimpleNamespace]:
        """Split output the way execd does (seen live against opensandbox-server 1.2.1): one
        message per line with its newline stripped, an empty line arriving as ``"\n"``, and
        a final line without a newline sent as-is. Sending the whole output as one message
        is how a "".join() that glued every line together went unnoticed."""
        if not text:
            return []
        lines = text.split("\n")
        if text.endswith("\n"):
            lines = lines[:-1]
        return [SimpleNamespace(text=line if line else "\n") for line in lines]

    @classmethod
    def _execution(cls, exit_code: int, stdout: str, stderr: str) -> SimpleNamespace:
        return SimpleNamespace(
            exit_code=exit_code,
            logs=SimpleNamespace(stdout=cls._messages(stdout), stderr=cls._messages(stderr)),
        )

    async def run(self, command: Any, *, opts: Any = None, handlers: Any = None) -> Any:
        self.run_calls.append((command, opts, handlers))
        from app.services.sandbox.opensandbox import FLOOR_PROBE

        if command == FLOOR_PROBE:  # the provider's security-floor check (Plan 7a)
            return self._execution(0, self._sandbox.floor, "")
        if isinstance(command, list) and command and command[0] == "grep":
            return await self._grep(command, handlers)
        result = self._run_result(command)
        if handlers is not None:
            if getattr(handlers, "on_stdout", None):
                for message in self._messages(result.stdout):
                    await handlers.on_stdout(message)
            if getattr(handlers, "on_stderr", None):
                for message in self._messages(result.stderr):
                    await handlers.on_stderr(message)
        return self._execution(result.exit_code, result.stdout, result.stderr)

    async def _grep(self, argv: list[str], handlers: Any) -> Any:
        pattern = argv[argv.index("-e") + 1]
        text = "".join(self._sandbox._store.grep(pattern, argv[-1]))
        if handlers is not None and getattr(handlers, "on_stdout", None):
            for message in self._messages(text):
                await handlers.on_stdout(message)
        # ``skip_accumulation`` means the SDK never buffers stdout into the Execution logs.
        skip = handlers is not None and getattr(handlers, "skip_accumulation", False)
        return self._execution(0 if text else 1, "" if skip else text, "")

    async def set_env(self, key: str, value: str) -> None:
        self.set_env_calls.append((key, value))


class FakeOpenSandboxSandbox:
    """OpenSandbox SDK ``Sandbox`` object: id, files, commands, get_info, get_endpoint,
    destroy/kill/close."""

    def __init__(
        self,
        sandbox_id: str = "sandbox-1",
        *,
        state: str = "Running",
        exec_handler: ExecHandler | None = None,
    ) -> None:
        self.id = sandbox_id
        self.state = state
        self._store = FakeOpenSandboxStore()
        self.files = FakeOpenSandboxFiles(self._store)
        self.commands = FakeOpenSandboxCommands(self, exec_handler)
        self.destroyed = False
        # What the security-floor probe sees: caps dropped, no-new-privs, a non-root uid.
        self.floor = "CapEff: 0000000000000000\nNoNewPrivs: 1\n1001\n"
        self.endpoint_ports: list[int] = []
        # execd /pty state: session_id -> FakePtyHandle (what the runtime seam sees).
        self.ptys: dict[str, FakePtyHandle] = {}
        self.pty_creates: list[str] = []
        self.pty_deletes: list[str] = []

    async def get_info(self) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(state=self.state))

    async def get_endpoint(self, port: int) -> SimpleNamespace:
        self.endpoint_ports.append(port)
        return SimpleNamespace(endpoint=f"http://127.0.0.1:8080/proxy/{port}", headers={})

    async def kill(self) -> None:
        self.destroyed = True

    async def close(self) -> None:
        return None

    async def destroy(self) -> None:
        self.destroyed = True


class FakeOpenSandboxManager:
    """Management-API ``SandboxManager`` double: health, status, a paged listing filtered by
    metadata (as the server does, AND-matched), and pause/resume/kill."""

    def __init__(self, state: str = "Running") -> None:
        self.state = state
        self.listed = False
        self.sandboxes: list[SimpleNamespace] = []  # id, metadata (what the server stores)
        self.filters: list[Any] = []
        self.page_size = 2
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.killed: list[str] = []

    async def list_sandbox_infos(self, filter: Any) -> SimpleNamespace:
        self.listed = True
        self.filters.append(filter)
        wanted = getattr(filter, "metadata", None) or {}
        matching = [
            s for s in self.sandboxes
            if all((s.metadata or {}).get(key) == value for key, value in wanted.items())
        ]
        page = getattr(filter, "page", None) or 1
        chunk = matching[(page - 1) * self.page_size : page * self.page_size]
        return SimpleNamespace(
            sandbox_infos=chunk,
            pagination=SimpleNamespace(
                page=page, has_next_page=page * self.page_size < len(matching)
            ),
        )

    async def get_sandbox_info(self, sandbox_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(state=self.state))

    async def pause_sandbox(self, sandbox_id: str) -> None:
        self.paused.append(sandbox_id)
        self.state = "Paused"

    async def resume_sandbox(self, sandbox_id: str) -> None:
        self.resumed.append(sandbox_id)
        self.state = "Running"

    async def kill_sandbox(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)


class RecordingActivity:
    """``SandboxActivityRecorder`` double: records which workspaces were touched."""

    def __init__(self) -> None:
        self.touched: list[Any] = []

    async def touch(self, workspace_id: Any) -> None:
        self.touched.append(workspace_id)

    async def close(self) -> None:
        return None


def opensandbox_runtime_with(
    exec_handler: ExecHandler | None = None,
    *,
    sandbox_id: str = "sandbox-1",
    state: str = "Running",
) -> Any:
    """A real ``OpenSandboxRuntime`` wired to the fake sandbox and manager (no live daemon)."""
    from app.core.config import Settings
    from app.services.sandbox.opensandbox import OpenSandboxRuntime

    runtime = OpenSandboxRuntime(
        Settings(opensandbox_api_url="http://127.0.0.1:8080", opensandbox_api_key="test-key")
    )
    sandbox = FakeOpenSandboxSandbox(sandbox_id, state=state, exec_handler=exec_handler)
    manager = FakeOpenSandboxManager(state=state)

    async def connect(sandbox_id_: str, **_: Any) -> FakeOpenSandboxSandbox:
        return sandbox

    async def create(*_: Any, **__: Any) -> FakeOpenSandboxSandbox:
        return sandbox

    async def get_manager() -> FakeOpenSandboxManager:
        return manager

    # execd /pty seams: an in-memory stand-in for the HTTP+WebSocket API (the real
    # implementation lives in app.services.sandbox.opensandbox_pty).
    async def pty_create_session(base_url: str, cwd: str | None, **_: Any) -> str:
        session_id = f"execd-{uuid4().hex[:8]}"
        sandbox.pty_creates.append(session_id)
        sandbox.ptys[session_id] = FakePtyHandle(session_id)
        return session_id

    async def pty_status(base_url: str, server_session_id: str, **_: Any) -> tuple[bool, int]:
        return server_session_id in sandbox.ptys, 0

    async def pty_connect(
        base_url: str,
        server_session_id: str,
        on_data: Any,
        *,
        offset: int,
        takeover: bool,
        cols: int,
        rows: int,
        **_: Any,
    ) -> FakePtyHandle:
        try:
            return sandbox.ptys[server_session_id]
        except KeyError:
            raise RuntimeError("no such execd PTY session") from None

    async def pty_delete_session(base_url: str, server_session_id: str, **_: Any) -> None:
        sandbox.pty_deletes.append(server_session_id)
        sandbox.ptys.pop(server_session_id, None)

    runtime._connect = connect  # type: ignore[method-assign]
    runtime._create = create  # type: ignore[method-assign]
    runtime._get_manager = get_manager  # type: ignore[method-assign]
    runtime._pty_create_session = pty_create_session  # type: ignore[method-assign]
    runtime._pty_session_status = pty_status  # type: ignore[method-assign]
    runtime._pty_connect = pty_connect  # type: ignore[method-assign]
    runtime._pty_delete_session = pty_delete_session  # type: ignore[method-assign]
    runtime._installation = "test-installation"  # no database read in unit tests
    runtime.activity = RecordingActivity()  # no sandbox_activity writes in unit tests
    runtime._fake_manager = manager

    async def auth_probe_status() -> int:
        return 401  # a server that requires its API key

    runtime._auth_probe_status = auth_probe_status  # type: ignore[method-assign]
    runtime._fake_sandbox = sandbox  # exposed for provider-level assertions
    return runtime
