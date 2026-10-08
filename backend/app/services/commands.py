import asyncio
import shlex
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.lifecycle import lifecycle
from app.models import CommandRun, CustomCommand, Workspace
from app.services.agent_tools import DangerousCommandError
from app.services.audit import AuditLogService
from app.services.daytona_runtime import DaytonaRuntime, get_daytona_runtime
from app.services.env_templates import enigma_exports, substitute
from app.services.git_providers import redact_secrets
from app.services.jobs import (
    ENQUEUE_FAILED,
    RunDispatcher,
    RunQueueUnavailableError,
    get_run_dispatcher,
)
from app.services.notifications import NotificationService
from app.services.run_paths import RunPathError, resolve_run_cwd
from app.services.runtime import (
    EventBus,
    InMemoryEventBus,
    InMemoryRunRegistry,
    RunRegistry,
    event_bus,
    run_registry,
)
from app.services.workspace_env import build_template_context, redactable_env_values
from app.services.workspace_env import resolve as _resolve_env
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)

# New registry entries are appended with this gap so a later insert can be slotted between
# two commands without renumbering the whole workspace.
SORT_ORDER_STEP = 10


class CustomCommandNotFoundError(Exception):
    pass


class CommandRunNotFoundError(Exception):
    pass


class CommandConfirmationRequiredError(Exception):
    pass


class CommandWorkspacePathError(Exception):
    pass


