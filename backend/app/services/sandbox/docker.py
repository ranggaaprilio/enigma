"""Local Docker (or Podman) provider — the only module that imports the ``docker`` SDK.

One long-lived container per workspace (PID 1 is an init + ``sleep infinity``); every
operation is a ``docker exec`` into it, OpenHands-style. The SDK is synchronous, so calls
run on this runtime's own thread pools — never the loop's default executor, which a few
long-running streams would otherwise exhaust for the whole process:

* ``exec`` pool — one thread per in-flight command for its whole life (bounded by
  ``DOCKER_MAX_CONCURRENT_EXECS``);
* ``control`` pool — short calls (inspect, start, create, the kill exec), so stopping a
  command can never queue behind the commands it is trying to stop;
* ``pty`` pool — one reader thread per open terminal, for the terminal's whole life.

Commands run as ``timeout -k <grace> <secs> /bin/sh -c <command>`` (``0`` = no limit).
GNU ``timeout`` makes itself a process-group leader and signals the whole group, so a
timed-out command dies with its children inside the container. Its pid is written to a
pidfile so a second exec can kill the same group when the caller is cancelled (shutdown
drain) or the host-side guard fires — the equivalent of Daytona deleting the session.

File access (``docker_fs.py``: argv execs + tar archives) and terminals
(``docker_pty.py``: tmux sessions that survive a reconnect) live in sibling modules.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import os
import re
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from posixpath import join as posix_join
from typing import Any
from uuid import UUID, uuid4

import docker
import requests
import structlog
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool, Ulimit

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.activity import SandboxActivityRecorder
from app.services.sandbox.base import (
    CommandTimeoutError,
    ProvisioningError,
    PtySize,
    RuntimeCommandResult,
    RuntimePreviewLink,
    RuntimeUnavailableError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from app.services.sandbox.docker_fs import DockerFileSystem
from app.services.sandbox.docker_pty import (
    TERM,
    DockerPtyHandle,
    attach_argv,
    deliver_threadsafe,
    new_pidfile,
    tmux_session_name,
)
from app.services.sandbox.port_relay import PORT_RELAY_TIMEOUT_SECONDS, port_relay_command

logger = structlog.get_logger(__name__)

DEFAULT_DOCKER_HOST = "unix:///var/run/docker.sock"
MANAGED_LABEL = "enigma.managed"
# This database's id (``sandbox_installation``): the upkeep loop only touches its own.
INSTALLATION_LABEL = "enigma.installation"
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
# Pure-sh (builtins only, nothing a tenant could shadow on PATH): "busy" while an exec or an
# attached terminal client is alive in the sandbox.
_BUSY_SCRIPT = (
    "for f in /tmp/.enigma-exec/*.pid /tmp/.enigma-pty/*.pid; do "
    '[ -f "$f" ] || continue; read pid < "$f" || continue; '
    'kill -0 "$pid" 2>/dev/null && echo busy && exit 0; done; echo idle'
)
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


def container_security_spec(settings: Settings) -> dict[str, Any]:
    """The sandbox container's security and resource profile (Epic 05), in one place.

    Every capability is dropped and none added: git, mise, code-server and dev servers all
    run as the unprivileged sandbox user without one. Adding a capability needs a comment
    and a test. ``no-new-privileges`` also defuses the base image's passwordless sudo.
    ``init`` reaps the orphans that backgrounded dev servers leave. Swap is capped at the
    memory limit, ``/tmp`` is a tmpfs, and the open-file limit is explicit. With
    ``DOCKER_SANDBOX_DISK_MB`` the writable layer gets a size quota.
    """
    memory = f"{settings.docker_sandbox_memory_mb}m"
    disk: dict[str, Any] = {}
    if settings.docker_sandbox_disk_mb:
        disk["storage_opt"] = {"size": f"{settings.docker_sandbox_disk_mb}m"}
    return {
        "user": settings.docker_sandbox_user,
        "init": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": settings.docker_sandbox_pids_limit,
        "mem_limit": memory,
        "memswap_limit": memory,
        "nano_cpus": int(settings.docker_sandbox_cpus * 1_000_000_000),
        "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={settings.docker_sandbox_tmp_mb}m"},
        "ulimits": [
            Ulimit(
                name="nofile",
                soft=settings.docker_sandbox_nofile,
                hard=settings.docker_sandbox_nofile,
            )
        ],
        **disk,
    }


def assert_safe_container(attrs: dict[str, Any]) -> None:
    """The security floor, checked on an existing container's inspect output before it is
    adopted: a hand-edited or drifted container under a sandbox's name is refused."""
    host = attrs.get("HostConfig") or {}
    problems = []
    if host.get("Privileged"):
        problems.append("privileged")
    # Capabilities are checked on the live process (CapEff/CapBnd, see
    # ``_assert_no_capabilities``): Docker and Podman report CapDrop differently.
    if host.get("CapAdd"):
        problems.append("cap_add")
    if not host.get("CapDrop"):
        problems.append("cap_drop")
    options = [str(opt).replace(":", "=", 1) for opt in host.get("SecurityOpt") or []]
    if not any(opt in {"no-new-privileges", "no-new-privileges=true"} for opt in options):
        problems.append("no-new-privileges")
    if any(opt.endswith("=unconfined") for opt in options):
        problems.append("unconfined")
    for opt in options:
        key, _, value = opt.partition("=")
        if key in {"seccomp", "apparmor"} and value not in {"", "default", "docker-default"}:
            problems.append(f"custom {key} profile")
        if key == "label" and value.startswith("disable"):
            problems.append("label=disable")
    for key in ("Devices", "DeviceRequests", "DeviceCgroupRules", "Sysctls", "PortBindings"):
        if host.get(key):
            problems.append(key)
    if host.get("PublishAllPorts"):
        problems.append("PublishAllPorts")
    if not isinstance(host.get("PidsLimit"), int) or host["PidsLimit"] <= 0:
        problems.append("pids_limit")
    mode = str(host.get("NetworkMode") or "")
    if mode == "host" or mode.startswith("container:"):
        problems.append("network")
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"):
        if str(host.get(key) or "").lower() == "host":
            problems.append(key)
    if attrs.get("Mounts"):
        problems.append("mounts")
    if _is_root_user(str((attrs.get("Config") or {}).get("User") or "")):
        problems.append("user")
    if problems:
        raise ProvisioningError(
            "Refusing a sandbox container that drifted from the security floor: "
            f"{sorted(problems)}"
        )


