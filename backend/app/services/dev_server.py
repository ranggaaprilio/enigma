"""Compatibility facade over `/workspaces/{id}/dev/*`.

Epic 11 replaces the single-slot dev server with N tracked `workspace_services` rows
(`services/workspace_services.py`). `run`/`status`/`ensure_running`/`stop` here all delegate to
that manager's *primary* row — the workspace's enabled `role='dev'` registry command if one
exists, else the built-in mise service — so `PreviewPanel`'s existing auto-resume poller and
every pre-Epic-11 API consumer keep working unchanged while the new multi-service surface
(`/workspaces/{id}/services/*`) is adopted.

`setup()` (prepare the environment without starting it) stays self-contained: it only ever
touches `workspace.dev_worktree_id`, never `desired`/`workspace_services`, and Epic 11 does not
add a per-service equivalent — starting any other service installs inline (see
`WorkspaceServiceManager._launch`).
"""

from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import CustomCommand, Workspace, WorkspaceService, WorkspaceWorktree
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.preview import COMMON_PREVIEW_PORTS
from app.services.run_paths import join_subpath, validate_relative_subpath
from app.services.sandbox_probe import port_is_open
from app.services.service_scripts import (
    build_mise_setup_script,
    build_registry_script,
    builtin_paths,
)
from app.services.workspace_services import (
    WorkspaceServiceManager,
    WorkspaceServiceNotClonedError,
    WorkspaceServiceRuntimeError,
    WorkspaceServiceWorkspacePathError,
)
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

logger = structlog.get_logger(__name__)

# Config filenames mise looks for, in precedence order we care about for detection.
MISE_CONFIG_FILES = (
    "mise.toml",
    ".mise.toml",
    "mise/config.toml",
    ".config/mise/config.toml",
)


class DevServerWorkspacePathError(Exception):
    pass


class DevServerNotClonedError(Exception):
    pass


class DevServerRuntimeError(Exception):
    pass


@dataclass(frozen=True)
class DevServerState:
    status: str
    port: int | None = None
    message: str | None = None
    # The worktree the current dev server belongs to (None = base clone) and its branch.
    worktree_id: UUID | None = None
    branch: str | None = None


