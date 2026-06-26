from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Workspace
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.preview import COMMON_PREVIEW_PORTS
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)

# Where the background setup script records its progress and logs inside the sandbox.
STATUS_PATH = "/tmp/enigma-dev.status"
LOG_PATH = "/tmp/enigma-dev.log"
MISE_INSTALL_LOG_PATH = "/tmp/enigma-mise-install.log"

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

    async def start(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        workspace = await self._get_workspace(workspace_id, user_id)

        # If a setup run is already in flight (or the dev server is up), don't start
        # a second one — the background script keeps running across requests. Still
        # mark it as desired so it auto-resumes after the sandbox sleeps and wakes.
        current = await self._read_status(workspace)
        if current in {"installing", "running"}:
            await self._mark_autostart(workspace, True)
            return await self._resolve_state(workspace, current)

        return await self._launch(workspace, user_id)

    async def _launch(self, workspace: Workspace, user_id: UUID) -> DevServerState:
        """Launch the background setup + dev-server script and mark it as desired."""
        sandbox = await self.runtime.get_sandbox(workspace)
        if not await self._is_cloned(sandbox, workspace.workspace_path):
            raise DevServerNotClonedError
        if not await self._has_mise_config(sandbox, workspace.workspace_path):
            return DevServerState(
                status="no-config",
                message="No mise.toml found in the repository.",
            )

        try:
            await self.runtime.exec(
                workspace,
                self._launch_command(),
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise DevServerRuntimeError(str(error)) from error

        await self._mark_autostart(workspace, True)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="dev_server_start",
            target_type="dev_server",
            target_id=workspace.id,
            metadata={},
        )
        return DevServerState(status="installing", message="Setting up environment…")

    async def status(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        workspace = await self._get_workspace(workspace_id, user_id)
        raw = await self._read_status(workspace)
        return await self._resolve_state(workspace, raw)

    async def ensure_running(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        """Idempotent auto-resume: if the dev server is *desired* (was started before)
        but is no longer actually serving — typically because the sandbox auto-stopped
        and woke — relaunch it. Reading status already wakes the sandbox VM."""
        workspace = await self._get_workspace(workspace_id, user_id)
        raw = await self._read_status(workspace)
        resolved = await self._resolve_state(workspace, raw)

        # Never auto-start a dev server the user never asked for (or explicitly stopped).
        if not workspace.dev_autostart:
            return resolved

        # Serving / setup in progress / nothing to run / surfaced failure → leave it.
        if resolved.status in {"running", "installing", "no-dev-task", "error"}:
            return resolved

        # Status file says "running" but no port is open yet. Only relaunch if the
        # process is actually dead (stale status after a wake); otherwise it's just a
        # genuinely slow startup and we must not double-launch.
        if resolved.status == "starting" and await self._dev_process_alive(workspace):
            return resolved

        # Desired but down (stopped / idle / dead-after-wake) → relaunch.
        try:
            return await self._launch(workspace, user_id)
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
        # An explicit stop clears the desired flag so it is not auto-resumed on wake.
        await self._mark_autostart(workspace, False)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="dev_server_stop",
            target_type="dev_server",
            target_id=workspace.id,
            metadata={},
        )
        return DevServerState(status="stopped")

    async def _mark_autostart(self, workspace: Workspace, value: bool) -> None:
        if workspace.dev_autostart == value:
            return
        workspace.dev_autostart = value
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

    async def _resolve_state(self, workspace: Workspace, raw: str) -> DevServerState:
        if raw == "running":
            port = await self._first_open_port(workspace)
            if port is not None:
                return DevServerState(status="running", port=port)
            return DevServerState(status="starting", message="Starting dev server…")
        if raw == "installing":
            return DevServerState(status="installing", message="Setting up environment…")
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

    def _launch_command(self) -> str:
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
            f"echo installing > {STATUS_PATH}\n"
            f"{install_line}"
            'export PATH="$HOME/.local/bin:$PATH"\n'
            "command -v mise || "
            f"{{ echo '[setup] mise not found on PATH'; echo error > {STATUS_PATH}; exit 127; }}\n"
            "mise trust || echo '[setup] mise trust failed (continuing)'\n"
            "mise install || "
            f"{{ echo '[setup] mise install failed'; echo error > {STATUS_PATH}; exit 1; }}\n"
            # mise installs language *tools* (node, pnpm, …) but not project
            # dependencies. If the repo defines an `install` task (e.g. `pnpm install`),
            # run it first — otherwise `mise run dev` fails with errors like
            # `next: not found` because node_modules was never populated.
            "if mise task ls 2>/dev/null | grep -qw install; then "
            "echo '[setup] installing project dependencies'; "
            "mise run install || "
            f"{{ echo '[setup] dependency install failed'; echo error > {STATUS_PATH}; exit 1; }}; "
            "fi\n"
            "if mise task ls 2>/dev/null | grep -qw dev; then "
            f"echo running > {STATUS_PATH}; "
            "mise run dev; "
            f"echo stopped > {STATUS_PATH}; "
            f"else echo '[setup] no dev task found'; echo no-dev-task > {STATUS_PATH}; fi\n"
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
        try:
            info = await sandbox.fs.get_file_info(f"{repo_path}/.git")
        except Exception:
            return False
        return bool(getattr(info, "is_dir", False))

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        if not workspace.daytona_sandbox_id:
            raise DevServerWorkspacePathError
        return workspace