class CustomCommandInvalidError(Exception):
    """A command payload is well-formed JSON but violates a domain rule -- e.g. marking a
    `task`-kind command as the entry point."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class CommandRuntime:
    """Command-run view over the process-local run state seam (see ``app.services.runtime``)."""

    def __init__(
        self, registry: RunRegistry | None = None, bus: EventBus | None = None
    ) -> None:
        self.registry = registry if registry is not None else InMemoryRunRegistry()
        self.bus = bus if bus is not None else InMemoryEventBus()

    @staticmethod
    def _topic(run_id: UUID) -> str:
        return f"command_run:{run_id}"

    def register_run(self, run_id: UUID, task: asyncio.Task) -> None:
        self.registry.register(run_id, task)

    def get_task(self, run_id: UUID) -> asyncio.Task | None:
        return self.registry.get_task(run_id)

    def subscribe(self, run_id: UUID) -> asyncio.Queue[dict]:
        return self.bus.subscribe(self._topic(run_id))

    def unsubscribe(self, run_id: UUID, queue: asyncio.Queue[dict]) -> None:
        self.bus.unsubscribe(self._topic(run_id), queue)

    async def publish(self, run_id: UUID, payload: dict) -> None:
        await self.bus.publish(self._topic(run_id), payload)

    async def wait_ready(self, run_id: UUID) -> bool:
        return await self.bus.wait_ready(self._topic(run_id))


command_runtime = CommandRuntime(run_registry, event_bus)


def command_status_frame(run: CommandRun) -> dict:
    """The terminal frame of a command-run stream."""
    frame = {"type": "status", "status": run.status, "exit_code": run.exit_code}
    if run.error_reason:
        frame["reason"] = run.error_reason
    return frame


class CommandService:
    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        runtime: CommandRuntime = command_runtime,
        daytona_runtime: DaytonaRuntime | None = None,
        dispatcher: RunDispatcher | None = None,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.settings = settings
        self.runtime = runtime
        self.daytona_runtime = daytona_runtime or get_daytona_runtime()
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> RunDispatcher:
        return self._dispatcher if self._dispatcher is not None else get_run_dispatcher()

    async def create_command(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
        command: str,
        description: str | None,
        icon: str | None,
        hotkey: str | None,
        requires_confirmation: bool,
        role: str = "custom",
        kind: str | None = None,
        cwd: str | None = None,
        port: int | None = None,
        enabled: bool = True,
        timeout_seconds: int | None = None,
        sort_order: int | None = None,
        health_path: str | None = None,
        is_entry_point: bool = False,
    ) -> CustomCommand:
        workspace = await self._get_workspace(workspace_id, user_id)
        # A `dev`-role command is, by default, a long-running service (Epic 11) — that is what
        # lets it appear in the multi-service preview at all. An explicit `kind` always wins.
        effective_kind = kind if kind is not None else ("service" if role == "dev" else "task")
        if is_entry_point and effective_kind != "service":
            raise CustomCommandInvalidError(
                "Only a long-running service command can be the entry point."
            )
        if is_entry_point:
            # Clear any existing entry point in this workspace first -- the partial unique
            # index (`uq_custom_commands_entry_point`) allows at most one `true` row, so
            # inserting a second one without this raises an unhandled IntegrityError.
            await self._clear_entry_point(workspace.id, exclude_command_id=None)
        custom_command = CustomCommand(
            workspace_id=workspace.id,
            name=name,
            command=command,
            description=description,
            icon=icon,
            hotkey=hotkey,
            requires_confirmation=requires_confirmation,
            role=role,
            kind=effective_kind,
            cwd=cwd,
            port=port,
            enabled=enabled,
            timeout_seconds=timeout_seconds,
            sort_order=(
                await self._next_sort_order(workspace.id) if sort_order is None else sort_order
            ),
            health_path=health_path,
            is_entry_point=is_entry_point,
        )
        self.session.add(custom_command)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(custom_command)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="custom_command_create",
            target_type="custom_command",
            target_id=custom_command.id,
            metadata={"name": custom_command.name},
        )
        logger.info(
            "custom_command_create",
            custom_command_id=str(custom_command.id),
            workspace_id=str(workspace.id),
            user_id=str(user_id),
        )
        return custom_command

    async def _clear_entry_point(
        self, workspace_id: UUID, *, exclude_command_id: UUID | None
    ) -> None:
        """Flip off `is_entry_point` on every other command in the workspace, in the same
        transaction as the caller's own insert/update -- never a separate commit, so a crash
        between the two can never leave two commands (or zero, then a stale one) marked."""
        stmt = update(CustomCommand).where(
            CustomCommand.workspace_id == workspace_id,
            CustomCommand.is_entry_point.is_(True),
        )
        if exclude_command_id is not None:
            stmt = stmt.where(CustomCommand.id != exclude_command_id)
        await self.session.execute(stmt.values(is_entry_point=False))

    async def list_commands(self, workspace_id: UUID, user_id: UUID) -> list[CustomCommand]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(CustomCommand)
            .where(CustomCommand.workspace_id == workspace.id)
            .order_by(CustomCommand.sort_order.asc(), CustomCommand.created_at.asc())
        )
        return list(result.scalars().all())

    async def update_command(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        values: dict,
    ) -> CustomCommand:
        custom_command = await self._get_command(workspace_id, command_id, user_id)
        was_runnable_service = custom_command.enabled and custom_command.kind == "service"
        if values.get("is_entry_point") is True:
            effective_kind = values.get("kind", custom_command.kind)
            if effective_kind != "service":
                raise CustomCommandInvalidError(
                    "Only a long-running service command can be the entry point."
                )
            # Clear before setting, in the same transaction -- same reasoning as create_command.
            await self._clear_entry_point(workspace_id, exclude_command_id=custom_command.id)
        for key, value in values.items():
            setattr(custom_command, key, value)
        self.session.add(custom_command)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(custom_command)
        # A service that was disabled, or demoted to a plain task, must not keep running
        # unnoticed — best-effort, a sandbox error here must not fail the edit itself.
        now_runnable_service = custom_command.enabled and custom_command.kind == "service"
        if was_runnable_service and not now_runnable_service:
            await self._stop_service_for_command(workspace_id, user_id, custom_command.id)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="custom_command_update",
            target_type="custom_command",
            target_id=custom_command.id,
            metadata={"name": custom_command.name},
        )
        logger.info(
            "custom_command_update",
            custom_command_id=str(custom_command.id),
            workspace_id=str(workspace_id),
            user_id=str(user_id),
        )
        return custom_command

    async def delete_command(self, workspace_id: UUID, command_id: UUID, user_id: UUID) -> None:
        custom_command = await self._get_command(workspace_id, command_id, user_id)
        command_name = custom_command.name
        # Stop a running service *before* the cascade delete removes its workspace_services
        # row along with the command, or there is nothing left to look up afterwards.
        if custom_command.kind == "service":
            await self._stop_service_for_command(workspace_id, user_id, custom_command.id)
        await self.session.delete(custom_command)
        await self.session.commit()
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="custom_command_delete",
            target_type="custom_command",
            target_id=command_id,
            metadata={"name": command_name},
        )
        logger.info(
            "custom_command_delete",
            custom_command_id=str(command_id),
            workspace_id=str(workspace_id),
            user_id=str(user_id),
        )

    async def import_commands(
        self,
        workspace_id: UUID,
        user_id: UUID,
        items: list[dict],
    ) -> list[CustomCommand]:
        """Bulk-create registry entries from detection candidates or a preset.

        Each entry is created exactly as a hand-authored one would be — same validation, same
        audit trail — so an imported command carries no special status afterwards.
        """
        workspace = await self._get_workspace(workspace_id, user_id)
        sort_order = await self._next_sort_order(workspace.id)
        created: list[CustomCommand] = []
        for item in items:
            item_role = item.get("role", "custom")
            item_kind = item.get("kind")
            if item_kind is None:
                item_kind = "service" if item_role == "dev" else "task"
            custom_command = CustomCommand(
                workspace_id=workspace.id,
                name=item["name"],
                command=item["command"],
                description=item.get("description"),
                icon=item.get("icon"),
                hotkey=item.get("hotkey"),
                requires_confirmation=bool(item.get("requires_confirmation", False)),
                role=item_role,
                kind=item_kind,
                cwd=item.get("cwd"),
                port=item.get("port"),
                enabled=True,
                timeout_seconds=item.get("timeout_seconds"),
                sort_order=sort_order,
            )
            sort_order += SORT_ORDER_STEP
            self.session.add(custom_command)
            created.append(custom_command)
        await self.session.flush()
        await self.session.commit()
        for custom_command in created:
            await self.session.refresh(custom_command)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="custom_command_import",
            target_type="workspace",
            target_id=workspace.id,
            metadata={"count": len(created), "names": [row.name for row in created][:20]},
        )
        logger.info(
            "custom_command_import",
            workspace_id=str(workspace.id),
            user_id=str(user_id),
            count=len(created),
        )
        return created

    async def reorder_commands(
        self,
        workspace_id: UUID,
        user_id: UUID,
        order: dict[UUID, int],
    ) -> list[CustomCommand]:
        """Bulk ``sort_order`` update. Ids outside this workspace are ignored, not an error."""
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(CustomCommand).where(CustomCommand.workspace_id == workspace.id)
        )
        rows = list(result.scalars().all())
        for row in rows:
            if row.id in order:
                row.sort_order = order[row.id]
                self.session.add(row)
        await self.session.flush()
        await self.session.commit()
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="custom_command_reorder",
            target_type="workspace",
            target_id=workspace.id,
            metadata={"count": len(order)},
        )
        return sorted(rows, key=lambda row: (row.sort_order, row.created_at))

    async def create_run(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        confirmed: bool,
        worktree_id: UUID | None = None,
    ) -> CommandRun:
        lifecycle.ensure_accepting_work()
        workspace = await self._get_workspace(workspace_id, user_id)
        custom_command = await self._get_command(workspace_id, command_id, user_id)
        if custom_command.requires_confirmation and not confirmed:
            raise CommandConfirmationRequiredError
        if not workspace.daytona_sandbox_id:
            raise CommandWorkspacePathError

        # Resolve up front so an unusable worktree or working directory fails the request with a
        # real status code instead of surfacing asynchronously as a run in status "error".
        try:
            _, relative_cwd, resolved_worktree_id, _ = await resolve_run_cwd(
                self.session,
                workspace,
                worktree_id=worktree_id,
                command_cwd=custom_command.cwd,
            )
        except RunPathError as error:
            raise CommandWorkspacePathError(str(error)) from error

        run = CommandRun(
            custom_command_id=custom_command.id,
            workspace_id=custom_command.workspace_id,
            user_id=user_id,
            worktree_id=resolved_worktree_id,
            cwd=relative_cwd or None,
            status="running",
            output="",
        )
        self.session.add(run)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(run)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=custom_command.workspace_id,
            action="command_run_start",
            target_type="command_run",
            target_id=run.id,
            metadata={
                "custom_command_id": custom_command.id,
                "name": custom_command.name,
                "worktree_id": str(resolved_worktree_id) if resolved_worktree_id else None,
                "cwd": relative_cwd or None,
            },
        )

        if self.settings.run_execution_mode == "queue":
            await self._enqueue_run(run)
        else:
            task = asyncio.create_task(self._execute_run(run.id, confirmed))
            self.runtime.register_run(run.id, task)
        logger.info(
            "custom_command_run_start",
            command_run_id=str(run.id),
            custom_command_id=str(custom_command.id),
            workspace_id=str(workspace_id),
            user_id=str(user_id),
        )
        return run

    async def _enqueue_run(self, run: CommandRun) -> None:
        run.enqueued_at = datetime.now(UTC)
        self.session.add(run)
        await self.session.commit()
        try:
            await self.dispatcher.enqueue_command_run(run.id)
        except Exception as error:
            await self.fail_active_run(
                run.id, ENQUEUE_FAILED, "The run could not be queued for execution."
            )
            raise RunQueueUnavailableError from error
        await self.session.refresh(run)

    async def claim_run(self, run_id: UUID, worker_id: str, attempt: int) -> str:
        """Atomically take ownership of a queued run (see ``AgentService.claim_run``)."""
        async with self.session_maker() as session:
            claimed = await session.execute(
                update(CommandRun)
                .where(
                    CommandRun.id == run_id,
                    CommandRun.status == "running",
                    CommandRun.claimed_at.is_(None),
                )
                .values(claimed_at=datetime.now(UTC), worker_id=worker_id, attempt=attempt)
                .returning(CommandRun.id)
            )
            if claimed.scalar_one_or_none() is not None:
                await session.commit()
                return "claimed"
            status = await session.scalar(
                select(CommandRun.status).where(CommandRun.id == run_id)
            )
            if status is None:
                return "missing"
            return "already_claimed" if status == "running" else "already_terminal"

    async def fail_active_run(self, run_id: UUID, reason: str, message: str) -> bool:
        """End a still-running run as ``error``/``reason``; a no-op if it already finished."""
        async with self.session_maker() as session:
            result = await session.execute(
                update(CommandRun)
                .where(CommandRun.id == run_id, CommandRun.status == "running")
                .values(
                    status="error",
                    error_reason=reason,
                    completed_at=datetime.now(UTC),
                    output=CommandRun.output + f"\n{message}\n",
                )
                .returning(CommandRun.id)
            )
            if result.scalar_one_or_none() is None:
                await session.rollback()
                return False
            await session.commit()
            run = await session.get(CommandRun, run_id)
            await self.runtime.publish(run_id, command_status_frame(run))
            return True

    async def list_runs(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
    ) -> list[CommandRun]:
        custom_command = await self._get_command(workspace_id, command_id, user_id)
        result = await self.session.execute(
            select(CommandRun)
            .where(CommandRun.custom_command_id == custom_command.id)
            .order_by(CommandRun.started_at.desc())
            .limit(25)
        )
        return list(result.scalars().all())

    async def get_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> CommandRun:
        await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(CommandRun).where(
                CommandRun.id == run_id,
                CommandRun.workspace_id == workspace_id,
                CommandRun.user_id == user_id,
            )
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise CommandRunNotFoundError
        return run

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _stop_service_for_command(
        self, workspace_id: UUID, user_id: UUID, command_id: UUID
    ) -> None:
        # Lazy import: workspace_services.py doesn't import commands.py, so this is not a
        # true cycle, but keeping the dependency import-time-local matches the pattern used
        # everywhere else a domain reaches into workspace_services.py for a side effect.
        from app.services.workspace_services import (
            WorkspaceServiceManager,
            WorkspaceServiceRuntimeError,
        )

        try:
            workspace = await self._get_workspace(workspace_id, user_id)
        except WorkspaceNotFoundError:
            return
        manager = WorkspaceServiceManager(self.session, self.settings, self.daytona_runtime)
        try:
            await manager.stop_for_command(workspace, command_id)
        except WorkspaceServiceRuntimeError:
            pass

    async def _get_command(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
    ) -> CustomCommand:
        await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(CustomCommand).where(
                CustomCommand.id == command_id,
                CustomCommand.workspace_id == workspace_id,
            )
        )
        custom_command = result.scalar_one_or_none()
        if custom_command is None:
            raise CustomCommandNotFoundError
        return custom_command

    async def _execute_run(self, run_id: UUID, confirmed: bool) -> None:
        async with self.session_maker() as session:
            run = await session.get(CommandRun, run_id)
            if run is None:
                return
            custom_command = await session.get(CustomCommand, run.custom_command_id)
            workspace = await session.get(Workspace, run.workspace_id)
            if custom_command is None or workspace is None:
                return

            # Populated once the env is loaded below; an exception before that point (e.g. an
            # invalid command) has nothing to redact yet.
            secrets: tuple[str, ...] = ()
            try:
                argv = validate_user_command(custom_command.command)
                command = " ".join(shlex.quote(value) for value in argv)
                env = await self._workspace_env(session, workspace.id)
                # Epic 13: command runs get the same strict `${ENIGMA_*}` substitution as a
                # service launch -- a resolution failure surfaces as a failed run (caught by
                # the `except Exception` below), never a silently empty value.
                template_context = await build_template_context(
                    session,
                    self.settings,
                    workspace,
                    branch=workspace.default_branch,
                    self_service_id=None,
                )
                env = {**enigma_exports(template_context), **env}
                env, _template_problems = substitute(env, template_context, strict=True)
                cwd, _, _, _ = await resolve_run_cwd(
                    session,
                    workspace,
                    worktree_id=run.worktree_id,
                    command_cwd=custom_command.cwd,
                )
                # Workspace env values are decrypted into the sandbox, so anything that echoes the
                # environment would otherwise persist plaintext secrets to CommandRun.output and
                # broadcast them over the run WebSocket. Enigma-derived exports (workspace id,
                # branch, sibling ports/URLs) are excluded -- none of them is a secret, and a
                # resolved preview URL would otherwise match both the `_URL` key rule and the
                # length rule.
                secrets = redactable_env_values(
                    env, exclude=set(enigma_exports(template_context).values())
                )

                async def on_output(content: str) -> None:
                    await self._append_output(session, run, redact_secrets(content, *secrets))

                exit_code = await self.daytona_runtime.stream_command(
                    workspace,
                    session_id=f"command-{run.id.hex}",
                    command=command,
                    cwd=cwd,
                    env=env,
                    timeout=self._run_timeout(custom_command),
                    on_stdout=on_output,
                    on_stderr=on_output,
                )
                status = "completed" if exit_code == 0 else "failed"
                await self._mark_finished(session, run, status, exit_code)
            except Exception as error:
                await self._append_output(
                    session, run, redact_secrets(f"{error}\n", *secrets)
                )
                await self._mark_finished(session, run, "error", None)

    def _run_timeout(self, custom_command: CustomCommand) -> int:
        """Per-command override, bounded by the platform maximum."""
        configured = getattr(custom_command, "timeout_seconds", None)
        default = self.settings.command_run_timeout_seconds
        return min(configured or default, self.settings.command_run_max_timeout_seconds)

    async def _next_sort_order(self, workspace_id: UUID) -> int:
        result = await self.session.execute(
            select(func.max(CustomCommand.sort_order)).where(
                CustomCommand.workspace_id == workspace_id
            )
        )
        current = result.scalar_one_or_none()
        return (current or 0) + SORT_ORDER_STEP

    async def _append_output(self, session: AsyncSession, run: CommandRun, content: str) -> None:
        if not content:
            return
        max_chars = self.settings.agent_max_command_output_chars
        previous = run.output or ""
        run.output = f"{previous}{content}"[:max_chars]
        # Publish exactly what was persisted, at its offset into ``run.output``: a stream can then
        # trim what it already sent and re-read from Postgres whatever the bus dropped.
        persisted = run.output[len(previous) :]
        if not persisted:
            return
        session.add(run)
        await session.flush()
        await session.commit()
        await session.refresh(run)
        await self.runtime.publish(
            run.id, {"type": "output", "content": persisted, "offset": len(previous)}
        )

    async def _mark_finished(
        self,
        session: AsyncSession,
        run: CommandRun,
        status: str,
        exit_code: int | None,
    ) -> None:
        run.status = status
        run.exit_code = exit_code
        run.completed_at = datetime.now(UTC)
        session.add(run)
        await session.flush()
        await session.commit()
        await session.refresh(run)
        await self.runtime.publish(run.id, command_status_frame(run))
        logger.info(
            "custom_command_run_finish",
            command_run_id=str(run.id),
            custom_command_id=str(run.custom_command_id),
            workspace_id=str(run.workspace_id),
            user_id=str(run.user_id),
            status=run.status,
            exit_code=run.exit_code,
        )
        if not hasattr(session, "get"):
            return
        custom_command = await session.get(CustomCommand, run.custom_command_id)
        command_name = custom_command.name if custom_command is not None else "Custom command"
        notification_type = (
            "command_run_completed"
            if run.status == "completed"
            else "command_run_failed"
        )
        title = (
            "Command completed"
            if run.status == "completed"
            else "Command needs attention"
        )
        message = (
            f"{command_name} completed successfully."
            if run.status == "completed"
            else f"{command_name} finished with status {run.status}."
        )
        await NotificationService(session).create(
            user_id=run.user_id,
            workspace_id=run.workspace_id,
            notification_type=notification_type,
            title=title,
            message=message,
        )
        await AuditLogService(session).record(
            user_id=run.user_id,
            workspace_id=run.workspace_id,
            action="command_run_finish",
            target_type="command_run",
            target_id=run.id,
            metadata={
                "custom_command_id": run.custom_command_id,
                "name": command_name,
                "status": run.status,
                "exit_code": run.exit_code,
            },
        )

    async def _workspace_env(
        self,
        session: AsyncSession,
        workspace_id: UUID,
    ) -> dict[str, str]:
        return await load_workspace_env(session, self.settings, workspace_id)


async def load_workspace_env(
    session: AsyncSession,
    settings: Settings,
    workspace_id: UUID,
) -> dict[str, str]:
    """Back-compat shim -- ``app.services.workspace_env.resolve`` is now the single decrypt
    point."""
    return await _resolve_env(session, settings, workspace_id)


def validate_user_command(command: str) -> list[str]:
    """Build the argv for an **owner-authored** registry command.

    Unlike ``WorkspaceTools._validate_command`` — which constrains an *agent's* command choice,
    because that is model output — this applies no denylist. The workspace owner already has an
    unrestricted interactive PTY into the very same sandbox (``services/terminal.py``, whose
    ``_get_workspace`` performs the identical owner-only check as ``CommandService``), so a
    denylist here protects nothing while making ordinary commands such as ``cd api && pnpm dev``
    unusable without ticking a confirmation box.

    ``requires_confirmation`` therefore stays a pure *user intent* flag ("ask me first" before
    something destructive), never an authorisation mechanism.

    NOTE: this reasoning holds only while command CRUD and runs are owner-only. The frontend
    already models ``command.run`` / ``command.manage`` roles that the backend does not honour;
    if those are ever wired up, this policy must be revisited in the same change.
    """
    candidate = command.strip()
    if not candidate:
        raise DangerousCommandError("Command cannot be empty")
    return ["sh", "-lc", candidate]
