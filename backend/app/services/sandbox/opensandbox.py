"""OpenSandbox provider — the only module in the codebase that imports the ``opensandbox`` SDK.

Maps the neutral ``SandboxRuntime`` contract onto the OpenSandbox Python SDK: ``exec`` and
``stream_command`` are ``commands.run`` (optionally with streaming ``ExecutionHandlers``),
the file system wraps the SDK ``Filesystem`` service (whose method names differ from the
contract's — see ``OpenSandboxFileSystem``), previews come from ``get_endpoint``, and
interactive terminals talk to the execd ``/pty`` API directly (the SDK does not expose
it). ``find_files`` stays a fixed-string *content* search: the SDK's ``files.search`` is
glob-based, so it is implemented with ``grep -F`` run inside the sandbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from posixpath import basename
from posixpath import join as posix_join
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

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
from opensandbox.models.sandboxes import NetworkPolicy, NetworkRule, SandboxFilter

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.activity import SandboxActivityRecorder
from app.services.sandbox.base import (
    InsecureRuntimeError,
    ProvisioningError,
    PtyHandle,
    RuntimeCommandResult,
    RuntimePreviewLink,
    RuntimeUnavailableError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from app.services.sandbox.installation import INSTALLATION_LABEL, load_installation_id
from app.services.sandbox.opensandbox_pty import (
    EXECD_PORT,
    create_pty_session,
    delete_pty_session,
    open_pty_websocket,
    pty_base_url,
    pty_session_status,
)

logger = structlog.get_logger(__name__)

# --- the create request (Plan 7a) ------------------------------------------------------------
# On opensandbox-server 1.2.1 every sandbox shares one bridge and execd accepts any caller that
# lacks a token, so a sibling sandbox could exec in another tenant's (verified live, plan Task
# 1). Each sandbox therefore gets its own execd token (sent on every SDK, PTY and preview call)
# and an egress policy that never reaches a private address (enforced by a `dns+nft` server).
EXECD_TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
EXECD_TOKEN_ENV = "EXECD_ACCESS_TOKEN"
WORKSPACE_LABEL = "enigma.workspace"
PRIVATE_NETWORKS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",  # carrier-grade NAT
    "169.254.0.0/16",  # link-local, cloud metadata
    "fc00::/7",
    "fe80::/10",
    "::ffff:0:0/96",  # IPv4-mapped IPv6: would otherwise reopen every range above
)
# Everything else a create request could carry (extensions, volumes, platform, entrypoint,
# secure access, credential proxy, ...) can widen the sandbox profile or mount host state.
SAFE_CREATE_KEYS = frozenset(
    {"image", "timeout", "ready_timeout", "env", "metadata", "resource", "network_policy"}
)
CREATE_READY_TIMEOUT = timedelta(seconds=120)
# Upkeep (Plan 7a Task 5). A sandbox is busy while Enigma runs something in it: a running
# exec renews its `sandbox_activity` row every heartbeat, terminal output renews it as it
# flows, so the idle pass (another process, maybe) never pauses a sandbox in use. A sandbox
# this process saw running stays trusted awake for a minute (an idle window is far longer).
ACTIVITY_HEARTBEAT_SECONDS = 60.0
AWAKE_CACHE_SECONDS = 60.0
LIST_PAGE_SIZE = 100


# The security floor, read inside the sandbox (shell builtins and an absolute binary only: the
# tenant owns the front of PATH). opensandbox-server applies the container profile from its
# own sandbox.toml, so a server without drop_capabilities=["ALL"] is caught here.
FLOOR_PROBE = (
    "while read -r key value _; do case $key in CapEff:|NoNewPrivs:) "
    'echo "$key $value";; esac; done < /proc/self/status; /usr/bin/id -u'
)
FLOOR_PROBE_TIMEOUT = timedelta(seconds=30)


def floor_violations(output: str) -> list[str]:
    """What the probe output says is below the floor (empty when it is all in place)."""
    fields: dict[str, str] = {}
    uid = None
    for line in output.splitlines():
        key, _, value = line.strip().partition(" ")
        if key in ("CapEff:", "NoNewPrivs:"):
            fields[key] = value.strip()
        elif key.isdigit():
            uid = key
    violations = []
    if fields.get("CapEff:", "").strip("0") != "" or "CapEff:" not in fields:
        violations.append("CapEff is not 0 (capabilities not all dropped)")
    if fields.get("NoNewPrivs:") != "1":
        violations.append("NoNewPrivs is not 1")
    if uid is None or uid == "0":
        violations.append("the sandbox user is root")
    return violations


def execd_token(settings: Settings, workspace_id: UUID | str) -> str:
    """The workspace's execd access token: derived, never stored.

    Keyed by OPENSANDBOX_EXECD_TOKEN_KEY (required outside ``local``; there it falls back to
    AUTH_SECRET_KEY so a dev setup needs nothing extra).
    """
    key = settings.opensandbox_execd_token_key or settings.auth_secret_key
    message = f"opensandbox-execd:{workspace_id}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def execd_headers(settings: Settings, workspace: Workspace) -> dict[str, str]:
    return {EXECD_TOKEN_HEADER: execd_token(settings, workspace.id)}


def _network(target: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(target, strict=False)
    except ValueError:
        return None


def _is_private(target: str) -> bool:
    network = _network(target)
    if network is None:
        return False
    return any(
        network.version == private.version and network.overlaps(private)
        for private in map(ipaddress.ip_network, PRIVATE_NETWORKS)
    )


def network_policy(settings: Settings) -> NetworkPolicy:
    """SANDBOX_NETWORK_* as an egress policy; private ranges are denied whatever they say."""
    deny = [NetworkRule(action="deny", target=network) for network in PRIVATE_NETWORKS]
    if settings.sandbox_network_block_all:
        return NetworkPolicy(defaultAction="deny", egress=deny)
    allow_list = [entry for entry in settings.sandbox_network_allow_list if entry]
    if allow_list:
        # SANDBOX_NETWORK_ALLOW_LIST holds CIDRs on every provider; a hostname here could
        # resolve to a private address, so it is dropped rather than trusted.
        dropped = [entry for entry in allow_list if _network(entry) is None or _is_private(entry)]
        if dropped:
            logger.warning("opensandbox_allow_list_entries_ignored", entries=dropped)
        allow = [
            NetworkRule(action="allow", target=entry)
            for entry in allow_list
            if entry not in dropped
        ]
        return NetworkPolicy(defaultAction="deny", egress=deny + allow)
    return NetworkPolicy(defaultAction="allow", egress=deny)


def build_create_kwargs(
    settings: Settings, workspace: Workspace, installation: str
) -> dict[str, Any]:
    image = settings.opensandbox_sandbox_image
    if settings.opensandbox_image_digest_required and "@sha256:" not in image:
        raise ProvisioningError(
            "OPENSANDBOX_SANDBOX_IMAGE must be pinned by digest (name@sha256:...)"
        )
    resource = {
        "cpu": settings.opensandbox_sandbox_cpu,
        "memory": settings.opensandbox_sandbox_memory,
    }
    if settings.opensandbox_sandbox_disk:
        resource["disk"] = settings.opensandbox_sandbox_disk
    return {
        "image": image,
        "timeout": None,  # Enigma owns the lifecycle: destroy() on delete, upkeep pauses
        "ready_timeout": CREATE_READY_TIMEOUT,
        "env": {EXECD_TOKEN_ENV: execd_token(settings, workspace.id)},
        "metadata": {INSTALLATION_LABEL: installation, WORKSPACE_LABEL: str(workspace.id)},
        "resource": resource,
        "network_policy": network_policy(settings),
    }


def assert_safe_create_kwargs(kwargs: Mapping[str, Any], settings: Settings) -> None:
    """Refuse a create request that could widen the sandbox profile (defence in depth)."""
    extra = set(kwargs) - SAFE_CREATE_KEYS
    if extra:
        raise ProvisioningError(f"refusing unsafe OpenSandbox create fields: {sorted(extra)}")
    if kwargs.get("image") != settings.opensandbox_sandbox_image:
        raise ProvisioningError(
            "refusing an OpenSandbox image other than OPENSANDBOX_SANDBOX_IMAGE"
        )
    if set(kwargs.get("env") or {}) != {EXECD_TOKEN_ENV}:
        raise ProvisioningError("refusing OpenSandbox create env beyond the execd token")
    if not isinstance(kwargs.get("network_policy"), NetworkPolicy):
        raise ProvisioningError("refusing an OpenSandbox sandbox without a network policy")

# Upper bound on ``find_files`` output kept in backend memory, and on how long the search may
# run. The SDK accumulates
# stdout into ``execution.logs`` by default, so a large tree would hold the whole match set in
# memory; ``find_files`` streams with ``skip_accumulation`` and caps what it keeps.
MAX_SEARCH_OUTPUT_BYTES = 8 * 1024 * 1024
SEARCH_TIMEOUT_SECONDS = 60

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


def _line_of(message: Any) -> str:
    """One execd output message back as a line, newline included.

    execd (opensandbox-server 1.2.1) sends stdout/stderr one line per message with the newline
    stripped, and an empty line as ``"\n"``; partial writes are buffered until their newline.
    """
    text = message.text
    return text if text == "\n" else f"{text}\n"


def _joined_lines(messages: Sequence[Any]) -> str:
    """Accumulated execd output as text (the final newline cannot be told apart, so none)."""
    return "".join(_line_of(message) for message in messages).removesuffix("\n")


def _execd_mode(mode: str) -> int:
    """``"755"`` -> ``755``: execd reads ``mode`` as octal *digits* (``ParseUint(s, 8, 32)``), so
    the SDK must send the digits as written, not their value (``int("755", 8)`` is 493, which
    execd rejects as invalid octal). ``int(mode, 8)`` still validates the input is octal."""
    int(mode, 8)
    return int(mode)


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

    def __init__(self, sandbox: Sandbox, home: str) -> None:
        self._sandbox = sandbox
        self._home = home

    def _abs(self, path: str) -> str:
        """Relative paths resolve against the sandbox home, as Daytona's do (execd would
        resolve them against ``/``)."""
        return path if path.startswith("/") else posix_join(self._home, path)

    async def list_files(self, path: str) -> Sequence[Any]:
        try:
            entries = await self._sandbox.files.list_directory(
                DirectoryListEntry(path=self._abs(path))
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return [_translate_entry(entry) for entry in entries]

    async def get_file_info(self, path: str) -> Any:
        absolute = self._abs(path)
        try:
            infos = await self._sandbox.files.get_file_info([absolute])
        except Exception as error:
            raise map_opensandbox_error(error) from error
        # The SDK keys its answer by the path it was asked about: the absolute one.
        info = infos.get(absolute)
        if info is None:
            raise FileNotFoundError(path)
        return _translate_entry(info)

    async def download_file(self, path: str) -> bytes:
        try:
            return await self._sandbox.files.read_bytes(self._abs(path))
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def upload_file(self, content: bytes, path: str) -> None:
        try:
            await self._sandbox.files.write_files([WriteEntry(path=self._abs(path), data=content)])
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def create_folder(self, path: str, mode: str) -> None:
        try:
            await self._sandbox.files.create_directories(
                [WriteEntry(path=self._abs(path), mode=_execd_mode(mode))]
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def move_files(self, source: str, destination: str) -> None:
        try:
            await self._sandbox.files.move_files(
                [MoveEntry(src=self._abs(source), dest=self._abs(destination))]
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def delete_file(self, path: str, recursive: bool = False) -> None:
        try:
            if recursive:
                await self._sandbox.files.delete_directories([self._abs(path)])
            else:
                await self._sandbox.files.delete_files([self._abs(path)])
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def find_files(self, path: str, pattern: str) -> Sequence[Any]:
        # A fixed-string content search, as Daytona's is — not the SDK's glob ``files.search``.
        # Bounded: a hard timeout, and stdout is streamed with
        # ``skip_accumulation`` so the SDK never buffers the whole match set in memory; we keep
        # only the first ``MAX_SEARCH_OUTPUT_BYTES`` and drop the record the cap cut.
        if "\n" in pattern:
            return []
        target = self._abs(path)
        argv = ["grep", "-r", "-n", "-H", "-I", "-F", "--null", "-e", pattern, "--", target]
        chunks: list[str] = []
        kept = 0

        async def on_stdout(message: Any) -> None:
            nonlocal kept
            if kept >= MAX_SEARCH_OUTPUT_BYTES:
                return
            text = _line_of(message)
            remaining = MAX_SEARCH_OUTPUT_BYTES - kept
            if len(text) > remaining:
                text = text[:remaining]
            chunks.append(text)
            kept += len(text)

        opts = RunCommandOpts(timeout=timedelta(seconds=SEARCH_TIMEOUT_SECONDS))
        handlers = ExecutionHandlers(on_stdout=on_stdout, skip_accumulation=True)
        try:
            execution = await self._sandbox.commands.run(argv, opts=opts, handlers=handlers)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        exit_code = execution.exit_code if execution.exit_code is not None else 1
        if exit_code not in (0, 1):  # grep: 1 = no match, 2 = error
            raise SandboxRuntimeError("OpenSandbox file search failed")
        output = "".join(chunks)
        if len(output) >= MAX_SEARCH_OUTPUT_BYTES:
            output = output[: output.rfind("\n") + 1]  # drop the record the cap cut
        return parse_grep_matches(output)

    async def set_file_permissions(self, path: str, *, mode: str) -> None:
        try:
            await self._sandbox.files.set_permissions(
                [SetPermissionEntry(path=self._abs(path), mode=_execd_mode(mode))]
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error


class OpenSandboxRuntime:
    provider = "opensandbox"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._config: ConnectionConfig | None = None
        self._manager: SandboxManager | None = None
        # Enigma terminal session id -> execd-assigned PTY session id. execd generates its
        # own id on POST /pty; this in-process mapping lets a reconnect resolve the same
        # execd session (and is discarded alongside the sandbox on restart).
        self._pty_sessions: dict[str, str] = {}
        self._installation: str | None = None  # this database's id, loaded on first create
        # Sandboxes whose security floor this process has seen in place (checked once each).
        self._floor_checked: set[str] = set()
        self.activity = SandboxActivityRecorder()
        self._awake_until: dict[str, float] = {}  # sandbox id -> monotonic deadline

    def _home(self) -> str:
        return self.settings.sandbox_home.rstrip("/") or "/"

    def _resolve_cwd(self, workspace: Workspace, cwd: str | None) -> str:
        """``cwd`` (default: the workspace), relative to the sandbox home as on Daytona."""
        path = cwd or workspace.workspace_path or ""
        return path if path.startswith("/") else posix_join(self._home(), path)

    @property
    def configured(self) -> bool:
        return bool(self.settings.opensandbox_api_url)

    def _connection_config(self) -> ConnectionConfig:
        if self._config is None:
            self._config = _build_connection_config(self.settings)
        return self._config

    # The SDK seams (replaced by tests): connect to an existing sandbox, create one, and the
    # management-API client for ``health``/``status``.
    async def _connect(self, sandbox_id: str, *, headers: dict[str, str]) -> Sandbox:
        # The sandbox's execd token rides on every data-plane request (the lifecycle server
        # ignores it); a sandbox created before tokens has none and accepts it all the same.
        base = self._connection_config()
        config = base.model_copy(update={"headers": {**base.headers, **headers}})
        return await Sandbox.connect(sandbox_id, connection_config=config, skip_health_check=True)

    async def _create(self, kwargs: dict[str, Any]) -> Sandbox:
        token = kwargs["env"][EXECD_TOKEN_ENV]
        base = self._connection_config()
        config = base.model_copy(update={"headers": {**base.headers, EXECD_TOKEN_HEADER: token}})
        return await Sandbox.create(**kwargs, connection_config=config)

    async def _installation_id(self) -> str:
        if self._installation is None:
            self._installation = await load_installation_id(self.settings)
        return self._installation

    async def _get_manager(self) -> SandboxManager:
        if self._manager is None:
            self._manager = await SandboxManager.create(connection_config=self._connection_config())
        return self._manager

    # P des seams (replaced by tests): each talks to the execd ``/pty`` API directly,
    # because the SDK does not expose those routes.
    async def _pty_create_session(
        self, base_url: str, cwd: str | None, *, headers: dict[str, str]
    ) -> str:
        return await create_pty_session(base_url, cwd=cwd, headers=headers)

    async def _pty_session_status(
        self, base_url: str, server_session_id: str, *, headers: dict[str, str]
    ) -> tuple[bool, int]:
        return await pty_session_status(base_url, server_session_id, headers=headers)

    async def _pty_connect(
        self,
        base_url: str,
        server_session_id: str,
        on_data: Callable[[bytes], Awaitable[None]],
        *,
        offset: int,
        takeover: bool,
        cols: int,
        rows: int,
        headers: dict[str, str],
    ) -> PtyHandle:
        return await open_pty_websocket(
            base_url,
            server_session_id,
            on_data,
            offset=offset,
            takeover=takeover,
            cols=cols,
            rows=rows,
            headers=headers,
        )

    async def _pty_delete_session(
        self, base_url: str, server_session_id: str, *, headers: dict[str, str]
    ) -> None:
        await delete_pty_session(base_url, server_session_id, headers=headers)

    async def close(self) -> None:
        manager, self._manager = self._manager, None
        if manager is not None:
            await manager.close()
        config, self._config = self._config, None
        if config is not None:
            await config.close_transport_if_owned()

    async def _auth_probe_status(self) -> int:
        """The status an unauthenticated lifecycle-API request gets (seam for tests)."""
        import httpx

        url = (self.settings.opensandbox_api_url or "").rstrip("/")
        if "://" not in url:
            url = f"http://{url}"
        async with httpx.AsyncClient(timeout=3.0) as client:
            return (await client.get(f"{url}/v1/sandboxes")).status_code

    async def health(self) -> str:
        # Fail closed: a server that does not authenticate lets anyone who can reach it
        # create, read and destroy every tenant's sandbox.
        if self.settings.opensandbox_allow_insecure:
            logger.warning("opensandbox_insecure_server_allowed")
        elif not self.settings.opensandbox_api_key:
            raise InsecureRuntimeError("no OPENSANDBOX_API_KEY is configured")
        else:
            try:
                status = await self._auth_probe_status()
            except Exception as error:
                raise map_opensandbox_error(error) from error
            if status != 401:
                raise InsecureRuntimeError("the server answers without an API key")
        try:
            await (await self._get_manager()).list_sandbox_infos(SandboxFilter())
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return "ok"

    async def provision(self, workspace: Workspace) -> str:
        kwargs = build_create_kwargs(self.settings, workspace, await self._installation_id())
        assert_safe_create_kwargs(kwargs, self.settings)
        try:
            sandbox = await self._create(kwargs)
        except Exception as error:
            raise ProvisioningError(str(map_opensandbox_error(error))) from error
        violations = await self._floor_violations(sandbox)
        if violations:
            try:
                await sandbox.destroy()
            except Exception:
                logger.warning(
                    "opensandbox_unsafe_sandbox_cleanup_failed", sandbox_id=str(sandbox.id)
                )
            raise ProvisioningError(
                "OpenSandbox sandbox failed its security check: " + "; ".join(violations)
            )
        self._floor_checked.add(str(sandbox.id))
        return str(sandbox.id)

    async def _state(self, sandbox_id: str) -> str:
        try:
            info = await (await self._get_manager()).get_sandbox_info(sandbox_id)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return str(getattr(getattr(info, "status", None), "state", "") or "").lower()

    async def _wake(self, sandbox_id: str) -> None:
        """Resume a sandbox the idle pass paused; the next exec, terminal or preview wakes it."""
        if time.monotonic() < self._awake_until.get(sandbox_id, 0.0):
            return
        if await self._state(sandbox_id) == "paused":
            try:
                await (await self._get_manager()).resume_sandbox(sandbox_id)
            except Exception as error:
                raise map_opensandbox_error(error) from error
            logger.info("opensandbox_sandbox_resumed", sandbox_id=sandbox_id)
        self._awake_until[sandbox_id] = time.monotonic() + AWAKE_CACHE_SECONDS

    @contextlib.asynccontextmanager
    async def _busy(self, workspace: Workspace):
        """Keep the sandbox's activity fresh for as long as Enigma runs something in it."""
        await self.activity.touch(workspace.id)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(ACTIVITY_HEARTBEAT_SECONDS)
                await self.activity.touch(workspace.id)

        beating = asyncio.ensure_future(heartbeat())
        try:
            yield
        finally:
            beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beating

    # --- upkeep (services/sandbox_maintenance.py) ------------------------------------------

    async def managed_sandboxes(self) -> list[tuple[str, str]]:
        """``(sandbox id, workspace id label)`` for this installation's sandboxes only.

        Always filtered by this database's installation id: an unscoped listing would let the
        orphan sweep destroy another installation's sandboxes on a shared server.
        """
        installation = await self._installation_id()
        manager = await self._get_manager()
        found: list[tuple[str, str]] = []
        page = 1
        while True:
            try:
                result = await manager.list_sandbox_infos(
                    SandboxFilter(
                        metadata={INSTALLATION_LABEL: installation},
                        page=page,
                        page_size=LIST_PAGE_SIZE,
                    )
                )
            except Exception as error:
                raise map_opensandbox_error(error) from error
            for info in result.sandbox_infos:
                metadata = getattr(info, "metadata", None) or {}
                if metadata.get(INSTALLATION_LABEL) == installation:  # never trust the filter
                    found.append((str(info.id), metadata.get(WORKSPACE_LABEL, "")))
            pagination = getattr(result, "pagination", None)
            if not getattr(pagination, "has_next_page", False):
                return found
            page += 1

    async def pause_if_running(self, workspace: Workspace) -> bool:
        if not workspace.sandbox_id or await self._state(workspace.sandbox_id) != "running":
            return False
        try:
            await (await self._get_manager()).pause_sandbox(workspace.sandbox_id)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        self._awake_until.pop(workspace.sandbox_id, None)
        return True

    async def remove_sandbox(self, sandbox_id: str) -> None:
        try:
            await (await self._get_manager()).kill_sandbox(sandbox_id)
        except Exception as error:
            mapped = map_opensandbox_error(error)
            if not isinstance(mapped, SandboxNotFoundError):
                raise mapped from error
        self._awake_until.pop(sandbox_id, None)
        self._floor_checked.discard(sandbox_id)

    async def _floor_violations(self, sandbox: Sandbox) -> list[str]:
        try:
            execution = await sandbox.commands.run(
                FLOOR_PROBE, opts=RunCommandOpts(timeout=FLOOR_PROBE_TIMEOUT)
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return floor_violations(_joined_lines(execution.logs.stdout))

    async def _get_sandbox(self, workspace: Workspace, *, check_floor: bool = True) -> Sandbox:
        if not workspace.sandbox_id:
            raise SandboxNotFoundError("Workspace has no OpenSandbox sandbox")
        try:
            sandbox = await self._connect(
                workspace.sandbox_id, headers=execd_headers(self.settings, workspace)
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error
        if check_floor:
            await self._wake(workspace.sandbox_id)
        if check_floor and workspace.sandbox_id not in self._floor_checked:
            # Adopted (created by another process or before this check existed): refuse to
            # run anything in it below the floor, but keep the user's files.
            violations = await self._floor_violations(sandbox)
            if violations:
                raise SandboxRuntimeError(
                    "OpenSandbox sandbox failed its security check (recreate the workspace): "
                    + "; ".join(violations)
                )
            self._floor_checked.add(workspace.sandbox_id)
        return sandbox

    async def fs(self, workspace: Workspace) -> OpenSandboxFileSystem:
        return OpenSandboxFileSystem(await self._get_sandbox(workspace), self._home())

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
        # Connecting resumes a sandbox the idle pass paused and verifies its floor.
        await self._get_sandbox(workspace)

    async def delete(self, workspace: Workspace) -> None:
        if not workspace.sandbox_id:
            return
        try:
            # No floor check on teardown: a sandbox that fails it must stay removable.
            sandbox = await self._get_sandbox(workspace, check_floor=False)
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
            async with self._busy(workspace):
                execution = await sandbox.commands.run(
                    command, opts=_command_opts(self._resolve_cwd(workspace, cwd), env, timeout)
                )
        except Exception as error:
            raise map_opensandbox_error(error) from error
        return RuntimeCommandResult(
            exit_code=execution.exit_code if execution.exit_code is not None else 1,
            stdout=_joined_lines(execution.logs.stdout),
            stderr=_joined_lines(execution.logs.stderr),
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
            await on_stdout(_line_of(message))

        async def on_stderr_msg(message: Any) -> None:
            await on_stderr(_line_of(message))

        handlers = ExecutionHandlers(on_stdout=on_stdout_msg, on_stderr=on_stderr_msg)
        try:
            async with self._busy(workspace):
                execution = await sandbox.commands.run(
                    command,
                    opts=_command_opts(self._resolve_cwd(workspace, cwd), env, timeout),
                    handlers=handlers,
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
        sandbox = await self._get_sandbox(workspace)
        try:
            endpoint = await sandbox.get_endpoint(EXECD_PORT)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        base_url = pty_base_url(str(getattr(endpoint, "endpoint", "") or ""))
        headers = execd_headers(self.settings, workspace)

        # Environment variables ride on the sandbox env file (execd's ``set_env``), so a
        # fresh session sees them; they persist for later sessions of the same sandbox.
        if env:
            try:
                for key, value in env.items():
                    await sandbox.commands.set_env(key, value)
            except Exception as error:
                raise map_opensandbox_error(error) from error

        # Map Enigma's session id to the execd-assigned session id. execd generates its own
        # id on POST /pty, so we keep the pair in-process for reconnect (a terminal survives a
        # page reload by reconnecting to the same execd session).
        server_session_id = self._pty_sessions.get(session_id)
        if server_session_id is None:
            try:
                server_session_id = await self._pty_create_session(base_url, cwd, headers=headers)
            except Exception as error:
                raise map_opensandbox_error(error) from error
            self._pty_sessions[session_id] = server_session_id

        try:
            exists, offset = await self._pty_session_status(
                base_url, server_session_id, headers=headers
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error
        if not exists:
            # The sandbox restarted and its execd sessions are gone: recreate.
            try:
                server_session_id = await self._pty_create_session(base_url, cwd, headers=headers)
            except Exception as error:
                raise map_opensandbox_error(error) from error
            self._pty_sessions[session_id] = server_session_id
            offset = 0

        # A terminal is in use while output flows (the recorder debounces the writes).
        await self.activity.touch(workspace.id)

        async def on_output(data: bytes) -> None:
            await self.activity.touch(workspace.id)
            await on_data(data)

        try:
            return await self._pty_connect(
                base_url,
                server_session_id,
                on_output,
                offset=offset,
                takeover=True,
                cols=cols,
                rows=rows,
                headers=headers,
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        server_session_id = self._pty_sessions.pop(session_id, None)
        if server_session_id is None:
            return
        sandbox = await self._get_sandbox(workspace, check_floor=False)  # teardown
        try:
            endpoint = await sandbox.get_endpoint(EXECD_PORT)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        base_url = pty_base_url(str(getattr(endpoint, "endpoint", "") or ""))
        try:
            await self._pty_delete_session(
                base_url, server_session_id, headers=execd_headers(self.settings, workspace)
            )
        except Exception as error:
            raise map_opensandbox_error(error) from error

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        sandbox = await self._get_sandbox(workspace)
        try:
            endpoint = await sandbox.get_endpoint(port)
        except Exception as error:
            raise map_opensandbox_error(error) from error
        url = str(getattr(endpoint, "endpoint", "") or "")
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        # execd's port proxy sits behind its token: the preview proxy and the
        # verification observation send these headers with every request.
        headers = execd_headers(self.settings, workspace)
        return RuntimePreviewLink(url=url, token="", headers=headers)

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
    "MAX_SEARCH_OUTPUT_BYTES",
    "SEARCH_TIMEOUT_SECONDS",
]
