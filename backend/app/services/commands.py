import asyncio
import os
import shlex
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import CommandRun, CustomCommand, Workspace
from app.services.agent_tools import DANGEROUS_COMMANDS, DANGEROUS_TOKENS, DangerousCommandError
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)

PRACTICAL_CONFIRMED_COMMANDS = {"git", "npm", "pnpm", "uv", "yarn"}


class CustomCommandNotFoundError(Exception):
    pass


class CommandRunNotFoundError(Exception):
    pass


class CommandConfirmationRequiredError(Exception):
    pass


class CommandWorkspacePathError(Exception):
    pass


class CommandRuntime:
    def __init__(self) -> None:
        self.subscribers: dict[UUID, set[asyncio.Queue[dict]]] = defaultdict(set)
        self.tasks: dict[UUID, asyncio.Task] = {}

    def register_run(self, run_id: UUID, task: asyncio.Task) -> None:
        self.tasks[run_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(run_id, None))

    def subscribe(self, run_id: UUID) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self.subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: UUID, queue: asyncio.Queue[dict]) -> None:
        queues = self.subscribers.get(run_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self.subscribers.pop(run_id, None)

    async def publish(self, run_id: UUID, payload: dict) -> None:
        for queue in list(self.subscribers.get(run_id, set())):
            await queue.put(payload)


command_runtime = CommandRuntime()


class CommandService:
    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        runtime: CommandRuntime = command_runtime,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.settings = settings
        self.runtime = runtime

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
    ) -> CustomCommand:
        workspace = await self._get_workspace(workspace_id, user_id)
        custom_command = CustomCommand(
            workspace_id=workspace.id,
            name=name,
            command=command,
            description=description,
            icon=icon,
            hotkey=hotkey,
            requires_confirmation=requires_confirmation,
        )
        self.session.add(custom_command)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(custom_command)
        logger.info(
            "custom_command_create",
            custom_command_id=str(custom_command.id),
            workspace_id=str(workspace.id),
            user_id=str(user_id),
        )
        return custom_command

    async def list_commands(self, workspace_id: UUID, user_id: UUID) -> list[CustomCommand]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(CustomCommand)
            .where(CustomCommand.workspace_id == workspace.id)
            .order_by(CustomCommand.created_at.asc())
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
        for key, value in values.items():
            setattr(custom_command, key, value)
        self.session.add(custom_command)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(custom_command)
        logger.info(
            "custom_command_update",
            custom_command_id=str(custom_command.id),
            workspace_id=str(workspace_id),
            user_id=str(user_id),
        )
        return custom_command

    async def delete_command(self, workspace_id: UUID, command_id: UUID, user_id: UUID) -> None:
        custom_command = await self._get_command(workspace_id, command_id, user_id)
        await self.session.delete(custom_command)
        await self.session.commit()
        logger.info(
            "custom_command_delete",
            custom_command_id=str(command_id),
            workspace_id=str(workspace_id),
            user_id=str(user_id),
        )

    async def create_run(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        confirmed: bool,
    ) -> CommandRun:
        custom_command = await self._get_command(workspace_id, command_id, user_id)
        if custom_command.requires_confirmation and not confirmed:
            raise CommandConfirmationRequiredError

        run = CommandRun(
            custom_command_id=custom_command.id,
            workspace_id=custom_command.workspace_id,
            user_id=user_id,
            status="running",
            output="",
        )
        self.session.add(run)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(run)

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

            try:
                workspace_path = self._workspace_path(workspace)
                argv = validate_custom_command(
                    custom_command.command,
                    requires_confirmation=custom_command.requires_confirmation,
                    confirmed=confirmed,
                )
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=workspace_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=os.environ.copy(),
                )
                try:
                    exit_code = await asyncio.wait_for(
                        self._stream_process_output(session, run, process),
                        timeout=self.settings.agent_command_timeout_seconds,
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise RuntimeError("Command timed out") from None
                status = "completed" if exit_code == 0 else "failed"
                await self._mark_finished(session, run, status, exit_code)
            except Exception as error:
                await self._append_output(session, run, f"{error}\n")
                await self._mark_finished(session, run, "error", None)

    async def _stream_process_output(
        self,
        session: AsyncSession,
        run: CommandRun,
        process: asyncio.subprocess.Process,
    ) -> int:
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        async def read_stream(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while chunk := await stream.read(8192):
                await queue.put(chunk)

        stdout_task = asyncio.create_task(read_stream(process.stdout))
        stderr_task = asyncio.create_task(read_stream(process.stderr))
        waiter = asyncio.create_task(process.wait())
        try:
            while True:
                if waiter.done() and stdout_task.done() and stderr_task.done() and queue.empty():
                    break
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                text = chunk.decode("utf-8", errors="replace")
                await self._append_output(session, run, text)
            return process.returncode or 0
        finally:
            stdout_task.cancel()
            stderr_task.cancel()

    async def _append_output(self, session: AsyncSession, run: CommandRun, content: str) -> None:
        if not content:
            return
        max_chars = self.settings.agent_max_command_output_chars
        next_output = f"{run.output or ''}{content}"
        run.output = next_output[:max_chars]
        session.add(run)
        await session.flush()
        await session.commit()
        await session.refresh(run)
        await self.runtime.publish(run.id, {"type": "output", "content": content})

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
        await self.runtime.publish(
            run.id,
            {"type": "status", "status": run.status, "exit_code": run.exit_code},
        )
        logger.info(
            "custom_command_run_finish",
            command_run_id=str(run.id),
            custom_command_id=str(run.custom_command_id),
            workspace_id=str(run.workspace_id),
            user_id=str(run.user_id),
            status=run.status,
            exit_code=run.exit_code,
        )

    def _workspace_path(self, workspace: Workspace) -> Path:
        if workspace.local_path is None:
            raise CommandWorkspacePathError("Workspace directory is not ready")
        root = self.settings.workspace_root.resolve()
        path = Path(workspace.local_path).resolve()
        if path == root or root not in path.parents or not path.is_dir():
            raise CommandWorkspacePathError("Workspace directory is not ready")
        return path


def validate_custom_command(
    command: str,
    *,
    requires_confirmation: bool,
    confirmed: bool,
) -> list[str]:
    if any(token in command for token in DANGEROUS_TOKENS):
        raise DangerousCommandError("Shell control operators are not allowed")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise DangerousCommandError("Command could not be parsed") from error
    if not argv:
        raise DangerousCommandError("Command cannot be empty")
    executable = argv[0]
    if "/" in executable:
        raise DangerousCommandError("Command is not allowed")
    if executable in DANGEROUS_COMMANDS:
        if executable in PRACTICAL_CONFIRMED_COMMANDS and requires_confirmation and confirmed:
            return argv
        raise DangerousCommandError("Command is not allowed")
    return argv
