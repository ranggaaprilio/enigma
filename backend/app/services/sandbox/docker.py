"""Local Docker (or Podman) provider — the only module that imports the ``docker`` SDK.

One long-lived container per workspace (PID 1 is an init + ``sleep infinity``); every
operation is a ``docker exec`` into it, OpenHands-style. The SDK is synchronous, so calls
run on this runtime's own thread pools — never the loop's default executor, which a few
long-running streams would otherwise exhaust for the whole process:

* ``exec`` pool — one thread per in-flight command for its whole life (bounded by
  ``DOCKER_MAX_CONCURRENT_EXECS``);
* ``control`` pool — short calls (inspect, start, create, the kill exec), so stopping a
  command can never queue behind the commands it is trying to stop.

Commands run as ``timeout -k <grace> <secs> /bin/sh -c <command>`` (``0`` = no limit).
GNU ``timeout`` makes itself a process-group leader and signals the whole group, so a
timed-out command dies with its children inside the container. Its pid is written to a
pidfile so a second exec can kill the same group when the caller is cancelled (shutdown
drain) or the host-side guard fires — the equivalent of Daytona deleting the session.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from posixpath import join as posix_join
from typing import Any
from uuid import uuid4

import docker
import requests
import structlog
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.base import (
    CommandTimeoutError,
    ProvisioningError,
    RuntimeCommandResult,
    RuntimePreviewLink,
    RuntimeUnavailableError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)

logger = structlog.get_logger(__name__)

DEFAULT_DOCKER_HOST = "unix:///var/run/docker.sock"
MANAGED_LABEL = "enigma.managed"
WORKSPACE_LABEL = "enigma.workspace-id"
OWNER_LABEL = "enigma.owner-id"

# Seconds between SIGTERM and SIGKILL, in-container and in the cancel path.
KILL_GRACE_SECONDS = 5
# Extra host-side slack before giving up on a command whose in-container timeout failed.
HOST_GUARD_SLACK_SECONDS = 15
CONTROL_POOL_SIZE = 8
_EXEC_DIR = "/tmp/.enigma-exec"
_GNU_TIMEOUT_EXIT_CODES = {124, 137}

_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_STATUS = {
    "running": "ready",
    "created": "creating",
    "restarting": "creating",
    "paused": "stopped",
    "exited": "stopped",
}

# $0 = pidfile, $1 = the user's command. A ``<pidfile>.cancel`` tombstone left by the
# killer stops a command whose exec had not started yet. ``timeout`` runs as a background
# job + ``wait`` so the pidfile names ``timeout`` itself (the group leader).
_WRAPPER = (
    f"mkdir -p {_EXEC_DIR}; "
    '[ -e "$0.cancel" ] && {{ rm -f "$0.cancel"; exit 143; }}; '
    'timeout -k {grace} {secs} /bin/sh -c "$1" & pid=$!; '
    'echo "$pid" > "$0"; wait "$pid"; rc=$?; rm -f "$0" "$0.cancel"; exit "$rc"'
)
# TERM the group (or, if ``timeout`` has not called setpgid yet, the pid itself), wait up
# to the grace period for it to go, KILL only what is left.
_KILLER = (
    'touch "$0.cancel"; '
    'i=0; while [ ! -s "$0" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i+1)); done; '
    'pid=$(cat "$0" 2>/dev/null) || exit 0; '
    'kill -s TERM -- "-$pid" 2>/dev/null || kill -s TERM "$pid" 2>/dev/null; '
    'i=0; while kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null; do '
    '[ "$i" -ge {ticks} ] && {{ kill -s KILL -- "-$pid" 2>/dev/null; '
    'kill -s KILL "$pid" 2>/dev/null; break; }}; sleep 0.1; i=$((i+1)); done; '
    'rm -f "$0" "$0.cancel"; exit 0'
)

_NOT_YET = "is not available on the Docker sandbox runtime yet"


def map_docker_error(error: Exception) -> SandboxRuntimeError:
    """Provider errors → neutral taxonomy. Messages never carry the daemon URL/socket."""
    if isinstance(error, SandboxRuntimeError):
        return error
    if isinstance(error, NotFound):
        return SandboxNotFoundError("Docker sandbox was not found")
    if isinstance(error, APIError):
        return SandboxRuntimeError(f"Docker error: {error.explanation or error.status_code}")
    if isinstance(error, (requests.exceptions.RequestException, DockerException)):
        return RuntimeUnavailableError("Docker is unavailable")
    return SandboxRuntimeError(f"Docker operation failed ({type(error).__name__})")


def _validate_env(env: dict[str, str] | None) -> list[str] | None:
    if not env:
        return None
    for key in env:
        if not _ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
    return [f"{key}={value}" for key, value in sorted(env.items())]


def _is_root_user(user: str) -> bool:
    name = user.strip().split(":", 1)[0].strip()
    if not name or name == "root":
        return True
    try:
        return int(name, 0) == 0
    except ValueError:
        return False


def assert_safe_container_spec(spec: dict[str, Any]) -> None:
    """Day-one floor for every sandbox container (the full profile is Epic 05).

    The Docker socket is root-equivalent on the host, so a sandbox gets no mounts at all;
    it runs unprivileged, as a non-root user, on a dedicated bridge network, with every
    capability dropped, a pid limit, and ``no-new-privileges`` (which also defuses the
    base image's passwordless sudo).
    """
    problems = [
        key
        for key in ("volumes", "mounts", "devices", "cap_add", "volumes_from", "network_mode")
        if spec.get(key)
    ]
    if spec.get("privileged"):
        problems.append("privileged")
    for key in ("pid_mode", "ipc_mode", "userns_mode", "uts_mode", "cgroupns"):
        if str(spec.get(key) or "").lower() == "host":
            problems.append(key)
    network = str(spec.get("network") or "")
    if not network or network in {"host", "none", "bridge"} or network.startswith("container:"):
        problems.append("network")
    if _is_root_user(str(spec.get("user") or "")):
        problems.append("user")
    security_opt = [str(opt).replace(":", "=") for opt in spec.get("security_opt") or []]
    if "no-new-privileges=true" not in security_opt or any(
        opt.endswith("=unconfined") for opt in security_opt
    ):
        problems.append("security_opt")
    if spec.get("cap_drop") != ["ALL"]:
        problems.append("cap_drop")
    if not isinstance(spec.get("pids_limit"), int) or spec["pids_limit"] <= 0:
        problems.append("pids_limit")
    if problems:
        raise ProvisioningError(f"Refusing unsafe sandbox container spec: {sorted(problems)}")


def _not_yet(what: str) -> RuntimeUnavailableError:
    return RuntimeUnavailableError(f"{what} {_NOT_YET}")


class DockerRuntime:
    provider = "docker"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None  # docker.DockerClient: control calls, bounded timeout
        self._exec_api: Any = None  # docker.APIClient: exec streams, no read timeout
        self._exec_pool: ThreadPoolExecutor | None = None
        self._control_pool: ThreadPoolExecutor | None = None
        # Guards first connect; asyncio locks bind to one loop, so keep one per loop.
        self._locks: dict[int, asyncio.Lock] = {}

    # --- threads & clients ------------------------------------------------------------

    def _pool(self, kind: str) -> ThreadPoolExecutor:
        if kind == "exec":
            if self._exec_pool is None:
                self._exec_pool = ThreadPoolExecutor(
                    max_workers=self.settings.docker_max_concurrent_execs,
                    thread_name_prefix="docker-exec",
                )
            return self._exec_pool
        if self._control_pool is None:
            self._control_pool = ThreadPoolExecutor(
                max_workers=CONTROL_POOL_SIZE, thread_name_prefix="docker-control"
            )
        return self._control_pool

    async def _run(self, kind: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool(kind), partial(func, *args, **kwargs))
        except SandboxRuntimeError:
            raise
        except Exception as error:
            raise map_docker_error(error) from error

    async def _call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await self._run("control", func, *args, **kwargs)

    @property
    def _base_url(self) -> str:
        return self.settings.docker_host or DEFAULT_DOCKER_HOST

    @property
    def configured(self) -> bool:
        if self.settings.docker_host:
            return True
        return os.path.exists(DEFAULT_DOCKER_HOST.removeprefix("unix://"))

    def _connect(self) -> tuple[Any, Any]:
        client = docker.DockerClient(
            base_url=self._base_url,
            version="auto",
            timeout=self.settings.docker_provision_timeout_seconds,
            max_pool_size=CONTROL_POOL_SIZE,
        )
        # A command can be silent for longer than any sane request timeout, so the exec
        # stream gets its own client without one, pooled for every concurrent exec.
        exec_api = docker.APIClient(
            base_url=self._base_url,
            version=client.api.api_version,
            timeout=None,
            max_pool_size=self.settings.docker_max_concurrent_execs,
        )
        return client, exec_api

    async def _clients(self) -> tuple[Any, Any]:
        if self._client is None:
            loop = asyncio.get_running_loop()
            lock = self._locks.setdefault(id(loop), asyncio.Lock())
            async with lock:
                if self._client is None:
                    try:
                        client, exec_api = await loop.run_in_executor(
                            self._pool("control"), self._connect
                        )
                    except Exception as error:
                        raise RuntimeUnavailableError("Docker is unavailable") from error
                    self._client, self._exec_api = client, exec_api
        return self._client, self._exec_api

    async def close(self) -> None:
        client, exec_api = self._client, self._exec_api
        self._client = self._exec_api = None
        for closable in (exec_api, client):
            if closable is not None:
                await asyncio.to_thread(closable.close)
        for pool in (self._exec_pool, self._control_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self._exec_pool = self._control_pool = None
        self._locks.clear()

    async def health(self) -> str:
        client, _ = await self._clients()
        await self._call(client.ping)
        return "ok"

    # --- lifecycle --------------------------------------------------------------------

    def _home(self) -> str:
        return self.settings.daytona_sandbox_home.rstrip("/") or "/"

    def _resolve_cwd(self, workspace: Workspace, cwd: str | None) -> str:
        path = cwd or workspace.workspace_path or ""
        return path if path.startswith("/") else posix_join(self._home(), path)

    def _container_spec(self, workspace: Workspace, name: str) -> dict[str, Any]:
        return {
            # Operator-chosen only: ``workspace.snapshot`` is user-settable and names a
            # Daytona snapshot, so it never selects a local image.
            "image": self.settings.docker_sandbox_image,
            "name": name,
            "detach": True,
            # tini/catatonit as PID 1 reaps the orphans that backgrounded dev servers leave.
            "init": True,
            "entrypoint": ["sleep", "infinity"],
            "command": [],
            "user": self.settings.docker_sandbox_user,
            "working_dir": self._home(),
            "labels": {
                MANAGED_LABEL: "1",
                WORKSPACE_LABEL: str(workspace.id),
                OWNER_LABEL: str(workspace.owner_id),
            },
            "network": self.settings.docker_sandbox_network,
            "mem_limit": f"{self.settings.docker_sandbox_memory_mb}m",
            "nano_cpus": int(self.settings.docker_sandbox_cpus * 1_000_000_000),
            "pids_limit": self.settings.docker_sandbox_pids_limit,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }

    def _ensure_network(self, client: Any) -> None:
        name = self.settings.docker_sandbox_network
        # The ``name`` filter is a substring match; only an exact name counts.
        if any(network.name == name for network in client.networks.list(names=[name])):
            return
        try:
            client.networks.create(name, driver="bridge", labels={MANAGED_LABEL: "1"})
        except APIError as error:
            if error.status_code != 409:  # another process created it first
                raise

    @staticmethod
    def _ensure_running(container: Any) -> Any:
        container.reload()
        if container.status == "paused":
            container.unpause()
        elif container.status != "running":
            container.start()
        container.reload()
        return container

    def _ensure_workspace_dir(self, container: Any, workspace: Workspace) -> None:
        # Parity with the Daytona provision's create_folder(workspace_path, "755").
        result = container.exec_run(
            ["mkdir", "-p", "--", self._resolve_cwd(workspace, None)], workdir=self._home()
        )
        if result.exit_code != 0:
            raise ProvisioningError("Could not create the workspace directory")

    def _provision_sync(self, client: Any, workspace: Workspace) -> str:
        name = f"execute-{workspace.id.hex}"
        try:
            existing = client.containers.get(name)
        except NotFound:
            existing = None
        if existing is not None:
            labels = existing.labels or {}
            if labels.get(MANAGED_LABEL) != "1" or labels.get(WORKSPACE_LABEL) != str(workspace.id):
                raise ProvisioningError(
                    "A container with this workspace's sandbox name exists but was not "
                    "created by Enigma for it; remove it and retry"
                )
            container = self._ensure_running(existing)
            self._ensure_workspace_dir(container, workspace)
            return container.id
        image = self.settings.docker_sandbox_image
        try:
            client.images.get(image)
        except ImageNotFound as error:
            raise ProvisioningError(
                "The sandbox image is not built; run sandbox/build-local-image.sh"
            ) from error
        self._ensure_network(client)
        spec = self._container_spec(workspace, name)
        assert_safe_container_spec(spec)
        container = client.containers.run(**spec)
        try:
            self._ensure_workspace_dir(container, workspace)
        except Exception:
            container.remove(force=True)  # no half-made sandbox for the next retry to adopt
            raise
        return container.id

    async def provision(self, workspace: Workspace) -> str:
        client, _ = await self._clients()
        try:
            return await self._call(self._provision_sync, client, workspace)
        except (RuntimeUnavailableError, ProvisioningError):
            raise
        except SandboxRuntimeError as error:
            raise ProvisioningError(str(error)) from error

    async def _container(self, workspace: Workspace, *, start: bool = True) -> Any:
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no Docker sandbox")
        client, _ = await self._clients()
        container = await self._call(client.containers.get, workspace.sandbox_id)
        if start:
            container = await self._call(self._ensure_running, container)
        return container

    async def status(self, workspace: Workspace) -> str:
        container = await self._container(workspace, start=False)
        await self._call(container.reload)
        return _STATUS.get(str(container.status or "").lower(), "error")

    async def start(self, workspace: Workspace) -> None:
        await self._container(workspace)

    async def delete(self, workspace: Workspace) -> None:
        if not workspace.sandbox_id:
            return
        client, _ = await self._clients()
        try:
            container = await self._call(client.containers.get, workspace.sandbox_id)
            await self._call(container.remove, force=True)
        except SandboxNotFoundError:
            return

    # --- commands ---------------------------------------------------------------------

    @staticmethod
    def _wrap(command: str, timeout: int | None, pidfile: str) -> list[str]:
        script = _WRAPPER.format(grace=KILL_GRACE_SECONDS, secs=max(0, int(timeout or 0)))
        return ["/bin/sh", "-c", script, pidfile, command]

    async def _create_exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int | None,
    ) -> tuple[Any, str, str]:
        environment = _validate_env(env)
        container = await self._container(workspace)
        client, _ = await self._clients()
        pidfile = f"{_EXEC_DIR}/{uuid4().hex}.pid"
        created = await self._call(
            client.api.exec_create,
            container.id,
            self._wrap(command, timeout, pidfile),
            environment=environment,
            workdir=self._resolve_cwd(workspace, cwd),
        )
        return container, created["Id"], pidfile

    async def _kill(self, container: Any, exec_id: str, pidfile: str) -> None:
        script = _KILLER.format(ticks=KILL_GRACE_SECONDS * 10)
        try:
            client, _ = await self._clients()
            state = await self._call(client.api.exec_inspect, exec_id)
            if not state.get("Running") and state.get("ExitCode") is not None:
                return  # already finished: nothing to kill
            await asyncio.wait_for(
                self._call(container.exec_run, ["/bin/sh", "-c", script, pidfile]),
                timeout=KILL_GRACE_SECONDS + 15,
            )
        except Exception as error:  # the container may already be gone
            logger.warning(
                "docker_exec_kill_failed", error_type=type(error).__name__, exec_id=exec_id
            )

    async def _finish(
        self,
        work: Awaitable[Any],
        container: Any,
        exec_id: str,
        pidfile: str,
        timeout: int | None,
        abandoned: threading.Event | None = None,
    ) -> Any:
        """Await the exec; on host-side timeout or cancellation stop delivering output
        (``abandoned``) and kill the command's process group inside the container.

        ``work`` maps every error to ``SandboxRuntimeError`` (``_run``), so a
        ``TimeoutError`` here can only be the host guard's."""
        guard = None if not timeout else timeout + KILL_GRACE_SECONDS + HOST_GUARD_SLACK_SECONDS
        try:
            return await asyncio.wait_for(work, timeout=guard)
        except TimeoutError as error:
            if abandoned is not None:
                abandoned.set()
            await asyncio.shield(self._kill(container, exec_id, pidfile))
            raise CommandTimeoutError("Command timed out") from error
        except asyncio.CancelledError:
            if abandoned is not None:
                abandoned.set()
            await asyncio.shield(self._kill(container, exec_id, pidfile))
            raise

    async def _exit_code(self, exec_id: str, timeout: int | None, started: float) -> int:
        client, _ = await self._clients()
        inspected = await self._call(client.api.exec_inspect, exec_id)
        # The stream can end a moment before the daemon records the exit code.
        for _ in range(20):
            if not inspected.get("Running") and inspected.get("ExitCode") is not None:
                break
            await asyncio.sleep(0.05)
            inspected = await self._call(client.api.exec_inspect, exec_id)
        code = inspected.get("ExitCode")
        if (
            timeout
            and code in _GNU_TIMEOUT_EXIT_CODES
            and time.monotonic() - started >= timeout - 0.5
        ):
            raise CommandTimeoutError("Command timed out")
        return code if code is not None else 1

    async def exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> RuntimeCommandResult:
        container, exec_id, pidfile = await self._create_exec(
            workspace, command, cwd=cwd, env=env, timeout=timeout
        )
        _, exec_api = await self._clients()
        started = time.monotonic()
        output = await self._finish(
            self._run("exec", exec_api.exec_start, exec_id, stream=False, demux=False),
            container,
            exec_id,
            pidfile,
            timeout,
        )
        exit_code = await self._exit_code(exec_id, timeout, started)
        # Combined output in stdout, empty stderr — the Daytona provider's shape exactly.
        return RuntimeCommandResult(
            exit_code=exit_code,
            stdout=(output or b"").decode("utf-8", errors="replace"),
            stderr="",
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
        container, exec_id, pidfile = await self._create_exec(
            workspace, command, cwd=cwd, env=env, timeout=timeout
        )
        _, exec_api = await self._clients()
        loop = asyncio.get_running_loop()
        abandoned = threading.Event()

        def deliver(callback: Callable[[str], Awaitable[None]], text: str) -> bool:
            """Hand a chunk to the consumer and wait for it (backpressure). False once the
            caller has gone, so the pump stops instead of blocking on a dead consumer."""
            if not text:
                return True
            if abandoned.is_set():
                return False
            future = asyncio.run_coroutine_threadsafe(callback(text), loop)
            while True:
                try:
                    future.result(timeout=0.5)
                    return True
                except TimeoutError:
                    if abandoned.is_set() or loop.is_closed():
                        future.cancel()
                        return False

        def pump() -> None:
            decoders = (
                codecs.getincrementaldecoder("utf-8")(errors="replace"),
                codecs.getincrementaldecoder("utf-8")(errors="replace"),
            )
            callbacks = (on_stdout, on_stderr)
            for chunks in exec_api.exec_start(exec_id, stream=True, demux=True):
                for decoder, callback, chunk in zip(decoders, callbacks, chunks, strict=True):
                    if chunk and not deliver(callback, decoder.decode(chunk)):
                        return
            for decoder, callback in zip(decoders, callbacks, strict=True):
                deliver(callback, decoder.decode(b"", final=True))

        started = time.monotonic()
        await self._finish(self._run("exec", pump), container, exec_id, pidfile, timeout, abandoned)
        return await self._exit_code(exec_id, timeout, started)

    # --- surfaces that land in later epics --------------------------------------------

    def _require_sandbox(self, workspace: Workspace) -> None:
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no Docker sandbox")

    async def fs(self, workspace: Workspace) -> Any:
        self._require_sandbox(workspace)
        raise _not_yet("File access")  # Epic 03

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
    ) -> Any:
        self._require_sandbox(workspace)
        raise _not_yet("The terminal")  # Epic 03

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        return None  # no PTY sessions exist yet; killing an unknown session is a no-op

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        self._require_sandbox(workspace)
        raise _not_yet("App preview")  # Epic 04


__all__ = ["DockerRuntime", "assert_safe_container_spec", "map_docker_error"]
