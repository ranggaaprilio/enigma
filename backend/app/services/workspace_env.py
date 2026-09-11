"""Single decrypt point for `WorkspaceEnvVar` rows, plus `.env` materialisation into a sandbox.

Before this module, `services/commands.py`, `services/terminal.py`, and
`services/workspace_services.py` each carried their own copy of the "decrypt every row for a
workspace" loop. `resolve`/`load_all_values` are now the only place `WorkspaceSecretCipher` is
used to read a stored value back out.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import CustomCommand, Workspace, WorkspaceEnvVar, WorkspaceService
from app.models.workspace_service import WORKSPACE_SERVICE_ACTIVE_STATUSES
from app.services.daytona_runtime import DaytonaRuntime
from app.services.env_file import serialize_env
from app.services.run_paths import join_subpath
from app.services.workspaces import WorkspaceSecretCipher

logger = structlog.get_logger(__name__)


class EnvFileError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


async def resolve(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
    *,
    custom_command_id: UUID | None = None,
) -> dict[str, str]:
    """Workspace-wide rows first, then rows scoped to `custom_command_id` override by key."""
    cipher = WorkspaceSecretCipher(settings.workspace_env_encryption_key)
    values: dict[str, str] = {}

    base_result = await session.execute(
        select(WorkspaceEnvVar).where(
            WorkspaceEnvVar.workspace_id == workspace_id,
            WorkspaceEnvVar.custom_command_id.is_(None),
        )
    )
    for item in base_result.scalars().all():
        decrypted = cipher.decrypt(item.encrypted_value)
        if decrypted is not None:
            values[item.key] = decrypted

    if custom_command_id is not None:
        scoped_result = await session.execute(
            select(WorkspaceEnvVar).where(
                WorkspaceEnvVar.workspace_id == workspace_id,
                WorkspaceEnvVar.custom_command_id == custom_command_id,
            )
        )
        for item in scoped_result.scalars().all():
            decrypted = cipher.decrypt(item.encrypted_value)
            if decrypted is not None:
                values[item.key] = decrypted

    return values


async def load_all_values(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
) -> dict[str, str]:
    """Every row regardless of scope, for building the redaction tuple only -- never handed to
    a launched process. Keys may collide across scopes; when they do, any one value is fine
    since only VALUES (not the dict) are used downstream via `redactable_env_values`."""
    cipher = WorkspaceSecretCipher(settings.workspace_env_encryption_key)
    result = await session.execute(
        select(WorkspaceEnvVar).where(WorkspaceEnvVar.workspace_id == workspace_id)
    )
    values: dict[str, str] = {}
    for item in result.scalars().all():
        decrypted = cipher.decrypt(item.encrypted_value)
        if decrypted is not None:
            values[item.key] = decrypted
    return values


# Env keys whose values are credential material whatever their length.
_SECRET_KEY = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|API_?KEY|ACCESS_?KEY|AUTH|DSN|_URL|_URI)",
    re.IGNORECASE,
)
# A value this long is unlikely to be an ordinary word that legitimately shows up in output.
_REDACT_MIN_LENGTH = 16


def redactable_env_values(env: dict[str, str]) -> tuple[str, ...]:
    """The env values worth masking in run output.

    `redact_secrets` is a plain substring replace, so feeding it *every* value turned
    `DEBUG=1` into `[redacted]0 passed in [redacted].23s` and blanked every line mentioning
    `development`. A value is masked when its key looks like a credential (`DATABASE_PASSWORD`,
    `API_TOKEN`, a connection `_URL` that may embed one) or when it is long enough that it
    cannot be an incidental word. Short values under plain keys (`PORT=3000`, `NODE_ENV`) stay
    visible.
    """
    return tuple(
        value
        for key, value in env.items()
        if value and (_SECRET_KEY.search(key) or len(value) >= _REDACT_MIN_LENGTH)
    )


async def materialize_env_file(
    runtime: DaytonaRuntime,
    workspace: Workspace,
    *,
    checkout: str,
    env: dict[str, str],
    timeout: int,
) -> str | None:
    """Write `env` as a `.env` file at `workspace.env_file_path` (relative to `app_root`, below
    `checkout`), or do nothing at all when `env_file_path` is unset.

    Returns the sandbox-relative path written, or `None`. Raises `EnvFileError` when the parent
    directory does not already exist in the checkout -- this never creates directories inside
    the user's repository. Content is written exclusively through `sandbox.fs`, never through
    `runtime.exec`, so a secret value never appears in a command string or log.
    """
    if not workspace.env_file_path:
        return None

    path = join_subpath(checkout, workspace.app_root, workspace.env_file_path)
    parent = path.rsplit("/", 1)[0] if "/" in path else checkout

    sandbox = await runtime.get_sandbox(workspace)
    try:
        await sandbox.fs.get_file_info(parent)
    except Exception as error:
        raise EnvFileError(
            f"env_file_path parent directory '{parent}' does not exist in the checkout"
        ) from error

    await sandbox.fs.upload_file(serialize_env(env).encode("utf-8"), path)
    await sandbox.fs.set_file_permissions(path, mode="600")

    await _append_git_exclude(runtime, workspace, checkout=checkout, timeout=timeout)
    return path


async def _append_git_exclude(
    runtime: DaytonaRuntime,
    workspace: Workspace,
    *,
    checkout: str,
    timeout: int,
) -> None:
    """Best-effort: keep the materialised `.env` out of `git status`/diffs. A linked worktree's
    `.git` is a gitdir-pointer *file*, not a directory, so `<checkout>/.git/info/exclude` would
    not exist there -- `git rev-parse --git-path info/exclude` resolves the real (shared) path
    for both the base clone and any worktree. This command carries no secret values, only the
    file's path, so it is fine that it goes through `runtime.exec` and may appear in a log."""
    pattern = "/" + "/".join(part for part in (workspace.app_root, workspace.env_file_path) if part)
    quoted_pattern = shlex.quote(pattern)
    command = (
        'f="$(git rev-parse --git-path info/exclude)" && '
        'mkdir -p "$(dirname "$f")" && '
        f'{{ grep -qxF -- {quoted_pattern} "$f" 2>/dev/null || '
        f"printf '%s\\n' {quoted_pattern} >> \"$f\"; }}"
    )
    try:
        await runtime.exec(workspace, command, cwd=checkout, timeout=timeout)
    except Exception:
        logger.warning("env_file_exclude_append_failed", workspace_id=str(workspace.id))