def _parse_capability_mask(status: str, field: str) -> int | None:
    for line in status.splitlines():
        if line.startswith(f"{field}:"):
            try:
                return int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                return None
    return None


def _is_sandbox_address(value: str) -> bool:
    """A private IPv4 a bridge network hands out (not loopback, link-local or unspecified)."""
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return address.is_private and not (
        address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or address.is_reserved
    )


def sandbox_network_name(workspace: Workspace) -> str:
    return f"enigma-sbx-{workspace.id.hex}"


def _network_subnets(network: Any) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    configs = ((network.attrs or {}).get("IPAM") or {}).get("Config") or []
    subnets = []
    for config in configs:
        try:
            subnets.append(ipaddress.ip_network(config.get("Subnet") or "", strict=False))
        except ValueError:
            continue
    return subnets


@dataclass(frozen=True)
class _Proxies:
    preview: list[Any]
    egress: list[Any]


def container_name(workspace: Workspace) -> str:
    """The sandbox's container name: also its DNS name on the sandbox network."""
    return f"execute-{workspace.id.hex}"


class DockerRuntime:
    provider = "docker"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Any = None  # docker.DockerClient: control calls, bounded timeout
        self._exec_api: Any = None  # docker.APIClient: exec streams, no read timeout
        self._exec_pool: ThreadPoolExecutor | None = None
        self._control_pool: ThreadPoolExecutor | None = None
        self._pty_pool: ThreadPoolExecutor | None = None  # one reader per open terminal
        # Guards first connect; asyncio locks bind to one loop, so keep one per loop.
        self._locks: dict[int, asyncio.Lock] = {}
        # The shared idle clock (Epic 05): debounced per instance, stored in Postgres.
        self.activity = SandboxActivityRecorder()
        self._installation: str | None = None  # this database's id, loaded on first use
        self._project: str | None = None  # the backend's own compose project, if any
        # When this process last finished an upkeep pass (Epic 06 /health); None if it runs none.
        self.last_upkeep_at: datetime | None = None
        self._project_known = False

    # --- threads & clients ------------------------------------------------------------

    def _pool(self, kind: str) -> ThreadPoolExecutor:
        if kind == "exec":
            if self._exec_pool is None:
                self._exec_pool = ThreadPoolExecutor(
                    max_workers=self.settings.docker_max_concurrent_execs,
                    thread_name_prefix="docker-exec",
                )
            return self._exec_pool
        if kind == "pty":
            if self._pty_pool is None:
                self._pty_pool = ThreadPoolExecutor(
                    max_workers=self.settings.docker_max_concurrent_execs,
                    thread_name_prefix="docker-pty",
                )
            return self._pty_pool
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
        for pool in (self._exec_pool, self._control_pool, self._pty_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        self._exec_pool = self._control_pool = self._pty_pool = None
        self._locks.clear()

    async def health(self) -> str:
        client, _ = await self._clients()
        await self._call(client.ping)
        return "ok"

    # --- lifecycle --------------------------------------------------------------------

    def _home(self) -> str:
        return self.settings.sandbox_home.rstrip("/") or "/"

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
            "entrypoint": ["sleep", "infinity"],
            "command": [],
            "working_dir": self._home(),
            "labels": {
                MANAGED_LABEL: "1",
                INSTALLATION_LABEL: self._require_installation(),
                WORKSPACE_LABEL: str(workspace.id),
                OWNER_LABEL: str(workspace.owner_id),
            },
            "network": sandbox_network_name(workspace),
            **container_security_spec(self.settings),
        }

    # --- networks: one per sandbox (Epic 05) -------------------------------------------

    def _subnet_candidates(self, workspace: Workspace) -> list[ipaddress.IPv4Network]:
        pool = ipaddress.ip_network(self.settings.docker_sandbox_subnet_pool)
        prefix = max(self.settings.docker_sandbox_subnet_prefix, pool.prefixlen)
        subnets = list(pool.subnets(new_prefix=prefix))
        # Start where this workspace hashes to, so concurrent provisions rarely collide.
        start = workspace.id.int % len(subnets)
        return subnets[start:] + subnets[:start]

    # --- installation scoping ---------------------------------------------------------

    async def _installation_id(self) -> str:
        if self._installation is None:
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import create_async_engine
            from sqlalchemy.pool import NullPool

            from app.models import SandboxInstallation

            # Read once per runtime, on a throwaway connection: a pooled one would be bound
            # to whichever event loop happened to open it.
            engine = create_async_engine(self.settings.database_url, poolclass=NullPool)
            try:
                async with engine.connect() as connection:
                    value = await connection.scalar(select(SandboxInstallation.id))
            finally:
                await engine.dispose()
            if value is None:
                raise RuntimeUnavailableError(
                    "This database has no sandbox installation id; run the migrations"
                )
            self._installation = str(value)
        return self._installation

    def _require_installation(self) -> str:
        if self._installation is None:  # every entry point awaits _installation_id first
            raise RuntimeUnavailableError("The sandbox installation id is not loaded")
        return self._installation

    def _ours(self) -> list[str]:
        """Label filters for this installation's sandboxes and networks -- never another
        stack's on the same daemon, never a container Enigma did not create."""
        return [f"{MANAGED_LABEL}=1", f"{INSTALLATION_LABEL}={self._require_installation()}"]

    def _compose_project(self, client: Any) -> str | None:
        """The compose project of the container this process runs in (None outside one),
        so only this stack's preview proxy is attached to its sandboxes."""
        if not self._project_known:
            try:
                me = client.containers.get(socket.gethostname())
                self._project = (me.labels or {}).get(COMPOSE_PROJECT_LABEL)
            except (NotFound, APIError):
                self._project = None
            self._project_known = True
        return self._project

    def _labelled(self, client: Any, label: str) -> list[Any]:
        filters = [label]
        project = self._compose_project(client)
        if project:
            filters.append(f"{COMPOSE_PROJECT_LABEL}={project}")
        return client.containers.list(filters={"label": filters})

    def _proxies(self, client: Any) -> _Proxies:
        """This stack's preview proxies and egress proxies (Epics 04/05, egress allow-list)."""
        return _Proxies(
            preview=self._labelled(client, self.settings.docker_preview_proxy_label),
            egress=self._labelled(client, self.settings.docker_egress_proxy_label),
        )

    # --- egress policy ------------------------------------------------------------------

    def _egress_via_proxy(self) -> bool:
        """An allow list is enforced by the egress proxy, the sandbox's only way out. As on
        Daytona, ``block_all`` wins: no way out at all, the allow list notwithstanding."""
        return bool(self.settings.sandbox_network_allow_list) and not (
            self.settings.sandbox_network_block_all
        )

    def _network_internal(self) -> bool:
        return bool(self.settings.sandbox_network_block_all) or self._egress_via_proxy()

    def _proxy_env(self) -> dict[str, str]:
        """Points proxy-aware tools (git, curl, npm, pip, uv, mise) at the egress proxy. A
        convenience, not the control: the network is internal, so the proxy is the only
        way out whether or not a command honours these."""
        if not self._egress_via_proxy():
            return {}
        url = (
            f"http://{self.settings.docker_egress_proxy_host}:"
            f"{self.settings.docker_egress_proxy_port}"
        )
        no_proxy = "localhost,127.0.0.1,::1"
        return {
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
            "http_proxy": url,
            "https_proxy": url,
            "NO_PROXY": no_proxy,
            "no_proxy": no_proxy,
        }

    def _environment(self, env: dict[str, str] | None) -> list[str] | None:
        return _validate_env({**self._proxy_env(), **(env or {})})

    # --- networks: one per sandbox (Epic 05) -------------------------------------------

    def _ensure_sandbox_network(self, client: Any, workspace: Workspace) -> Any:
        name = sandbox_network_name(workspace)
        internal = self._network_internal()
        for network in client.networks.list(names=[name]):
            if network.name != name:  # the name filter is a substring match
                continue
            network = client.networks.get(network.id)
            if bool((network.attrs or {}).get("Internal")) == internal:
                return network
            # The egress policy changed since it was made: rebuild it (callers reconnect).
            self._remove_sandbox_network(client, workspace)
            break
        used = [
            subnet for network in client.networks.list() for subnet in _network_subnets(network)
        ]
        attempts = 0
        for subnet in self._subnet_candidates(workspace):
            if any(subnet.overlaps(other) for other in used):
                continue
            attempts += 1
            if attempts > 64:
                break
            try:
                return client.networks.create(
                    name,
                    driver="bridge",
                    internal=internal,
                    labels={
                        MANAGED_LABEL: "1",
                        INSTALLATION_LABEL: self._require_installation(),
                        WORKSPACE_LABEL: str(workspace.id),
                    },
                    ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=str(subnet))]),
                )
            except APIError as error:
                if error.status_code == 409 and "already exists" in str(error).lower():
                    return client.networks.get(name)  # another process created it first
                text = str(getattr(error, "explanation", "") or error).lower()
                if not any(hint in text for hint in ("overlap", "in use", "already used")):
                    raise  # a real daemon error, not a taken subnet
                used.append(subnet)  # taken meanwhile (a race or a hidden network): next one
        raise ProvisioningError(
            "No free sandbox subnet is left; widen DOCKER_SANDBOX_SUBNET_POOL"
        )

    def _attach_proxies(self, network: Any, proxies: _Proxies) -> None:
        """Put this stack's preview proxies on ``network`` under their alias, and its egress
        proxies too while an allow list is in force (taken off again once it is not)."""
        network.reload()
        attached = set((network.attrs or {}).get("Containers") or {})
        wanted = [(proxy, self.settings.docker_preview_proxy_host) for proxy in proxies.preview]
        if self._egress_via_proxy():
            wanted += [(proxy, self.settings.docker_egress_proxy_host) for proxy in proxies.egress]
        else:
            for proxy in proxies.egress:
                if proxy.id in attached:
                    try:
                        network.disconnect(proxy, force=True)
                    except NotFound:
                        pass
        for proxy, alias in wanted:
            if proxy.id in attached:
                continue
            try:
                network.connect(proxy, aliases=[alias])
            except APIError as error:
                if error.status_code != 409:  # 409: it joined meanwhile
                    logger.warning(
                        "docker_proxy_attach_failed", error_type=type(error).__name__
                    )

    def _isolate(
        self, client: Any, container: Any, workspace: Workspace, proxies: _Proxies
    ) -> None:
        """Ensure ``container`` is on its own network only (moving it off the legacy shared
        one) and that the preview proxy can reach it there."""
        container.reload()  # first: a container deleted meanwhile must not get a new network
        network = self._ensure_sandbox_network(client, workspace)
        # Again: rebuilding the network for a changed egress policy disconnected the container,
        # and the inspect output read above still lists it as joined.
        container.reload()
        joined = set(((container.attrs or {}).get("NetworkSettings") or {}).get("Networks") or {})
        legacy = self.settings.docker_sandbox_network
        foreign = joined - {network.name, legacy}
        if foreign:
            raise ProvisioningError(
                f"The sandbox is attached to networks it must not be on: {sorted(foreign)}"
            )
        if network.name not in joined:
            network.connect(container)
        if legacy in joined and legacy != network.name:
            client.networks.get(legacy).disconnect(container, force=True)
        self._attach_proxies(network, proxies)

    def _remove_sandbox_network(self, client: Any, workspace: Workspace) -> None:
        try:
            network = client.networks.get(sandbox_network_name(workspace))
            network.reload()
        except NotFound:
            return
        for container_id in list((network.attrs or {}).get("Containers") or {}):
            try:
                network.disconnect(container_id, force=True)
            except NotFound:
                continue
        try:
            network.remove()
        except NotFound:
            return

    def _reconcile_networks_sync(self, client: Any) -> int:
        """Every sandbox of this installation on its own network with this stack's proxy
        attached; returns how many were checked. Idempotent, so safe to run concurrently."""
        proxies = self._proxies(client)
        if self._egress_via_proxy() and not proxies.egress:
            # Fails closed (the networks are internal), but every sandbox is offline.
            logger.warning(
                "docker_egress_proxy_missing", label=self.settings.docker_egress_proxy_label
            )
        checked = 0
        for container in client.containers.list(all=True, filters={"label": self._ours()}):
            workspace_id = (container.labels or {}).get(WORKSPACE_LABEL)
            if not workspace_id:
                continue
            try:
                self._isolate(client, container, Workspace(id=UUID(workspace_id)), proxies)
                checked += 1
            except (ValueError, APIError, NotFound, ProvisioningError) as error:
                logger.warning("docker_network_reconcile_failed", error_type=type(error).__name__)
        return checked

    async def reconcile_networks(self) -> int:
        await self._installation_id()
        client, _ = await self._clients()
        return await self._call(self._reconcile_networks_sync, client)

    @staticmethod
    def _assert_no_capabilities(container: Any) -> None:
        """The live process holds no capability at all, whatever the inspect output says."""
        result = container.exec_run(["/bin/cat", "/proc/1/status"])
        status = (result.output or b"").decode(errors="replace")
        effective = _parse_capability_mask(status, "CapEff")
        bounding = _parse_capability_mask(status, "CapBnd")
        if effective != 0 or bounding != 0:
            raise ProvisioningError(
                "Refusing a sandbox container that drifted from the security floor: "
                "['capabilities']"
            )

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
        name = container_name(workspace)
        try:
            existing = client.containers.get(name)
        except NotFound:
            existing = None
        if existing is not None:
            labels = existing.labels or {}
            installation = labels.get(INSTALLATION_LABEL)
            if (
                labels.get(MANAGED_LABEL) != "1"
                or labels.get(WORKSPACE_LABEL) != str(workspace.id)
                or (installation is not None and installation != self._require_installation())
            ):
                raise ProvisioningError(
                    "A container with this workspace's sandbox name exists but was not "
                    "created by Enigma for it; remove it and retry"
                )
            existing.reload()
            assert_safe_container(existing.attrs)
            self._isolate(client, existing, workspace, self._proxies(client))
            container = self._ensure_running(existing)
            self._assert_no_capabilities(container)
            self._ensure_workspace_dir(container, workspace)
            return container.id
        image = self.settings.docker_sandbox_image
        try:
            client.images.get(image)
        except ImageNotFound as error:
            raise ProvisioningError(
                "The sandbox image is not built; run sandbox/build-local-image.sh"
            ) from error
        network = self._ensure_sandbox_network(client, workspace)
        spec = self._container_spec(workspace, name)
        assert_safe_container_spec(spec)
        try:
            container = client.containers.run(**spec)
        except APIError as error:
            text = str(getattr(error, "explanation", "") or error).lower()
            if "storage_opt" in spec and ("storage-opt" in text or "storage opt" in text):
                raise ProvisioningError(
                    "The Docker storage driver does not support a per-sandbox disk quota; "
                    "unset DOCKER_SANDBOX_DISK_MB or use overlay2 on xfs with pquota"
                ) from error
            raise
        try:
            self._attach_proxies(network, self._proxies(client))
            self._ensure_workspace_dir(container, workspace)
        except Exception:
            container.remove(force=True)  # no half-made sandbox for the next retry to adopt
            raise
        return container.id

    async def provision(self, workspace: Workspace) -> str:
        await self._installation_id()
        client, _ = await self._clients()
        try:
            sandbox_id = await self._call(self._provision_sync, client, workspace)
            await self.activity.touch(workspace.id)
            return sandbox_id
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

    async def status_detail(self, workspace: Workspace) -> str | None:
        """Out-of-memory kills, read straight from the container (no exec wrapper, so a
        status poll neither wakes a stopped sandbox nor counts as activity)."""
        container = await self._container(workspace, start=False)
        limit = self.settings.docker_sandbox_memory_mb

        def read() -> str | None:
            container.reload()
            if ((container.attrs or {}).get("State") or {}).get("OOMKilled"):
                return (
                    f"The sandbox was stopped for exceeding its {limit} MB memory limit. "
                    "Raise DOCKER_SANDBOX_MEMORY_MB or use less memory."
                )
            if container.status != "running":
                return None
            # Absolute path: ~/.local/bin leads PATH and is the tenant's to write.
            result = container.exec_run(["/bin/cat", "/sys/fs/cgroup/memory.events"])
            killed = 0
            for line in (result.output or b"").decode(errors="replace").splitlines():
                key, _, value = line.partition(" ")
                if key == "oom_kill" and value.strip().isdigit():
                    killed = int(value)
            if not killed:
                return None
            return (
                f"{killed} process(es) in this sandbox were killed for exceeding its {limit} MB "
                "memory limit since it started. Raise DOCKER_SANDBOX_MEMORY_MB or use less "
                "memory."
            )

        return await self._call(read)

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
            pass
        await self._call(self._remove_sandbox_network, client, workspace)

    # --- upkeep (Epic 05): driven by services/sandbox_maintenance.py ------------------

    async def stop_if_running(self, workspace: Workspace, *, force: bool = False) -> bool:
        """Stop (keep) the sandbox; the next use starts it again. False -- and left alone --
        when it is not running, or (unless ``force``) when a command or an attached terminal
        is still alive in it, so a long, quiet build is not idle. The busy probe reads the
        sandbox's own /tmp, which its tenant controls: callers cap how long it may spare one.
        """
        container = await self._container(workspace, start=False)

        def stop() -> bool:
            container.reload()
            if container.status != "running":
                return False
            if not force:
                probe = container.exec_run(["/bin/sh", "-c", _BUSY_SCRIPT])
                if b"busy" in (probe.output or b""):
                    return False
            container.stop(timeout=10)
            return True

        return await self._call(stop)

    async def managed_sandboxes(self) -> list[tuple[str, str]]:
        """``(container id, workspace id label)`` of this installation's sandboxes -- and only
        those: another stack's, or anything Enigma did not create, is never touched."""
        await self._installation_id()
        client, _ = await self._clients()

        def listing() -> list[tuple[str, str]]:
            containers = client.containers.list(all=True, filters={"label": self._ours()})
            return [(c.id, (c.labels or {}).get(WORKSPACE_LABEL, "")) for c in containers]

        return await self._call(listing)

    async def legacy_sandboxes(self) -> list[tuple[str, str]]:
        """``(container id, workspace id label)`` of managed containers created before the
        installation label existed (labels are immutable, so they never gain it). Whose they
        are is for the caller to establish -- they may be another stack's."""
        client, _ = await self._clients()

        def listing() -> list[tuple[str, str]]:
            containers = client.containers.list(all=True, filters={"label": f"{MANAGED_LABEL}=1"})
            return [
                (c.id, (c.labels or {}).get(WORKSPACE_LABEL, ""))
                for c in containers
                if INSTALLATION_LABEL not in (c.labels or {})
            ]

        return await self._call(listing)

    async def isolate_sandbox(self, workspace: Workspace) -> None:
        """Move a sandbox the caller has established as this installation's onto its own
        network, with this stack's proxy attached."""
        await self._installation_id()
        client, _ = await self._clients()

        def isolate() -> None:
            container = client.containers.get(workspace.sandbox_id)
            self._isolate(client, container, workspace, self._proxies(client))

        await self._call(isolate)

    async def managed_network_workspaces(self) -> list[str]:
        await self._installation_id()
        client, _ = await self._clients()

        def listing() -> list[str]:
            networks = client.networks.list(filters={"label": self._ours()})
            labels = [((n.attrs or {}).get("Labels") or {}) for n in networks]
            return [found for found in (lab.get(WORKSPACE_LABEL) for lab in labels) if found]

        return await self._call(listing)

    async def remove_sandbox(self, workspace_id: UUID, container_id: str | None) -> None:
        """Remove an orphaned sandbox container (if any) and its network."""
        client, _ = await self._clients()

        def remove() -> None:
            if container_id:
                try:
                    client.containers.get(container_id).remove(force=True)
                except NotFound:
                    pass
            self._remove_sandbox_network(client, Workspace(id=workspace_id))

        await self._call(remove)

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
        environment = self._environment(env)
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
        await self.activity.touch(workspace.id)
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
        await self.activity.touch(workspace.id)
        _, exec_api = await self._clients()
        loop = asyncio.get_running_loop()
        abandoned = threading.Event()

        def deliver(callback: Callable[[str], Awaitable[None]], text: str) -> bool:
            return not text or deliver_threadsafe(loop, callback, text, abandoned)

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

    # --- files & terminal -------------------------------------------------------------

    async def fs(self, workspace: Workspace) -> DockerFileSystem:
        return DockerFileSystem(self, await self._container(workspace))

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
    ) -> DockerPtyHandle:
        pidfile = new_pidfile()
        argv = attach_argv(
            tmux_session_name(session_id),
            pidfile,
            cols=cols,
            rows=rows,
            cwd=self._resolve_cwd(workspace, cwd),
            environment=self._environment(env),
        )
        container = await self._container(workspace)
        client, exec_api = await self._clients()
        created = await self._call(
            client.api.exec_create,
            container.id,
            argv,
            stdin=True,
            tty=True,
            environment=[f"TERM={TERM}"],
            workdir=self._home(),
        )
        sock = await self._call(exec_api.exec_start, created["Id"], tty=True, socket=True)
        handle = DockerPtyHandle(self, container, created["Id"], pidfile, sock)

        async def on_output(data: bytes) -> None:
            # Terminal output (echoed keystrokes included) keeps the sandbox from idling.
            await self.activity.touch(workspace.id)
            await on_data(data)

        handle.start_reader(on_output)
        try:
            await handle.resize(PtySize(cols=cols, rows=rows))
        except BaseException:
            await handle.disconnect()
            raise
        return handle

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        container = await self._container(workspace, start=False)
        await self._call(container.reload)
        if container.status != "running":
            return  # a stopped container has no tmux server, so no session to kill
        # ``=name`` is an exact match; a missing session exits non-zero and is a no-op.
        await self._call(
            container.exec_run, ["tmux", "kill-session", "-t", f"={tmux_session_name(session_id)}"]
        )

    # --- preview ------------------------------------------------------------------------

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        """The container's address on the sandbox network, from that network's DNS.

        No daemon call: the preview proxy, which faces the internet, never holds the
        root-equivalent daemon socket, and it is the one process on the sandbox network.
        An address rather than the name because dev servers such as Vite 6+ refuse an
        unknown ``Host`` (``server.allowedHosts``) but accept any IP. A stopped container
        has no DNS entry, which surfaces as the proxy's self-retrying "Waking preview"
        page. No token: the network is the boundary and the proxy's HMAC capability the
        only gate.
        """
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no Docker sandbox")
        # Absolute name, so a resolver search domain can never expand it to an outside host.
        try:
            addresses = await self._resolve_host(f"{container_name(workspace)}.")
        except OSError as error:
            raise RuntimeUnavailableError("The sandbox is not running") from error
        address = next((a for a in addresses if _is_sandbox_address(a)), None)
        if address is None:
            # Never steer the proxy at a public host, its own loopback or a metadata endpoint.
            raise RuntimeUnavailableError("The sandbox is not reachable on the sandbox network")
        await self.activity.touch(workspace.id)
        return RuntimePreviewLink(url=f"http://{address}:{int(port)}", token="")

    async def _resolve_host(self, name: str) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(
            name, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        return [str(info[4][0]) for info in infos]

    async def expose_port(self, workspace: Workspace, port: int) -> None:
        """Relay a loopback-only listener to the preview proxy alone (see ``port_relay``)."""
        result = await self.exec(
            workspace,
            port_relay_command(port, self.settings.docker_preview_proxy_host),
            timeout=PORT_RELAY_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            logger.warning(
                "docker_port_relay_failed",
                port=int(port),
                exit_code=result.exit_code,
                output=result.stdout[-300:],
            )


__all__ = [
    "DockerRuntime",
    "assert_safe_container",
    "assert_safe_container_spec",
    "container_security_spec",
    "map_docker_error",
]
