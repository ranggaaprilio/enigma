"""Long-running service instances: start/stop/restart, auto-resume, and multi-port preview.

The definition/instance split is the core of Epic 11: `custom_commands` (Epic 10, `kind='service'`)
says *what* to run; `workspace_services` says *what is running now, where, and whether the user
wants it running*. A workspace's built-in mise `dev` task — Epic 10 dropped seeding a registry
entry for it, so a mise repository with an empty registry still needs somewhere to live — is
represented by a `workspace_services` row with `custom_command_id = NULL`.

The **primary** service is whichever row `DevServerService` (the pre-Epic-11 `/dev/*` facade)
controls: the workspace's enabled `role='dev'` service command if one exists, else the built-in
row. It alone uses the legacy `/tmp/enigma-dev.*` paths, so a workspace that had a dev server
running before this migration keeps behaving identically. Every other service gets its own
`/tmp/enigma-svc-<id>.*` files (`service_scripts.py`).

Supervision stays `nohup` + status/pid files, not Daytona sessions — a session dies with sandbox
auto-stop and its SDK-side log follower dies with a backend restart, so `ensure_running`
reconciliation would be needed either way; sessions are used only for log tailing (Epic 11's
log-stream endpoint), where that lifetime is exactly what is wanted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import CustomCommand, PreviewSession, Workspace, WorkspaceService, WorkspaceWorktree
from app.models.workspace_service import WORKSPACE_SERVICE_ACTIVE_STATUSES
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.git_providers import redact_secrets
from app.services.preview import COMMON_PREVIEW_PORTS, PreviewService
from app.services.run_detection import infer_port
from app.services.run_paths import join_subpath, resolve_worktree, validate_relative_subpath
from app.services.sandbox_probe import port_is_open, probe_ports
from app.services.service_scripts import (
    ProbeResult,
    ProbeRow,
    ServicePaths,
    build_mise_run_script,
    build_probe_script,
    build_registry_script,
    builtin_paths,
    kill_script,
    parse_probe_output,
    service_paths,
)
from app.services.workspace_env import (
    EnvFileError,
    load_all_values,
    prepare_launch_env,
    redactable_env_values,
)
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)

MISE_CONFIG_FILES = ("mise.toml", ".mise.toml", "mise/config.toml", ".config/mise/config.toml")

# A row in any of these statuses is "alive" for cap/port-conflict purposes. Re-exported alias
# of ``models.workspace_service.WORKSPACE_SERVICE_ACTIVE_STATUSES`` (the canonical home, so
# ``workspace_env.py`` -- which this module imports from -- can use it without a cycle).
ACTIVE_STATUSES = WORKSPACE_SERVICE_ACTIVE_STATUSES


class WorkspaceServiceWorkspacePathError(Exception):
    pass


class WorkspaceServiceNotClonedError(Exception):
    pass


class WorkspaceServiceRuntimeError(Exception):
    pass


class WorkspaceServiceNotFoundError(Exception):
    pass


class WorkspaceServicePortConflictError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class WorkspaceServiceLimitError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class WorkspaceServiceManager:
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

    # ------------------------------------------------------------------
    # Multi-service surface (`api/workspace_services.py`)
    # ------------------------------------------------------------------

    async def list(
        self, workspace_id: UUID, user_id: UUID
    ) -> tuple[list[WorkspaceService], WorkspaceService]:
        """Every service row, refreshed against the sandbox in one probe, with preview
        sessions reconciled to match. Returns ``(rows, primary_row)``."""
        workspace = await self._get_workspace(workspace_id, user_id)
        await self._sync_definitions(workspace)
        rows = await self._all_rows(workspace)
        primary = await self.primary_service(workspace)
        if not any(row.id == primary.id for row in rows):
            rows.append(primary)
        rows.sort(key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC))

        probe_map, target_ports = await self._probe_rows(workspace, rows, primary)
        claimed_ports = {row.port for row in rows if row.port is not None}
        for row in rows:
            await self._apply_probe(
                workspace, row, probe_map.get(str(row.id)), target_ports.get(row.id), claimed_ports
            )
            if row.port is not None:
                claimed_ports.add(row.port)
        await self.session.commit()
        await self._reconcile_previews(workspace, rows)
        return rows, primary

    async def start(
        self,
        workspace_id: UUID,
        service_id: UUID,
        user_id: UUID,
        worktree_id: UUID | None = None,
    ) -> tuple[WorkspaceService, str | None]:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self._get_row(workspace, service_id)
        primary = await self.primary_service(workspace)
        return await self._start_row(workspace, row, user_id, worktree_id, row.id == primary.id)

    async def stop(self, workspace_id: UUID, service_id: UUID, user_id: UUID) -> WorkspaceService:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self._get_row(workspace, service_id)
        primary = await self.primary_service(workspace)
        return await self._stop_row(workspace, row, user_id, row.id == primary.id)

    async def restart(
        self,
        workspace_id: UUID,
        service_id: UUID,
        user_id: UUID,
        worktree_id: UUID | None = None,
    ) -> WorkspaceService:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self._get_row(workspace, service_id)
        primary = await self.primary_service(workspace)
        is_primary = row.id == primary.id
        target_worktree_id = worktree_id if worktree_id is not None else row.worktree_id
        _, _, resolved_worktree_id = await resolve_worktree(
            self.session, workspace, target_worktree_id
        )
        await self._kill(workspace, row, is_primary)
        return await self._launch(
            workspace, row, user_id, worktree_id=resolved_worktree_id, is_primary=is_primary
        )

    async def ensure_running(
        self, workspace_id: UUID, user_id: UUID
    ) -> tuple[list[WorkspaceService], WorkspaceService]:
        workspace = await self._get_workspace(workspace_id, user_id)
        await self._sync_definitions(workspace)
        rows = await self._all_rows(workspace)
        primary = await self.primary_service(workspace)
        if not any(row.id == primary.id for row in rows):
            rows.append(primary)

        probe_map, target_ports = await self._probe_rows(workspace, rows, primary)
        claimed_ports = {row.port for row in rows if row.port is not None}
        relaunch: list[WorkspaceService] = []
        for row in rows:
            probe = probe_map.get(str(row.id))
            await self._apply_probe(
                workspace, row, probe, target_ports.get(row.id), claimed_ports
            )
            if row.port is not None:
                claimed_ports.add(row.port)
            if await self._should_relaunch(row, probe):
                relaunch.append(row)
        await self.session.commit()

        for row in relaunch:
            is_primary = row.id == primary.id
            try:
                await self._launch(
                    workspace, row, user_id, worktree_id=row.worktree_id, is_primary=is_primary
                )
            except WorkspaceServiceNotClonedError:
                row.status = "stopped"
                row.last_error = "Repository is not cloned yet."
                self._add(row)
                await self.session.commit()

        await self._reconcile_previews(workspace, rows)
        return rows, primary

    async def logs_tail(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, tail: int
    ) -> str:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self._get_row(workspace, service_id)
        primary = await self.primary_service(workspace)
        paths = self._paths_for(row, row.id == primary.id)
        try:
            result = await self.runtime.exec(
                workspace,
                f"tail -c {tail} {paths.log} 2>/dev/null || true",
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise WorkspaceServiceRuntimeError(str(error)) from error
        content = result.stdout or ""
        # A log's content is user-visible: redact the same way the log-stream WebSocket does,
        # so a poll of this endpoint can never leak a secret the stream already hides.
        env = await load_all_values(self.session, self.settings, workspace_id)
        return redact_secrets(content, *redactable_env_values(env))

    async def log_paths(self, workspace_id: UUID, service_id: UUID, user_id: UUID) -> ServicePaths:
        """Resolve which sandbox files a service's log stream should tail. Split from
        `logs_tail` so the WebSocket endpoint can build its own `stream_command` call."""
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self._get_row(workspace, service_id)
        primary = await self.primary_service(workspace)
        return self._paths_for(row, row.id == primary.id)

    async def to_response(self, workspace: Workspace, row: WorkspaceService) -> dict:
        """Enrich a bare `WorkspaceService` row with the display fields the API response
        needs — the command it runs, its branch, and its current preview URL, if any."""
        command = await self._command_for(row)
        branch = await self._branch_for(workspace, row.worktree_id)
        preview = await self._active_preview_for_service(workspace, row.id)
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "custom_command_id": row.custom_command_id,
            "name": command.name if command is not None else "Dev Server",
            "role": command.role if command is not None else "dev",
            "kind": command.kind if command is not None else "service",
            "command": command.command if command is not None else None,
            "port": row.port,
            "status": row.status,
            "desired": row.desired,
            "worktree_id": row.worktree_id,
            "branch": branch,
            "last_error": row.last_error,
            "exit_code": row.exit_code,
            "started_at": row.started_at,
            "last_seen_at": row.last_seen_at,
            "preview_session_id": preview.id if preview is not None else None,
            "public_url": preview.public_url if preview is not None else None,
        }

    async def to_response_list(
        self, workspace: Workspace, rows: list[WorkspaceService]
    ) -> list[dict]:
        return [await self.to_response(workspace, row) for row in rows]

    def environment_from_primary(self, primary: WorkspaceService) -> dict:
        """The workspace-wide mise environment state (install/mise-trust), read off the
        *already-probed* primary row's `_raw_status` — no extra sandbox round-trip beyond the
        one `list()`/`ensure_running()` already made."""
        raw = getattr(primary, "_raw_status", None)
        if raw == "ready":
            return {"status": "ready", "message": "Environment ready. Click Run App to start."}
        if raw in ("installing", "launching"):
            return {"status": "installing", "message": "Setting up environment…"}
        if primary.status == "error" and primary.last_error == "no-config":
            return {"status": "no-config", "message": "No mise.toml found in the repository."}
        if primary.status == "error":
            return {"status": "error", "message": primary.last_error or "Environment setup failed."}
        return {"status": "idle", "message": None}

    async def _branch_for(self, workspace: Workspace, worktree_id: UUID | None) -> str:
        if worktree_id is None:
            return workspace.default_branch
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            return workspace.default_branch
        return row.branch

    async def _active_preview_for_service(
        self, workspace: Workspace, service_id: UUID
    ) -> PreviewSession | None:
        result = await self.session.execute(
            select(PreviewSession)
            .where(
                PreviewSession.workspace_id == workspace.id,
                PreviewSession.service_id == service_id,
                PreviewSession.status == "active",
            )
            .limit(1)
        )
        return result.scalars().first()

    # ------------------------------------------------------------------
    # Compatibility facade (`services/dev_server.py`)
    # ------------------------------------------------------------------

    async def primary_service(self, workspace: Workspace) -> WorkspaceService:
        """The dev-role service command's row if one exists, else the built-in row —
        created (not yet flushed to a real id-bearing row unless the session supports it) the
        first time either is needed.

        A freshly created row seeds `desired`/`worktree_id` from the workspace's deprecated
        `dev_autostart`/`dev_worktree_id` columns — the same one-time backfill the Alembic
        migration performs for existing data, needed here too because those columns can still
        be the only record of "this was running" (e.g. a workspace touched directly in a test,
        or any row that predates this migration slipping through untouched).
        """
        dev_command = await self._registry_command(workspace, "dev")
        if dev_command is not None:
            row = await self._get_row_for_command(workspace, dev_command.id)
            if row is None:
                row = WorkspaceService(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    custom_command_id=dev_command.id,
                    worktree_id=workspace.dev_worktree_id,
                    status="stopped",
                    desired=bool(workspace.dev_autostart),
                )
                self._add(row)
            return row
        row = await self._get_builtin_row(workspace)
        if row is None:
            row = WorkspaceService(
                id=uuid4(),
                workspace_id=workspace.id,
                custom_command_id=None,
                worktree_id=workspace.dev_worktree_id,
                status="stopped",
                desired=bool(workspace.dev_autostart),
            )
            self._add(row)
        return row

    async def kill_primary(self, workspace: Workspace) -> None:
        """Best-effort kill of whatever the primary row currently has running, without
        touching `desired`/status — used by `DevServerService.setup()` when preparing a
        different worktree than the one currently serving."""
        row = await self.primary_service(workspace)
        await self._kill(workspace, row, True)

    async def run_primary(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> WorkspaceService:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self.primary_service(workspace)
        row, _warning = await self._start_row(workspace, row, user_id, worktree_id, True)
        return row

    async def stop_primary(self, workspace_id: UUID, user_id: UUID) -> WorkspaceService:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self.primary_service(workspace)
        return await self._stop_row(workspace, row, user_id, True)

    async def status_primary(self, workspace_id: UUID, user_id: UUID) -> WorkspaceService:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self.primary_service(workspace)
        probe_map, target_ports = await self._probe_rows(workspace, [row], row)
        await self._apply_probe(
            workspace, row, probe_map.get(str(row.id)), target_ports.get(row.id), frozenset()
        )
        await self.session.commit()
        return row

    async def ensure_running_primary(self, workspace_id: UUID, user_id: UUID) -> WorkspaceService:
        workspace = await self._get_workspace(workspace_id, user_id)
        row = await self.primary_service(workspace)
        probe_map, target_ports = await self._probe_rows(workspace, [row], row)
        probe = probe_map.get(str(row.id))
        await self._apply_probe(workspace, row, probe, target_ports.get(row.id), frozenset())
        if not row.desired:
            # Never auto-start what the user never asked for (or explicitly stopped) — but
            # still report the freshly probed state, exactly like a plain status read.
            await self.session.commit()
            return row
        should_relaunch = await self._should_relaunch(row, probe)
        await self.session.commit()
        if not should_relaunch:
            return row
        try:
            return await self._launch(
                workspace, row, user_id, worktree_id=row.worktree_id, is_primary=True
            )
        except WorkspaceServiceNotClonedError:
            row.status = "stopped"
            row.last_error = "Repository is not cloned yet."
            self._add(row)
            await self.session.commit()
            return row

    # ------------------------------------------------------------------
    # Hooks called from other domains
    # ------------------------------------------------------------------

    async def stop_for_command(self, workspace: Workspace, command_id: UUID) -> None:
        """A registry command whose service was deleted or disabled must not keep running
        under everyone's radar. Best-effort: swallow sandbox errors, the DB row still updates."""
        row = await self._get_row_for_command(workspace, command_id)
        if row is None or row.status not in (*ACTIVE_STATUSES, "exited"):
            return
        primary = await self.primary_service(workspace)
        try:
            await self._stop_row(workspace, row, workspace.owner_id, row.id == primary.id)
        except WorkspaceServiceRuntimeError:
            pass

    async def on_worktree_removed(self, workspace: Workspace, worktree_id: UUID) -> None:
        """Kill any service running in a worktree that is about to be removed, so the process
        does not survive detached from a now-unlinked working directory."""
        result = await self.session.execute(
            select(WorkspaceService).where(
                WorkspaceService.workspace_id == workspace.id,
                WorkspaceService.worktree_id == worktree_id,
            )
        )
        rows = list(result.scalars().all())
        if not rows:
            return
        primary = await self.primary_service(workspace)
        for row in rows:
            is_primary = row.id == primary.id
            if row.status in ACTIVE_STATUSES:
                await self._kill(workspace, row, is_primary)
            row.desired = False
            row.status = "stopped"
            row.last_error = "Worktree removed"
            self._add(row)
            if is_primary:
                workspace.dev_autostart = False
                workspace.dev_worktree_id = None
                self._add(workspace)
        await self.session.commit()
        for row in rows:
            await self._stop_previews(workspace, row)

    # ------------------------------------------------------------------
    # Internals shared by both surfaces
    # ------------------------------------------------------------------

    async def _start_row(
        self,
        workspace: Workspace,
        row: WorkspaceService,
        user_id: UUID,
        worktree_id: UUID | None,
        is_primary: bool,
    ) -> tuple[WorkspaceService, str | None]:
        command = await self._command_for(row)
        _, _, resolved_worktree_id = await resolve_worktree(self.session, workspace, worktree_id)
        await self._refresh_single(workspace, row, is_primary)

        same_worktree = row.worktree_id == resolved_worktree_id
        if row.status == "running" and same_worktree:
            row.desired = True
            self._add(row)
            if is_primary:
                workspace.dev_autostart = True
                workspace.dev_worktree_id = resolved_worktree_id
                self._add(workspace)
            await self.session.commit()
            return row, None
        if row.status == "installing" and same_worktree:
            return row, None

        if not same_worktree and row.status in ACTIVE_STATUSES:
            await self._kill(workspace, row, is_primary)

        await self._check_limit(workspace, row)
        target_port = None
        if command is not None:
            target_port = command.port or infer_port(command.command)
        warning = await self._check_port_conflict(workspace, row, target_port)

        row = await self._launch(
            workspace, row, user_id, worktree_id=resolved_worktree_id, is_primary=is_primary
        )
        return row, warning

    async def _stop_row(
        self, workspace: Workspace, row: WorkspaceService, user_id: UUID, is_primary: bool
    ) -> WorkspaceService:
        paths = self._paths_for(row, is_primary)
        try:
            await self.runtime.exec(
                workspace,
                f"{kill_script(paths, legacy_pkill=is_primary)}; echo stopped > {paths.status}",
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise WorkspaceServiceRuntimeError(str(error)) from error
        row.desired = False
        row.status = "stopped"
        row.last_error = None
        row._raw_status = "stopped"  # noqa: SLF001 - matches the sandbox file we just wrote
        self._add(row)
        if is_primary:
            workspace.dev_autostart = False
            workspace.dev_worktree_id = None
            self._add(workspace)
        await self.session.commit()
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="service_stop",
            target_type="workspace_service",
            target_id=row.id,
            metadata={
                "custom_command_id": (
                    str(row.custom_command_id) if row.custom_command_id else None
                )
            },
        )
        await self._stop_previews(workspace, row)
        return row

    async def _launch(
        self,
        workspace: Workspace,
        row: WorkspaceService,
        user_id: UUID,
        *,
        worktree_id: UUID | None,
        is_primary: bool,
    ) -> WorkspaceService:
        command = await self._command_for(row)
        checkout, _branch, resolved_worktree_id = await resolve_worktree(
            self.session, workspace, worktree_id
        )
        launch_cwd = await self._entry_cwd(workspace, checkout, command)

        reason = await self._ensure_runnable(
            workspace, checkout, launch_cwd, require_mise=command is None
        )
        if reason is not None:
            row.status = "error"
            row.last_error = reason
            row.worktree_id = resolved_worktree_id
            self._add(row)
            await self.session.commit()
            return row

        try:
            env = await prepare_launch_env(
                self.session,
                self.settings,
                self.runtime,
                workspace,
                checkout=checkout,
                custom_command_id=command.id if command else None,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except EnvFileError as error:
            row.status = "error"
            row.last_error = str(error)
            row.worktree_id = resolved_worktree_id
            self._add(row)
            await self.session.commit()
            return row

        install_deps = True
        paths = self._paths_for(row, is_primary)
        gen = uuid4().hex
        declared_port: int | None = None
        if command is None:
            script = build_mise_run_script(paths, gen, install_deps=install_deps)
        else:
            install_command = None
            install_cwd = None
            if install_deps:
                install_entry = await self._registry_command(workspace, "install")
                if install_entry is not None:
                    install_command = install_entry.command
                    candidate_cwd = await self._entry_cwd(workspace, checkout, install_entry)
                    install_cwd = candidate_cwd if candidate_cwd != launch_cwd else None
            script = build_registry_script(
                paths,
                gen,
                command=command.command,
                install_command=install_command,
                install_cwd=install_cwd,
                serve=True,
            )
            declared_port = command.port or infer_port(command.command)

        try:
            await self.runtime.exec(
                workspace,
                script,
                cwd=launch_cwd,
                env=env or None,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError as error:
            raise WorkspaceServiceRuntimeError(str(error)) from error

        now = datetime.now(UTC)
        row.desired = True
        row.worktree_id = resolved_worktree_id
        row.status = "installing"
        row.started_at = now
        row.last_seen_at = now
        row.last_error = None
        row.exit_code = None
        if declared_port is not None:
            row.port = declared_port
        self._add(row)
        if is_primary:
            workspace.dev_autostart = True
            workspace.dev_worktree_id = resolved_worktree_id
            self._add(workspace)
        await self.session.commit()
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="service_start",
            target_type="workspace_service",
            target_id=row.id,
            metadata={
                "worktree_id": str(resolved_worktree_id) if resolved_worktree_id else None,
                "custom_command_id": str(command.id) if command else None,
            },
        )
        return row

    async def _kill(self, workspace: Workspace, row: WorkspaceService, is_primary: bool) -> None:
        paths = self._paths_for(row, is_primary)
        try:
            await self.runtime.exec(
                workspace,
                kill_script(paths, legacy_pkill=is_primary),
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError:
            pass

    async def _refresh_single(
        self, workspace: Workspace, row: WorkspaceService, is_primary: bool
    ) -> None:
        probe_map, target_ports = await self._probe_rows(
            workspace, [row], row if is_primary else None
        )
        await self._apply_probe(
            workspace, row, probe_map.get(str(row.id)), target_ports.get(row.id), frozenset()
        )

    async def _target_port(self, row: WorkspaceService) -> int | None:
        """The port a row's own command declares or infers, or `None` for the built-in mise
        service (which has no command at all) or a registry command with no port information —
        both fall back to the `COMMON_PREVIEW_PORTS` scan in `_apply_probe`, exactly like the
        pre-Epic-11 `DevServerService._resolved_port`."""
        command = await self._command_for(row)
        if command is None:
            return None
        return command.port or infer_port(command.command)

    async def _probe_rows(
        self,
        workspace: Workspace,
        rows: list[WorkspaceService],
        primary: WorkspaceService | None,
    ) -> tuple[dict[str, ProbeResult], dict[UUID, int | None]]:
        if not rows:
            return {}, {}
        target_ports = {row.id: await self._target_port(row) for row in rows}
        probe_rows = [
            ProbeRow(
                id=str(row.id),
                paths=self._paths_for(row, primary is not None and row.id == primary.id),
                port=target_ports[row.id],
            )
            for row in rows
        ]
        try:
            result = await self.runtime.exec(
                workspace,
                build_probe_script(probe_rows),
                cwd=workspace.workspace_path,
                timeout=self.settings.dev_server_command_timeout_seconds,
            )
        except DaytonaRuntimeError:
            return {}, target_ports
        return parse_probe_output(result.stdout or ""), target_ports

    async def _apply_probe(
        self,
        workspace: Workspace,
        row: WorkspaceService,
        probe: ProbeResult | None,
        target_port: int | None,
        exclude_ports: frozenset[int] | set[int],
    ) -> None:
        """Map the sandbox status file (+ port) to the row's reported status.

        Deliberately does **not** demote a `running` file status to `stopped` just because the
        wrapper's pid looks dead — a confirmed-open port is definitive on its own, matching the
        pre-Epic-11 dev server (which never checked pid liveness for a plain status read, only
        for `ensure_running`'s relaunch decision — see `_should_relaunch`, which is handed this
        same probe result).

        The raw sandbox status token is kept on the row as `_raw_status` (not a mapped column)
        so `DevServerService`'s facade can still distinguish `ready` from a bare `stopped` for
        `setup()`'s benefit, without Epic 11 needing a DB status value pre-Epic-11 tooling never
        used.
        """
        if probe is None:
            return
        now = datetime.now(UTC)
        status_file = probe.status
        row._raw_status = status_file  # noqa: SLF001 - deliberate non-column attribute
        if status_file in ("installing", "launching"):
            row.status = "installing"
            row.last_error = None
        elif status_file.startswith("exited:"):
            row.status = "exited"
            row.last_error = None
            try:
                row.exit_code = int(status_file.split(":", 1)[1])
            except ValueError:
                row.exit_code = None
        elif status_file in ("error", "no-dev-task", "no-config"):
            row.status = "error"
            row.last_error = status_file
        elif status_file == "running":
            if target_port and probe.port_open:
                row.status = "running"
                row.port = target_port
            elif target_port is None:
                # No declared/inferred port at all (the built-in mise service, or a registry
                # command with neither) — scan the common ports, exactly like the pre-Epic-11
                # dev server did on every status read.
                await self._discover_port(workspace, row, exclude_ports)
                if row.port is not None:
                    row.status = "running"
                else:
                    row.status = self._grace_status(row, probe, now)
            else:
                # A declared/inferred port exists but is not (yet) open — report progress, not
                # a random other open port that happens to belong to a different service.
                row.port = None
                row.status = self._grace_status(row, probe, now)
        else:
            row.status = "stopped"
        row.last_seen_at = now
        self._add(row)

    def _grace_status(
        self, row: WorkspaceService, probe: ProbeResult, now: datetime
    ) -> str:
        """Reports `starting` (not `stopped`) even when the pid looks dead — matching the
        pre-Epic-11 dev server, which only ever reported `running`/`starting` from a plain
        status read. `_should_relaunch` re-examines this same probe's `pid_alive` directly to
        decide whether to relaunch, so a dead pid still gets relaunched despite the reported
        status staying `starting`."""
        grace_elapsed = (
            row.started_at is not None
            and (now - row.started_at).total_seconds() > self.settings.service_start_grace_seconds
        )
        return "running" if (grace_elapsed and probe.pid_alive) else "starting"

    async def _should_relaunch(self, row: WorkspaceService, probe: ProbeResult | None) -> bool:
        """Whether `ensure_running` should relaunch this row. A separate decision from the
        reported status: `running`/`installing`/`error`/`exited` never relaunch; `starting`
        only relaunches once the pid is confirmed dead (otherwise it is just a slow boot);
        `stopped` still checks pid liveness (a stale status write must not double-launch) and
        the crash-loop backoff before relaunching.

        `probe is None` means the sandbox exec itself failed (a transient Daytona error), not
        that the process is dead — there is no evidence either way, so never relaunch on it;
        the next poll gets another chance to probe for real.
        """
        if not row.desired:
            return False
        if probe is None:
            return False
        if row.status in ("running", "installing", "error", "exited"):
            return False
        if row.status == "starting":
            return not probe.pid_alive
        if probe.pid_alive:
            return False
        if row.started_at is not None:
            elapsed = (datetime.now(UTC) - row.started_at).total_seconds()
            if elapsed < self.settings.service_relaunch_min_interval_seconds:
                return False
        return True

    async def _discover_port(
        self, workspace: Workspace, row: WorkspaceService, exclude_ports: set[int]
    ) -> None:
        candidates = [
            port
            for port in self.common_ports
            if port != self.settings.editor_port and port not in exclude_ports
        ]
        if not candidates:
            return
        open_ports = await probe_ports(self.runtime, workspace, candidates)
        for port in self.common_ports:
            if port in open_ports and port not in exclude_ports:
                row.port = port
                self._add(row)
                return

    async def _check_limit(self, workspace: Workspace, row: WorkspaceService) -> None:
        siblings = await self._active_sibling_rows(workspace, row.id)
        limit = self.settings.max_running_services_per_workspace
        if len(siblings) >= limit:
            raise WorkspaceServiceLimitError(
                f"Stop a service first ({len(siblings)}/{limit} running)."
            )

    async def _check_port_conflict(
        self, workspace: Workspace, row: WorkspaceService, port: int | None
    ) -> str | None:
        if port is None:
            return None
        if port == self.settings.editor_port:
            raise WorkspaceServicePortConflictError(
                f"Port {port} is reserved for the code editor."
            )
        siblings = await self._active_sibling_rows(workspace, row.id)
        for sibling in siblings:
            if sibling.port == port:
                name = await self._display_name(sibling)
                raise WorkspaceServicePortConflictError(
                    f"Port {port} is already used by {name}. Stop it first."
                )
        if await port_is_open(self.runtime, workspace, port):
            return f"Port {port} already has something listening on it."
        return None

    async def _display_name(self, row: WorkspaceService) -> str:
        command = await self._command_for(row)
        return command.name if command is not None else "the dev server"

    async def _reconcile_previews(
        self, workspace: Workspace, rows: list[WorkspaceService]
    ) -> None:
        preview_service = PreviewService(self.session, self.settings, self.runtime)
        for row in rows:
            if row.status == "running" and row.port is not None:
                await preview_service.upsert_for_service(
                    workspace, service_id=row.id, port=row.port, worktree_id=row.worktree_id
                )
            else:
                await preview_service.stop_for_service(workspace, row.id)

    async def _stop_previews(self, workspace: Workspace, row: WorkspaceService) -> None:
        preview_service = PreviewService(self.session, self.settings, self.runtime)
        await preview_service.stop_for_service(workspace, row.id)

    # --- lookups -------------------------------------------------------

    async def get_workspace_for(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        """Public wrapper so API layers can fetch the workspace object for response
        enrichment (`to_response_list`, `environment_from_primary`) without reaching into a
        private method."""
        return await self._get_workspace(workspace_id, user_id)

    def max_concurrent_services(self) -> int:
        """The configured concurrency cap (`_check_limit`'s own bound), for the API layer to
        surface in the aggregate response — never the number of *configured* rows, which can
        be more or fewer than the cap."""
        return self.settings.max_running_services_per_workspace

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        if not workspace.daytona_sandbox_id:
            raise WorkspaceServiceWorkspacePathError
        return workspace

    async def _get_row(self, workspace: Workspace, service_id: UUID) -> WorkspaceService:
        row = await self.session.get(WorkspaceService, service_id)
        if row is None or row.workspace_id != workspace.id:
            raise WorkspaceServiceNotFoundError
        return row

    async def _get_row_for_command(
        self, workspace: Workspace, command_id: UUID
    ) -> WorkspaceService | None:
        result = await self.session.execute(
            select(WorkspaceService).where(
                WorkspaceService.workspace_id == workspace.id,
                WorkspaceService.custom_command_id == command_id,
            )
        )
        return result.scalars().first()

    async def _get_builtin_row(self, workspace: Workspace) -> WorkspaceService | None:
        result = await self.session.execute(
            select(WorkspaceService).where(
                WorkspaceService.workspace_id == workspace.id,
                WorkspaceService.custom_command_id.is_(None),
            )
        )
        return result.scalars().first()

    async def _all_rows(self, workspace: Workspace) -> list[WorkspaceService]:
        result = await self.session.execute(
            select(WorkspaceService)
            .where(WorkspaceService.workspace_id == workspace.id)
            .order_by(WorkspaceService.created_at.asc())
        )
        return list(result.scalars().all())

    async def _active_sibling_rows(
        self, workspace: Workspace, exclude_id: UUID
    ) -> list[WorkspaceService]:
        result = await self.session.execute(
            select(WorkspaceService).where(
                WorkspaceService.workspace_id == workspace.id,
                WorkspaceService.id != exclude_id,
                WorkspaceService.status.in_(ACTIVE_STATUSES),
            )
        )
        return list(result.scalars().all())

    async def _command_for(self, row: WorkspaceService) -> CustomCommand | None:
        if row.custom_command_id is None:
            return None
        return await self.session.get(CustomCommand, row.custom_command_id)

    async def _registry_command(self, workspace: Workspace, role: str) -> CustomCommand | None:
        """The enabled registry command that owns a lifecycle role — unchanged from the
        pre-Epic-11 `DevServerService._registry_entry`: identity is `role`, not `kind`, so a
        `dev`-role command drives the primary service whether or not it has been marked
        `kind='service'` yet (existing workspaces, and any command created before this epic)."""
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

    async def _sync_definitions(self, workspace: Workspace) -> None:
        result = await self.session.execute(
            select(CustomCommand).where(
                CustomCommand.workspace_id == workspace.id,
                CustomCommand.kind == "service",
                CustomCommand.enabled.is_(True),
            )
        )
        service_commands = list(result.scalars().all())
        for command in service_commands:
            existing = await self._get_row_for_command(workspace, command.id)
            if existing is None:
                self._add(
                    WorkspaceService(
                        id=uuid4(),
                        workspace_id=workspace.id,
                        custom_command_id=command.id,
                        status="stopped",
                        desired=False,
                    )
                )
        builtin = await self._get_builtin_row(workspace)
        if service_commands:
            builtin_is_idle = (
                builtin is not None
                and not builtin.desired
                and builtin.status not in ACTIVE_STATUSES
            )
            if builtin_is_idle:
                if hasattr(self.session, "delete"):
                    await self.session.delete(builtin)
        elif builtin is None:
            self._add(
                WorkspaceService(
                    id=uuid4(),
                    workspace_id=workspace.id,
                    custom_command_id=None,
                    status="stopped",
                    desired=False,
                )
            )
        if hasattr(self.session, "commit"):
            await self.session.commit()

    def _add(self, obj: object) -> None:
        if hasattr(self.session, "add"):
            self.session.add(obj)

    # --- launch path helpers (mirrors DevServerService pre-Epic-11) ----

    def _paths_for(self, row: WorkspaceService, is_primary: bool) -> ServicePaths:
        return builtin_paths() if is_primary else service_paths(row.id)

    async def _entry_cwd(
        self, workspace: Workspace, checkout: str, command: CustomCommand | None
    ) -> str:
        return join_subpath(
            checkout,
            validate_relative_subpath(getattr(workspace, "app_root", None)),
            validate_relative_subpath(getattr(command, "cwd", None)) if command else None,
        )

    async def _ensure_runnable(
        self,
        workspace: Workspace,
        checkout: str,
        launch_cwd: str | None,
        *,
        require_mise: bool,
    ) -> str | None:
        """Returns an error reason ('no-config') to surface, or None when runnable. Raises
        `WorkspaceServiceNotClonedError` when the checkout itself is missing."""
        sandbox = await self.runtime.get_sandbox(workspace)
        if not await _is_cloned(sandbox, checkout):
            raise WorkspaceServiceNotClonedError
        if require_mise:
            candidates = [launch_cwd] if launch_cwd and launch_cwd != checkout else []
            candidates.append(checkout)
            for directory in candidates:
                if await _has_mise_config(sandbox, directory):
                    break
            else:
                return "no-config"
        return None


async def _has_mise_config(sandbox: object, repo_path: str) -> bool:
    for name in MISE_CONFIG_FILES:
        try:
            await sandbox.fs.get_file_info(f"{repo_path}/{name}")
        except Exception:
            continue
        return True
    return False


async def _is_cloned(sandbox: object, repo_path: str) -> bool:
    try:
        await sandbox.fs.get_file_info(f"{repo_path}/.git")
    except Exception:
        return False
    return True