async def prepare_launch_env(
    session: AsyncSession,
    settings: Settings,
    runtime: DaytonaRuntime,
    workspace: Workspace,
    *,
    checkout: str,
    custom_command_id: UUID | None,
    timeout: int,
) -> dict[str, str]:
    """`resolve()` the env for this launch, then `materialize_env_file()` if
    `workspace.env_file_path` is set."""
    env = await resolve(session, settings, workspace.id, custom_command_id=custom_command_id)
    await materialize_env_file(runtime, workspace, checkout=checkout, env=env, timeout=timeout)
    return env


@dataclass(frozen=True)
class AffectedService:
    """A running/starting `WorkspaceService` row whose env just changed under it -- surfaced so
    a caller can prompt the user to restart it."""

    service_id: UUID
    custom_command_id: UUID | None
    name: str
    status: str
    keys: list[str] = field(default_factory=list)


async def affected_running_services(
    session: AsyncSession,
    workspace_id: UUID,
    changed: Iterable[tuple[str, UUID | None]],
) -> list[AffectedService]:
    """Which active services are impacted by the just-written `(key, custom_command_id)` pairs.

    A workspace-wide change (`custom_command_id is None`) affects every active service *except*
    one that carries its own scoped override for that same key -- the override already shadows
    the workspace-wide value (see `resolve()`), so the change is a no-op for that service. A
    service-scoped change only affects the one active row launched from that `custom_command_id`,
    if any is currently running.
    """
    changed_list = list(changed)
    if not changed_list:
        return []

    rows_result = await session.execute(
        select(WorkspaceService, CustomCommand)
        .outerjoin(CustomCommand, WorkspaceService.custom_command_id == CustomCommand.id)
        .where(
            WorkspaceService.workspace_id == workspace_id,
            WorkspaceService.status.in_(WORKSPACE_SERVICE_ACTIVE_STATUSES),
        )
    )
    active_rows = rows_result.all()
    if not active_rows:
        return []

    overrides_result = await session.execute(
        select(WorkspaceEnvVar.custom_command_id, WorkspaceEnvVar.key).where(
            WorkspaceEnvVar.workspace_id == workspace_id,
            WorkspaceEnvVar.custom_command_id.is_not(None),
        )
    )
    overridden_keys: dict[UUID, set[str]] = {}
    for command_id, key in overrides_result.all():
        overridden_keys.setdefault(command_id, set()).add(key)

    affected: dict[UUID, AffectedService] = {}
    for key, changed_command_id in changed_list:
        for service_row, command in active_rows:
            if changed_command_id is None:
                if key in overridden_keys.get(service_row.custom_command_id, set()):
                    continue
            elif service_row.custom_command_id != changed_command_id:
                continue

            existing = affected.get(service_row.id)
            if existing is None:
                affected[service_row.id] = AffectedService(
                    service_id=service_row.id,
                    custom_command_id=service_row.custom_command_id,
                    name=command.name if command is not None else "Dev Server",
                    status=service_row.status,
                    keys=[key],
                )
            elif key not in existing.keys:
                existing.keys.append(key)

    return list(affected.values())


