from dataclasses import dataclass, replace
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Workspace, WorkspaceWorktree
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.preview import COMMON_PREVIEW_PORTS
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

logger = structlog.get_logger(__name__)

# Where the background setup script records its progress and logs inside the sandbox.
STATUS_PATH = "/tmp/enigma-dev.status"
LOG_PATH = "/tmp/enigma-dev.log"
MISE_INSTALL_LOG_PATH = "/tmp/enigma-mise-install.log"
# Records the generation token of the most recent `run` launch. Only the launch that
# owns the current token may write terminal status states — so a dev server killed
# during a worktree switch cannot clobber the status of the launch that replaced it.
GEN_PATH = "/tmp/enigma-dev.gen"

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

    async def run(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState:
        """Run the app's `dev` task in a worktree. Marks it as desired so it auto-resumes
        after the sandbox sleeps and wakes. Only one dev server runs per sandbox, so
        switching to a different worktree stops the previous one first."""
        workspace = await self._get_workspace(workspace_id, user_id)
        cwd, branch, resolved_id = await self._resolve_worktree(workspace, worktree_id)

        current = await self._read_status(workspace)
        same_worktree = workspace.dev_worktree_id == resolved_id
        # Already serving this worktree → just record the desire and report state.
        if current == "running" and same_worktree:
            await self._mark_autostart(workspace, True)
            state = await self._resolve_state(workspace, current)
            return await self._with_worktree(workspace, state)
        # Setup still in progress for this worktree → can't run yet; surface progress.
        if current == "installing" and same_worktree:
            state = await self._resolve_state(workspace, current)
            return await self._with_worktree(workspace, state)

        # Switching worktrees (or a stale server elsewhere) → stop the running dev
        # server so only one exists, then launch in the target worktree.
        if not same_worktree:
            await self._stop_processes(workspace)

        return await self._launch_run(workspace, user_id, cwd, resolved_id, branch)

    async def _ensure_runnable(self, workspace: Workspace, cwd: str) -> DevServerState | None:
        """Validate the working directory is a checkout with a mise config. Returns a
        `no-config` state to surface, or `None` when good. Raises if it isn't a checkout."""
        sandbox = await self.runtime.get_sandbox(workspace)
        if not await self._is_cloned(sandbox, cwd):
            raise DevServerNotClonedError
        if not await self._has_mise_config(sandbox, cwd):
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
        blocked = await self._ensure_runnable(workspace, cwd)
        if blocked is not None:
            return blocked

        # Only one dev environment is live per sandbox, so preparing a different worktree
        # first stops whatever is serving the old one — otherwise the setup's `ready` status
        # and the still-running server describe two different worktrees at once.
        if workspace.dev_worktree_id != worktree_id:
            await self._stop_processes(workspace)

        try:
            await self.runtime.exec(
                workspace,
                self._setup_command(uuid4().hex),
                cwd=cwd,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error

        # Record which worktree the prepared environment belongs to (does NOT mark the
        # app as desired — that is the separate `run` phase).
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

    async def _launch_run(
        self,
        workspace: Workspace,
        user_id: UUID,
        cwd: str,
        worktree_id: UUID | None,
        branch: str,
    ) -> DevServerState:
        """Run the `dev` task in a worktree in the background and mark it as desired so
        it auto-resumes after a sandbox sleep/wake."""
        blocked = await self._ensure_runnable(workspace, cwd)
        if blocked is not None:
            return blocked

        # A feature worktree has no project deps installed yet (the post-clone setup
        # only prepares the base clone), so its run installs them first — a fast no-op
        # once installed. The base clone keeps its fast run (deps prepared by setup).
        install_deps = worktree_id is not None
        try:
            await self.runtime.exec(
                workspace,
                self._run_command(uuid4().hex, install_deps=install_deps),
                cwd=cwd,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error

        workspace.dev_autostart = True
        workspace.dev_worktree_id = worktree_id
        await self._commit_workspace(workspace)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="dev_server_run",
            target_type="dev_server",
            target_id=workspace.id,
            metadata={"worktree_id": str(worktree_id) if worktree_id else None},
        )
        return DevServerState(
            status="installing",
            message="Starting app…",
            worktree_id=worktree_id,
            branch=branch,
        )

    async def status(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        workspace = await self._get_workspace(workspace_id, user_id)
        raw = await self._read_status(workspace)
        return await self._with_worktree(workspace, await self._resolve_state(workspace, raw))

    async def ensure_running(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        """Idempotent auto-resume: if the dev server is *desired* (was started before)
        but is no longer actually serving — typically because the sandbox auto-stopped
        and woke — relaunch it in the worktree it was last running in. Reading status
        already wakes the sandbox VM."""
        workspace = await self._get_workspace(workspace_id, user_id)
        raw = await self._read_status(workspace)
        resolved = await self._resolve_state(workspace, raw)

        # Never auto-start a dev server the user never asked for (or explicitly stopped).
        if not workspace.dev_autostart:
            return await self._with_worktree(workspace, resolved)

        # Serving / setup in progress / nothing to run / surfaced failure → leave it.
        if resolved.status in {"running", "installing", "no-dev-task", "error"}:
            return await self._with_worktree(workspace, resolved)

        # Status file says "running" but no port is open yet. Only relaunch if the
        # process is actually dead (stale status after a wake); otherwise it's just a
        # genuinely slow startup and we must not double-launch.
        if resolved.status == "starting" and await self._dev_process_alive(workspace):
            return await self._with_worktree(workspace, resolved)

        # Any other "desired but down" status still has to be checked against reality: a
        # stale status (a `setup` that finished after the server came up, a crashed status
        # write) would otherwise start a second `mise run dev` while the first still holds
        # the port. The live process always belongs to `dev_worktree_id`, which is the very
        # worktree this method would relaunch in, so leaving it alone is correct.
        if await self._dev_process_alive(workspace):
            return await self._with_worktree(workspace, resolved)

        # Desired but down (stopped / idle / ready / dead-after-wake) → relaunch the
        # dev task in the worktree it was last running in; dependencies installed by
        # `setup`/a previous run persist on the sandbox disk.
        cwd, branch, resolved_id = await self._resolve_worktree(
            workspace, workspace.dev_worktree_id
        )
        try:
            return await self._launch_run(workspace, user_id, cwd, resolved_id, branch)
        except DevServerNotClonedError:
            return DevServerState(status="idle", message="Repository is not cloned yet.")

    async def stop(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        workspace = await self._get_workspace(workspace_id, user_id)
        try:
            # The bracket trick keeps pkill from matching its own command line.
            await self.runtime.exec(
                workspace,
                (
                    "pkill -f '[m]ise run dev' >/dev/null 2>&1 || true; "
                    "pkill -f '[m]ise install' >/dev/null 2>&1 || true; "
                    f"echo stopped > {STATUS_PATH}"
                ),
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error
        # An explicit stop clears the desired flag and the bound worktree so it is not
        # auto-resumed on wake.
        workspace.dev_autostart = False
        workspace.dev_worktree_id = None
        await self._commit_workspace(workspace)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="dev_server_stop",
            target_type="dev_server",
            target_id=workspace.id,
            metadata={},
        )
        return DevServerState(status="stopped")

    async def _stop_processes(self, workspace: Workspace) -> None:
        """Kill any running dev server or in-flight setup without touching the desired flag
        or status file, so a worktree switch can launch a fresh one. The generation guard in
        the run/setup scripts prevents a killed script from clobbering the new launch's
        status."""
        try:
            await self.runtime.exec(
                workspace,
                (
                    "pkill -f '[m]ise run dev' >/dev/null 2>&1 || true; "
                    "pkill -f '[m]ise install' >/dev/null 2>&1 || true; "
                    "pkill -f '[m]ise run install' >/dev/null 2>&1 || true"
                ),
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError:
            pass

    async def _mark_autostart(self, workspace: Workspace, value: bool) -> None:
        if workspace.dev_autostart == value:
            return
        workspace.dev_autostart = value
        await self._commit_workspace(workspace)

    async def _commit_workspace(self, workspace: Workspace) -> None:
        # Persist only when backed by a real session (mirrors AuditLogService.record).
        if not all(hasattr(self.session, method) for method in ("add", "commit")):
            return
        self.session.add(workspace)
        await self.session.commit()

    async def _dev_process_alive(self, workspace: Workspace) -> bool:
        # `pgrep ...; echo $?` always exits 0, so we read the pgrep exit code from
        # stdout ("0" = a matching process is alive). The `[m]ise` bracket keeps pgrep
        # from matching its own command line.
        try:
            result = await self.runtime.exec(
                workspace,
                "pgrep -f '[m]ise run dev' >/dev/null 2>&1; echo $?",
                cwd=workspace.workspace_path,
                timeout=5,
            )
        except DaytonaRuntimeError:
            return False
        return (result.stdout or "").strip() == "0"

    async def _resolve_worktree(
        self, workspace: Workspace, worktree_id: UUID | None
    ) -> tuple[str, str, UUID | None]:
        """Map a worktree id to ``(cwd, branch, worktree_id)``; ``None`` => base clone.

        Mirrors ``WorktreeService.resolve_path`` — raw paths never cross the API; a
        worktree is only ever targeted by its database id, confined to the workspace."""
        if worktree_id is None:
            return workspace.workspace_path, workspace.default_branch, None
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            raise WorktreeNotFoundError
        return row.path, row.branch, row.id

    async def _with_worktree(self, workspace: Workspace, state: DevServerState) -> DevServerState:
        """Attach the running worktree id/branch to a state that does not already carry
        them, so status/ensure responses tell the client which branch is live."""
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
            port = await self._first_open_port(workspace)
            if port is not None:
                return DevServerState(status="running", port=port)
            return DevServerState(status="starting", message="Starting dev server…")
        if raw == "installing":
            return DevServerState(status="installing", message="Setting up environment…")
        # `launching` is the brief window after Run App while the run script re-checks
        # the toolchain, before the dev server is up. Reported as `installing` so the
        # auto-resume poller treats it as in-flight and never double-launches.
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

    def _setup_command(self, gen: str) -> str:
        """Background script that prepares the environment — installs the mise
        toolchain and project dependencies — then stops at `ready`. It deliberately
        does NOT run the `dev` task; that is the separate `run` phase.

        Guarded by the same generation token as `_run_command`: an in-flight setup that has
        been superseded (a different worktree was set up, or a dev server was started) must
        not write `installing`/`ready`/`error` over the newer launch's status. Unguarded, its
        trailing `ready` write turned a *running* dev server into "desired but down", and the
        next `ensure_running` poll started a second server on the same port."""
        install_line = ""
        if self.settings.mise_auto_install:
            install_line = (
                "if ! command -v mise >/dev/null 2>&1; then "
                "echo '[setup] installing mise'; "
                f"curl -fsSL {self.settings.mise_install_url} | sh; fi\n"
            )
        # Runs entirely in the background so the request returns immediately while
        # `mise install` (which can take minutes) proceeds in the sandbox.
        # Everything (stdout+stderr, traced) is appended to a single log from the very
        # first line so even an early failure is captured; status writes go to a
        # separate file via explicit redirects and are unaffected by the exec redirect.
        script = (
            f"exec >{LOG_PATH} 2>&1\n"
            "set -x\n"
            'export PATH="$HOME/.local/bin:$PATH"\n'
            'export MISE_INSTALL_PATH="$HOME/.local/bin/mise"\n'
            # Claim this generation, then guard every status write against it.
            f"echo {gen} > {GEN_PATH}\n"
            f'setst() {{ [ "$(cat {GEN_PATH} 2>/dev/null)" = {gen} ] && '
            f'echo "$1" > {STATUS_PATH}; }}\n'
            "setst installing\n"
            f"{install_line}"
            'export PATH="$HOME/.local/bin:$PATH"\n'
            "command -v mise || "
            "{ echo '[setup] mise not found on PATH'; setst error; exit 127; }\n"
            "mise trust || echo '[setup] mise trust failed (continuing)'\n"
            "mise install || "
            "{ echo '[setup] mise install failed'; setst error; exit 1; }\n"
            # mise installs language *tools* (node, pnpm, …) but not project
            # dependencies. If the repo defines an `install` task (e.g. `pnpm install`),
            # run it now so the app is ready to start — otherwise `mise run dev` would
            # later fail with errors like `next: not found` (empty node_modules).
            "if mise task ls 2>/dev/null | grep -qw install; then "
            "echo '[setup] installing project dependencies'; "
            "mise run install || "
            "{ echo '[setup] dependency install failed'; setst error; exit 1; }; "
            "fi\n"
            # Environment prepared — stop here. The app is started on demand via `run`.
            "echo '[setup] environment ready'; setst ready\n"
        )
        return f"nohup sh -c {self._shell_quote(script)} >/dev/null 2>&1 &"

    def _run_command(self, gen: str, install_deps: bool) -> str:
        """Background script that runs the app's `dev` task in the current directory.

        A generation token guards every terminal status write and the dev-server launch
        itself: only the launch that owns the current token (last writer of `GEN_PATH`)
        may act. This lets a worktree switch kill the previous dev server without it
        clobbering the new launch's status or starting a second server."""
        # Optionally (re)install project deps first — a feature worktree starts with an
        # empty working directory, so its first run installs before serving.
        deps_block = ""
        if install_deps:
            deps_block = (
                "if mise task ls 2>/dev/null | grep -qw install; then "
                "echo '[run] installing project dependencies'; "
                "mise run install || "
                "{ echo '[run] dependency install failed'; setst error; exit 1; }; "
                "fi\n"
            )
        script = (
            f"exec >{LOG_PATH} 2>&1\n"
            "set -x\n"
            'export PATH="$HOME/.local/bin:$PATH"\n'
            'export MISE_INSTALL_PATH="$HOME/.local/bin/mise"\n'
            # Claim this generation, then guard every status write against it.
            f"echo {gen} > {GEN_PATH}\n"
            f'setst() {{ [ "$(cat {GEN_PATH} 2>/dev/null)" = {gen} ] && '
            f'echo "$1" > {STATUS_PATH}; }}\n'
            "setst launching\n"
            "command -v mise || "
            "{ echo '[run] mise not found on PATH'; setst error; exit 127; }\n"
            "mise trust || echo '[run] mise trust failed (continuing)'\n"
            "mise install || { echo '[run] mise install failed'; setst error; exit 1; }\n"
            f"{deps_block}"
            # Bail out if a newer launch superseded us (e.g. a worktree switch) before we
            # start the dev server, so two dev servers never run at once.
            f'[ "$(cat {GEN_PATH} 2>/dev/null)" = {gen} ] || '
            "{ echo '[run] superseded before start'; exit 0; }\n"
            "if mise task ls 2>/dev/null | grep -qw dev; then "
            "setst running; "
            "mise run dev; "
            "setst stopped; "
            "else echo '[run] no dev task found'; setst no-dev-task; fi\n"
        )
        return f"nohup sh -c {self._shell_quote(script)} >/dev/null 2>&1 &"

    @staticmethod
    def _shell_quote(value: str) -> str:
        # Single-quote for the outer shell; the script body contains no single quotes.
        return "'" + value.replace("'", "'\\''") + "'"

    async def _read_status(self, workspace: Workspace) -> str:
        try:
            result = await self.runtime.exec(
                workspace,
                f"cat {STATUS_PATH} 2>/dev/null || echo none",
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error
        return (result.stdout or "").strip() or "none"

    async def _read_log_tail(self, workspace: Workspace, lines: int = 40) -> str:
        command = (
            f"echo '=== mise install (binary) ==='; "
            f"tail -n {lines} {MISE_INSTALL_LOG_PATH} 2>/dev/null; "
            f"echo '=== setup (mise install / dev) ==='; "
            f"tail -n {lines} {LOG_PATH} 2>/dev/null"
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
        # Keep the surfaced message bounded — the UI shows this inline.
        return (result.stdout or "").strip()[-2000:]

    async def _first_open_port(self, workspace: Workspace) -> int | None:
        for port in self.common_ports:
            if await self._port_is_open(workspace, port):
                return port
        return None

    async def _port_is_open(self, workspace: Workspace, port: int) -> bool:
        code = (
            "import socket; "
            "s=socket.socket(); s.settimeout(.3); "
            f"r=s.connect_ex(('127.0.0.1',{port})); s.close(); "
            "raise SystemExit(0 if r == 0 else 1)"
        )
        try:
            result = await self.runtime.exec(
                workspace,
                f'python3 -c "{code}"',
                cwd=workspace.workspace_path,
                timeout=3,
            )
        except DaytonaRuntimeError:
            return False
        return result.exit_code == 0

    async def _has_mise_config(self, sandbox, repo_path: str) -> bool:
        for name in MISE_CONFIG_FILES:
            try:
                await sandbox.fs.get_file_info(f"{repo_path}/{name}")
            except Exception:
                continue
            return True
        return False

    async def _is_cloned(self, sandbox, repo_path: str) -> bool:
        # A base clone's `.git` is a directory; a worktree's `.git` is a file (a gitdir
        # link). Either presence means the working directory is a git checkout.
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