class DevServerService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: DaytonaRuntime | None = None,
        common_ports: tuple[int, ...] = COMMON_PREVIEW_PORTS,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_daytona_runtime()
        self.common_ports = common_ports
        self._manager = WorkspaceServiceManager(session, settings, self.runtime, common_ports)

    # ------------------------------------------------------------------
    # setup(): prepare an environment without starting it — self-contained.
    # ------------------------------------------------------------------

    async def setup(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState:
        """Prepare a worktree: install the mise toolchain + project dependencies,
        then stop at `ready`. This does NOT run the app and does NOT mark it as
        desired — actually running the dev task is an explicit user action (`run`)."""
        workspace = await self._get_workspace(workspace_id, user_id)
        cwd, branch, resolved_id = await self._resolve_worktree(workspace, worktree_id)

        # Skip a re-install only when the prepared/running environment already belongs
        # to the requested worktree; a different worktree needs its own dependencies
        # installed into its own working directory.
        current = await self._read_status(workspace)
        if workspace.dev_worktree_id == resolved_id and current in {
            "installing",
            "ready",
            "running",
        }:
            state = await self._resolve_state(workspace, current)
            return await self._with_worktree(workspace, state)

        return await self._launch_setup(workspace, user_id, cwd, resolved_id, branch)

    # ------------------------------------------------------------------
    # run / status / ensure_running / stop: delegate to the primary service row.
    # ------------------------------------------------------------------

    async def run(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState:
        row = await self._call(self._manager.run_primary, workspace_id, user_id, worktree_id)
        return await self._state_from_row(workspace_id, user_id, row)

    async def status(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        row = await self._call(self._manager.status_primary, workspace_id, user_id)
        return await self._state_from_row(workspace_id, user_id, row)

    async def ensure_running(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        row = await self._call(self._manager.ensure_running_primary, workspace_id, user_id)
        return await self._state_from_row(workspace_id, user_id, row)

    async def stop(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        row = await self._call(self._manager.stop_primary, workspace_id, user_id)
        return await self._state_from_row(workspace_id, user_id, row)

    async def _call(self, method, *args):
        try:
            return await method(*args)
        except WorkspaceServiceWorkspacePathError as error:
            raise DevServerWorkspacePathError from error
        except WorkspaceServiceNotClonedError as error:
            raise DevServerNotClonedError from error
        except WorkspaceServiceRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error

    async def _state_from_row(
        self, workspace_id: UUID, user_id: UUID, row: WorkspaceService
    ) -> DevServerState:
        workspace = await self._get_workspace(workspace_id, user_id)
        status, message = await self._dev_server_status(workspace, row)
        port = row.port if row.status == "running" else None
        # `worktree_id=None` legitimately means "the base clone", so the branch is resolved
        # either way — the response always names the branch being served.
        branch = await self._branch_for(workspace, row.worktree_id)
        return DevServerState(
            status=status,
            port=port,
            message=message,
            worktree_id=row.worktree_id,
            branch=branch,
        )

    async def _dev_server_status(
        self, workspace: Workspace, row: WorkspaceService
    ) -> tuple[str, str | None]:
        """Map a `WorkspaceService` row back to the pre-Epic-11 `DevServerStatus` vocabulary.

        `installing`/`starting`/`running`/`error`/`exited` are carried on the row itself, but
        `idle`/`ready`/`stopped` all collapse to the same DB status (`stopped`) — Epic 11 has no
        use for that pre-launch distinction, so it is recovered from `_raw_status`, the raw
        sandbox status token the manager's probe stashes on the row (not a mapped column; the
        same non-persisted-attribute pattern `PreviewSession.branch` already uses).
        """
        if row.status == "error":
            if row.last_error == "no-config":
                return "no-config", "No mise.toml found in the repository."
            if row.last_error == "no-dev-task":
                return "no-dev-task", "mise.toml has no 'dev' task to run."
            tail = await self._read_log_tail(workspace)
            message = "Environment setup failed."
            if tail:
                message = f"Environment setup failed:\n{tail}"
            return "error", message
        if row.status == "exited":
            message = (
                f"Process exited (code {row.exit_code})." if row.exit_code is not None else None
            )
            return "stopped", message
        if row.status in ("installing", "starting", "running"):
            return row.status, None
        raw = getattr(row, "_raw_status", None)
        if raw == "ready":
            return "ready", "Environment ready. Click Run App to start."
        if raw == "stopped":
            return "stopped", None
        return "idle", None

    async def _branch_for(self, workspace: Workspace, worktree_id: UUID | None) -> str:
        if worktree_id is None:
            return workspace.default_branch
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            return workspace.default_branch
        return row.branch

    # ------------------------------------------------------------------
    # setup()'s own helpers (unchanged from pre-Epic-11 behaviour)
    # ------------------------------------------------------------------

    async def _registry_entry(self, workspace: Workspace, role: str) -> CustomCommand | None:
        result = await self.session.execute(
            select(CustomCommand)
            .where(
                CustomCommand.workspace_id == workspace.id,
                CustomCommand.role == role,
                CustomCommand.enabled.is_(True),
            )
            .order_by(CustomCommand.sort_order.asc(), CustomCommand.created_at.asc())
            .limit(1)
        )
        return result.scalars().first()

    def _entry_cwd(self, workspace: Workspace, checkout: str, entry: CustomCommand | None) -> str:
        """Compose ``<checkout>/<app_root>/<entry.cwd>`` for a registry-driven launch."""
        return join_subpath(
            checkout,
            validate_relative_subpath(getattr(workspace, "app_root", None)),
            validate_relative_subpath(getattr(entry, "cwd", None)) if entry else None,
        )

    async def _ensure_runnable(
        self,
        workspace: Workspace,
        checkout: str,
        launch_cwd: str | None = None,
        *,
        require_mise: bool = True,
    ) -> DevServerState | None:
        sandbox = await self.runtime.get_sandbox(workspace)
        if not await self._is_cloned(sandbox, checkout):
            raise DevServerNotClonedError
        if require_mise:
            candidates = [launch_cwd] if launch_cwd and launch_cwd != checkout else []
            candidates.append(checkout)
            for directory in candidates:
                if await self._has_mise_config(sandbox, directory):
                    break
            else:
                return DevServerState(
                    status="no-config",
                    message="No mise.toml found in the repository.",
                )
        return None

    async def _launch_setup(
        self,
        workspace: Workspace,
        user_id: UUID,
        cwd: str,
        worktree_id: UUID | None,
        branch: str,
    ) -> DevServerState:
        """Install the toolchain + dependencies in the background, ending at `ready`."""
        entry = await self._registry_entry(workspace, "install")
        launch_cwd = self._entry_cwd(workspace, cwd, entry)
        blocked = await self._ensure_runnable(
            workspace, cwd, launch_cwd, require_mise=entry is None
        )
        if blocked is not None:
            return blocked

        # Only one dev environment is live per sandbox, so preparing a different worktree
        # first stops whatever is serving the old one.
        if workspace.dev_worktree_id != worktree_id:
            await self._manager.kill_primary(workspace)

        paths = builtin_paths()
        try:
            gen = uuid4().hex
            script = (
                build_registry_script(
                    paths, gen, command="", install_command=entry.command, serve=False
                )
                if entry
                else build_mise_setup_script(
                    paths,
                    gen,
                    mise_auto_install=self.settings.mise_auto_install,
                    mise_install_url=self.settings.mise_install_url,
                )
            )
            await self.runtime.exec(
                workspace,
                script,
                cwd=launch_cwd,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error

        workspace.dev_worktree_id = worktree_id
        await self._commit_workspace(workspace)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="dev_server_setup",
            target_type="dev_server",
            target_id=workspace.id,
            metadata={"worktree_id": str(worktree_id) if worktree_id else None},
        )
        return DevServerState(
            status="installing",
            message="Setting up environment…",
            worktree_id=worktree_id,
            branch=branch,
        )

    async def _commit_workspace(self, workspace: Workspace) -> None:
        if not all(hasattr(self.session, method) for method in ("add", "commit")):
            return
        self.session.add(workspace)
        await self.session.commit()

    async def _resolve_worktree(
        self, workspace: Workspace, worktree_id: UUID | None
    ) -> tuple[str, str, UUID | None]:
        if worktree_id is None:
            return workspace.workspace_path, workspace.default_branch, None
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            raise WorktreeNotFoundError
        return row.path, row.branch, row.id

    async def _with_worktree(self, workspace: Workspace, state: DevServerState) -> DevServerState:
        if state.worktree_id is not None or state.branch is not None:
            return state
        try:
            _, branch, resolved_id = await self._resolve_worktree(
                workspace, workspace.dev_worktree_id
            )
        except WorktreeNotFoundError:
            branch, resolved_id = workspace.default_branch, None
        return replace(state, worktree_id=resolved_id, branch=branch)

    async def _resolve_state(self, workspace: Workspace, raw: str) -> DevServerState:
        if raw == "running":
            port = await self._resolved_port(workspace)
            if port is not None:
                return DevServerState(status="running", port=port)
            return DevServerState(status="starting", message="Starting dev server…")
        if raw == "installing":
            return DevServerState(status="installing", message="Setting up environment…")
        if raw == "launching":
            return DevServerState(status="installing", message="Starting app…")
        if raw == "ready":
            return DevServerState(
                status="ready",
                message="Environment ready. Click Run App to start.",
            )
        if raw == "no-dev-task":
            return DevServerState(
                status="no-dev-task",
                message="mise.toml has no 'dev' task to run.",
            )
        if raw == "error":
            tail = await self._read_log_tail(workspace)
            message = "Environment setup failed."
            if tail:
                message = f"Environment setup failed:\n{tail}"
            return DevServerState(status="error", message=message)
        if raw == "stopped":
            return DevServerState(status="stopped")
        return DevServerState(status="idle")

    async def _read_status(self, workspace: Workspace) -> str:
        paths = builtin_paths()
        try:
            result = await self.runtime.exec(
                workspace,
                f"cat {paths.status} 2>/dev/null || echo none",
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error
        return (result.stdout or "").strip() or "none"

    async def _read_log_tail(self, workspace: Workspace, lines: int = 40) -> str:
        paths = builtin_paths()
        command = (
            f"echo '=== mise install (binary) ==='; "
            f"tail -n {lines} {paths.mise_install_log} 2>/dev/null; "
            f"echo '=== setup (mise install / dev) ==='; "
            f"tail -n {lines} {paths.log} 2>/dev/null"
        )
        try:
            result = await self.runtime.exec(
                workspace,
                command,
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError:
            return ""
        return (result.stdout or "").strip()[-2000:]

    async def _resolved_port(self, workspace: Workspace) -> int | None:
        entry = await self._registry_entry(workspace, "dev")
        declared = getattr(entry, "port", None) if entry else None
        if declared and await self._port_is_open(workspace, declared):
            return declared
        return await self._first_open_port(workspace)

    async def _first_open_port(self, workspace: Workspace) -> int | None:
        for port in self.common_ports:
            if await self._port_is_open(workspace, port):
                return port
        return None

    async def _port_is_open(self, workspace: Workspace, port: int) -> bool:
        return await port_is_open(self.runtime, workspace, port)

    async def _has_mise_config(self, sandbox, repo_path: str) -> bool:
        for name in MISE_CONFIG_FILES:
            try:
                await sandbox.fs.get_file_info(f"{repo_path}/{name}")
            except Exception:
                continue
            return True
        return False

    async def _is_cloned(self, sandbox, repo_path: str) -> bool:
        try:
            await sandbox.fs.get_file_info(f"{repo_path}/.git")
        except Exception:
            return False
        return True

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        if not workspace.daytona_sandbox_id:
            raise DevServerWorkspacePathError
        return workspace
