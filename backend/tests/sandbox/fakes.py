"""Shared test doubles for the sandbox runtime interface.

Two layers:

* ``FakeFileSystem`` / ``FakePtyHandle`` / ``FakeSandboxRuntime`` implement the
  provider-neutral protocols in memory. Consumer tests inject them; the conformance suite
  runs them so a test double can never drift from the contract real providers meet.
* ``FakeDaytona*`` mimic the Daytona SDK object graph (client → sandbox → fs/process) so the
  real ``DaytonaRuntime`` adapter can be exercised without the network.
"""

from __future__ import annotations

import queue
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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


# --- Docker SDK-level doubles ----------------------------------------------------------
#
# Mirror the slice of docker-py ``DockerRuntime`` uses: ``client.containers/images/
# networks``, ``client.api.exec_create/exec_start/exec_inspect``. Commands arrive wrapped
# (``/bin/sh -c <wrapper> <pidfile> <command>``); the fake runs ``exec_handler`` on the
# user's command — the last argv element — and records the wrapper argv for assertions.


@dataclass
class DockerExecResult:
    """What a fake exec produces: stdout/stderr chunks (streamed in order) and exit code."""

    exit_code: int = 0
    chunks: tuple[tuple[bytes | None, bytes | None], ...] = ()
    block: Any = None  # a threading.Event the exec waits on (simulates a hung command)


DockerExecHandler = Callable[[str], "RuntimeCommandResult | DockerExecResult"]