# A URL-shaped value: scheme://[user[:pass]@]host[:port][...]. Host may be bracketed IPv6.
_URL_HOST_PATTERN = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.\-]*://(?:[^@\s/]*@)?(?P<host>\[[0-9A-Fa-f:]+\]|[^\s/:?#]+)"
    r"(?::(?P<port>\d+))?"
)
# libpq-style `host=<value>` / `port=<value>` tokens (bare, space-terminated).
_LIBPQ_HOST_PATTERN = re.compile(r"(?<![\w-])host=(?P<host>[^\s]+)", re.IGNORECASE)
_LIBPQ_PORT_PATTERN = re.compile(r"(?<![\w-])port=(?P<port>\d+)", re.IGNORECASE)


def _is_loopback_host(host: str) -> bool:
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if addr.version == 4:
        return str(addr) == "0.0.0.0" or addr in ipaddress.ip_network("127.0.0.0/8")
    return addr == ipaddress.ip_address("::1")


def _parsed_allow_networks(settings: Settings) -> list:
    raw = settings.daytona_network_allow_list
    entries = (
        raw if isinstance(raw, list) else [item.strip() for item in raw.split(",") if item.strip()]
    )
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return networks


def egress_warnings(env: dict[str, str], settings: Settings) -> list[str]:
    """Flag env values that point egress at a host outside `DAYTONA_NETWORK_ALLOW_LIST` while
    `DAYTONA_NETWORK_BLOCK_ALL` is on. Never includes the value itself, credentials, path, or
    query -- only the env key, the host, and the port."""
    if not settings.daytona_network_block_all:
        return []

    allow_networks = _parsed_allow_networks(settings)
    warnings: list[str] = []
    seen: set[tuple[str, str, str | None]] = set()

    for key, value in env.items():
        if not value:
            continue
        found: list[tuple[str, str | None]] = []
        for match in _URL_HOST_PATTERN.finditer(value):
            found.append((match.group("host").strip("[]"), match.group("port")))
        host_match = _LIBPQ_HOST_PATTERN.search(value)
        if host_match:
            port_match = _LIBPQ_PORT_PATTERN.search(value)
            found.append(
                (host_match.group("host"), port_match.group("port") if port_match else None)
            )

        for host, port in found:
            if not host or _is_loopback_host(host):
                continue
            dedupe_key = (key, host, port)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            port_display = port or "?"
            try:
                addr = ipaddress.ip_address(host)
            except ValueError:
                warnings.append(
                    f"{key} points at {host}:{port_display}; egress is blocked by default -- "
                    "confirm its resolved address is in DAYTONA_NETWORK_ALLOW_LIST"
                )
                continue
            if not any(addr in network for network in allow_networks):
                warnings.append(
                    f"{key} points at {host}:{port_display} which is not in "
                    "DAYTONA_NETWORK_ALLOW_LIST while DAYTONA_NETWORK_BLOCK_ALL is on"
                )

    return warnings