@dataclass
class FakeContainerFs:
    """The container's files, driven only by the argv shapes ``DockerFileSystem`` sends
    (``find -printf``, ``mkdir -p -m``, ``mv -T``, ``rm -r|-d``, ``chmod`` and its read /
    search scripts; writes arrive through ``write``). An argv it does not recognise fails
    loudly, so a provider change cannot pass silently. ``readable=False`` paths model files
    the sandbox user may not read."""

    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/home", "/home/daytona"})
    links: dict[str, str] = field(default_factory=dict)
    modes: dict[str, str] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs or path in self.links

    def resolve(self, path: str) -> str:
        return self.links.get(path, path)

    def _children(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            key
            for key in (*self.files, *self.dirs, *self.links)
            if key.startswith(prefix) and "/" not in key[len(prefix) :] and key != "/"
        )

    def _record(self, path: str) -> bytes:
        target = self.resolve(path)
        kind = "l" if path in self.links else ("d" if path in self.dirs else "f")
        target_kind = "d" if target in self.dirs else ("f" if target in self.files else "N")
        size = len(self.files.get(target, b"")) if kind != "l" else len(self.links[path])
        mode = self.modes.get(path, "755" if kind == "d" else "644")
        fields = [kind, target_kind, str(size), "1700000000.5", mode, "-rw-r--r--"]
        fields += ["daytona", "daytona", path]
        return b"".join(value.encode("utf-8", "surrogateescape") + b"\0" for value in fields)

    def _subtree(self, path: str) -> list[str]:
        return [k for k in (*self.files, *self.dirs, *self.links) if k.startswith(path + "/")]

    def run(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        missing = (1, b"", b"No such file or directory")
        match argv:
            case ["find", "-H", path, "-mindepth", "1", "-maxdepth", "1", "-printf", _]:
                path = self.resolve(path)
                if path not in self.dirs:
                    return missing
                return 0, b"".join(self._record(child) for child in self._children(path)), b""
            case ["find", path, "-maxdepth", "0", "-printf", _]:
                return (0, self._record(path), b"") if self.exists(path) else missing
            case ["mkdir", "-p", "-m", mode, "--", path]:
                if path in self.files:
                    return 1, b"", b"File exists"
                while path not in self.dirs:
                    self.dirs.add(path)
                    self.modes.setdefault(path, mode)
                    path = dirname(path)
                return 0, b"", b""
            case ["mv", "-T", "--", source, destination]:
                if not self.exists(source):
                    return missing
                for store in (self.files, self.links, self.modes):
                    for key in [k for k in store if k == source or k.startswith(source + "/")]:
                        store[destination + key[len(source) :]] = store.pop(key)
                for key in [k for k in self.dirs if k == source or k.startswith(source + "/")]:
                    self.dirs.remove(key)
                    self.dirs.add(destination + key[len(source) :])
                return 0, b"", b""
            case ["rm", flag, "--", path] if flag in ("-r", "-d"):
                if not self.exists(path):
                    return missing
                below = self._subtree(path)
                if flag == "-d" and below:
                    return 1, b"", b"Directory not empty"
                for key in [path, *below]:
                    for store in (self.files, self.links, self.modes):
                        store.pop(key, None)
                    self.dirs.discard(key)
                return 0, b"", b""
            case ["chmod", mode, "--", path]:
                if not self.exists(path):
                    return missing
                self.modes[self.resolve(path)] = mode
                return 0, b"", b""
            case ["/bin/sh", "-c", script, "sh", path, cap] if (
                "head -c" in script and "-f" in script
            ):
                target = self.resolve(path)
                if target in self.unreadable:
                    return 1, b"", b"head: cannot open: Permission denied"
                if target in self.files:
                    return 0, self.files[target][: int(cap)], b""
                if self.exists(path):
                    return 1, b"", b"not a regular file"
                return missing
            case ["/bin/sh", "-c", script, "sh", pattern, path, cap] if "grep" in script:
                if not self.exists(path):
                    return 2, b"", b"No such file or directory"
                out = b""
                for key in sorted(k for k in self.files if k == path or k.startswith(path + "/")):
                    for number, line in enumerate(self.files[key].split(b"\n"), 1):
                        if pattern.encode() in line:
                            out += key.encode("utf-8", "surrogateescape") + b"\0"
                            out += str(number).encode() + b":" + line + b"\n"
                return 0, out[: int(cap)], b""
        raise AssertionError(f"FakeContainerFs does not understand {argv!r}")

    def write(self, path: str, parent: str, data: bytes) -> tuple[int, bytes]:
        """``mkdir -p parent && head -c n > path`` as the sandbox user."""
        target = self.resolve(path)
        if path in self.dirs or target in self.dirs:
            return 1, b"sh: cannot create: Is a directory"
        if target in self.unreadable:
            return 1, b"sh: cannot create: Permission denied"
        self.run(["mkdir", "-p", "-m", "755", "--", parent])
        self.modes.setdefault(target, "644")  # truncating keeps an existing mode
        self.files[target] = data
        return 0, b""


class FakeStdinSocket:
    """The socket of an upload exec: bytes sent are the file; reading runs the command and
    returns its (multiplexed) stderr, then EOF."""

    def __init__(self, on_complete: Callable[[bytes], bytes]) -> None:
        self.received = bytearray()
        self._on_complete = on_complete
        self._pending: bytes | None = None

    def settimeout(self, value: float) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        self.received.extend(data)

    def recv(self, size: int) -> bytes:
        if self._pending is None:
            stderr = self._on_complete(bytes(self.received))
            frame = b"\x02\0\0\0" + len(stderr).to_bytes(4, "big") + stderr
            self._pending = frame if stderr else b""
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def shutdown(self, how: int) -> None:
        return None


class FakePtySocket:
    """The raw socket ``exec_start(socket=True)`` returns: input echoes back as output."""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._out: queue.Queue[bytes] = queue.Queue()
        self._out.put(b"$ ")

    def recv(self, size: int) -> bytes:
        return self._out.get(timeout=10)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)
        self._out.put(data)

    def shutdown(self, how: int) -> None:
        self.closed = True
        self._out.put(b"")


class FakeDockerContainer:
    def __init__(self, client: FakeDockerClient, spec: dict[str, Any]) -> None:
        self.client = client
        self.spec = spec
        self.id = f"ctr-{uuid4().hex[:12]}"
        self.name = spec["name"]
        self.labels = dict(spec.get("labels") or {})
        self.status = "running"
        self.start_calls = 0
        self.removed = False
        self.exec_runs: list[list[str]] = []
        self.fs = FakeContainerFs()
        self.tmux_sessions: set[str] = set()
        # pidfile -> socket of the attached tmux client it names
        self.tmux_clients: dict[str, FakePtySocket] = {}

    def reload(self) -> None:
        if self.removed:
            raise _docker_not_found()

    def start(self) -> None:
        self.start_calls += 1
        self.status = "running"

    def unpause(self) -> None:
        self.status = "running"

    def stop(self, timeout: int = 10) -> None:
        self.status = "exited"

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.client.containers.by_id.pop(self.id, None)

    def exec_run(self, cmd: list[str], workdir: str | None = None, demux: bool = False) -> Any:
        self.exec_runs.append(list(cmd))
        if demux:
            code, stdout, stderr = self.fs.run(list(cmd))
            return SimpleNamespace(exit_code=code, output=(stdout or None, stderr or None))
        if len(cmd) == 4 and cmd[:2] == ["/bin/sh", "-c"] and "kill -s TERM" in cmd[2]:
            self.client.killed_pidfiles.append(cmd[3])
            event = self.client.blocked_by_pidfile.get(cmd[3])
            if event is not None:
                event.set()
        if len(cmd) == 4 and cmd[:2] == ["/bin/sh", "-c"] and "kill -s HUP" in cmd[2]:
            sock = self.tmux_clients.pop(cmd[3], None)
            if sock is not None:
                sock.shutdown(0)
        if cmd[:3] == ["tmux", "kill-session", "-t"]:
            name = cmd[3].removeprefix("=")
            if name not in self.tmux_sessions:
                return SimpleNamespace(exit_code=1, output=b"can't find session")
            self.tmux_sessions.discard(name)
            return SimpleNamespace(exit_code=0, output=b"")
        if cmd[:2] == ["mkdir", "-p"] and self.client.fail_mkdir:
            return SimpleNamespace(exit_code=1, output=b"read-only")
        if cmd[:3] == ["mkdir", "-p", "--"]:
            self.fs.run(["mkdir", "-p", "-m", "755", "--", cmd[3]])
        return SimpleNamespace(exit_code=0, output=b"")


class _FakeContainers:
    def __init__(self, client: FakeDockerClient) -> None:
        self.client = client
        self.by_id: dict[str, FakeDockerContainer] = {}
        self.run_specs: list[dict[str, Any]] = []

    def get(self, key: str) -> FakeDockerContainer:
        for container in self.by_id.values():
            if key in (container.id, container.name):
                return container
        raise _docker_not_found()

    def run(self, **spec: Any) -> FakeDockerContainer:
        self.run_specs.append(spec)
        container = FakeDockerContainer(self.client, spec)
        self.by_id[container.id] = container
        return container

    def list(self, all: bool = False, filters: dict | None = None) -> list[FakeDockerContainer]:
        return list(self.by_id.values())


class _FakeImages:
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def get(self, name: str) -> Any:
        if name not in self.names:
            from docker.errors import ImageNotFound

            raise ImageNotFound(f"No such image: {name}")
        return SimpleNamespace(tags=[name])


class _FakeNetworks:
    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.created: list[str] = []

    def list(self, names: list[str] | None = None) -> list[Any]:
        # Docker's ``name`` filter is a substring match, not an exact one.
        wanted = names or []
        return [SimpleNamespace(name=n) for n in self.names if any(w in n for w in wanted)]

    def create(self, name: str, driver: str = "bridge", labels: dict | None = None) -> Any:
        self.created.append(name)
        self.names.add(name)
        return SimpleNamespace(name=name)


class _FakeDockerAPI:
    def __init__(self, client: FakeDockerClient) -> None:
        self.client = client
        self.execs: dict[str, dict[str, Any]] = {}
        self.resizes: list[tuple[str, int | None, int | None]] = []
        self.closed = False
        self.api_version = "1.41"

    def exec_create(self, container_id, cmd, environment=None, workdir=None, tty=False, **_: Any):
        exec_id = f"exec-{len(self.execs)}"
        self.execs[exec_id] = {
            "container": container_id,
            "cmd": list(cmd),
            "environment": environment,
            "workdir": workdir,
            "tty": tty,
            "exit_code": None,
            "running": False,
            "finished": False,
        }
        return {"Id": exec_id}

    def _attach_tmux(self, exec_id: str) -> FakePtySocket:
        record = self.execs[exec_id]
        cmd = record["cmd"]
        container = self.client.containers.get(record["container"])
        container.tmux_sessions.add(cmd[cmd.index("-s") + 1])
        sock = container.tmux_clients[cmd[3]] = FakePtySocket()
        record["socket"] = sock
        return sock

    def exec_resize(self, exec_id: str, height: int | None = None, width: int | None = None):
        self.resizes.append((exec_id, width, height))

    def _result(self, exec_id: str) -> DockerExecResult:
        record = self.execs[exec_id]
        handler = self.client.exec_handler
        outcome = handler(record["cmd"][-1]) if handler else RuntimeCommandResult(0, "", "")
        if isinstance(outcome, RuntimeCommandResult):
            outcome = DockerExecResult(
                exit_code=outcome.exit_code,
                chunks=(
                    (outcome.stdout.encode() or None, None),
                    (None, outcome.stderr.encode() or None),
                ),
            )
        if outcome.block is not None:
            self.client.blocked_by_pidfile[record["cmd"][3]] = outcome.block
            self.client.blocked.append(outcome.block)
            outcome.block.wait(10)
        record["exit_code"] = outcome.exit_code
        return outcome

    def _upload(self, exec_id: str) -> FakeStdinSocket:
        record = self.execs[exec_id]
        container = self.client.containers.get(record["container"])
        _, _, _, _, path, parent, size = record["cmd"]
        record["running"] = True

        def complete(data: bytes) -> bytes:
            code, stderr = container.fs.write(path, parent, data[: int(size)])
            record["exit_code"], record["running"] = code, False
            return stderr

        return FakeStdinSocket(complete)

    def exec_start(self, exec_id, stream=False, demux=False, socket=False, **_: Any):
        if socket:
            if "tmux" in self.execs[exec_id]["cmd"]:
                return SimpleNamespace(_sock=self._attach_tmux(exec_id))
            return SimpleNamespace(_sock=self._upload(exec_id))
        record = self.execs[exec_id]
        record["running"] = True
        if stream and "[ -f" in record["cmd"][2]:  # DockerFileSystem.download_file
            container = self.client.containers.get(record["container"])
            container.exec_runs.append(list(record["cmd"]))
            code, out, err = container.fs.run(list(record["cmd"]))
            record["exit_code"], record["running"], record["finished"] = code, False, True
            return iter([(out[:3] or None, None), (out[3:] or None, err or None)])
        if not stream:
            try:
                outcome = self._result(exec_id)
                return b"".join((out or b"") + (err or b"") for out, err in outcome.chunks)
            finally:
                record["running"], record["finished"] = False, True

        def generate():
            try:
                outcome = self._result(exec_id)
                for out, err in outcome.chunks:
                    if out or err:
                        yield (out, err)
            finally:
                record["running"], record["finished"] = False, True

        return generate()

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        record = self.execs[exec_id]
        return {"ExitCode": record["exit_code"], "Running": record["running"]}

    def close(self) -> None:
        self.closed = True


class FakeDockerClient:
    def __init__(
        self,
        *,
        images: set[str] | None = None,
        networks: set[str] | None = None,
        exec_handler: DockerExecHandler | None = None,
    ) -> None:
        self.containers = _FakeContainers(self)
        self.images = _FakeImages(images if images is not None else {"enigma-sandbox:latest"})
        self.networks = _FakeNetworks(networks if networks is not None else {"enigma-sandboxes"})
        self.api = _FakeDockerAPI(self)
        self.exec_handler = exec_handler
        self.killed_pidfiles: list[str] = []
        self.blocked: list[Any] = []
        self.blocked_by_pidfile: dict[str, Any] = {}
        self.fail_mkdir = False
        self.closed = False

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def add_container(self, name: str, *, status: str = "running", labels: dict | None = None):
        container = self.containers.run(name=name, labels=labels or {})
        self.containers.run_specs.clear()
        container.status = status
        return container


def _docker_not_found():
    from docker.errors import NotFound

    return NotFound("No such container")


def docker_runtime_with(client: FakeDockerClient, **settings: Any):
    """A real ``DockerRuntime`` wired to the fake client (both control and exec APIs)."""
    from app.core.config import Settings
    from app.services.sandbox.docker import DockerRuntime

    runtime = DockerRuntime(Settings(docker_host="unix:///fake/docker.sock", **settings))
    runtime._client = client
    runtime._exec_api = client.api
    return runtime
