import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.models import AgentEvent, AgentFileChange, AgentRun, Workspace
from app.services.agent_providers import AgentProvider, MockAgentProvider, OpenAIResponsesProvider
from app.services.agent_tools import WorkspaceTools
from app.services.audit import AuditLogService
from app.services.daytona_runtime import get_daytona_runtime
from app.services.notifications import NotificationService
from app.services.repositories import RepositoryService
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher

ACTIVE_RUN_STATUSES = ("thinking", "executing")


class AgentRunAlreadyActiveError(Exception):
    pass


class AgentRunNotFoundError(Exception):
    pass


class AgentRunNotInWorkspaceError(Exception):
    pass


class AgentConfigurationError(Exception):
    pass


class AgentRuntime:
    def __init__(self) -> None:
        self.stop_events: dict[UUID, asyncio.Event] = {}
        self.subscribers: dict[UUID, set[asyncio.Queue[dict]]] = defaultdict(set)
        self.tasks: dict[UUID, asyncio.Task] = {}
        self.providers: dict[UUID, AgentProvider] = {}

    def register_run(self, run_id: UUID, task: asyncio.Task, stop_event: asyncio.Event) -> None:
        self.tasks[run_id] = task
        self.stop_events[run_id] = stop_event
        task.add_done_callback(lambda _task: self.forget_run(run_id))

    def forget_run(self, run_id: UUID) -> None:
        self.tasks.pop(run_id, None)
        self.stop_events.pop(run_id, None)
        self.providers.pop(run_id, None)

    def register_provider(self, run_id: UUID, provider: AgentProvider) -> None:
        self.providers[run_id] = provider

    def request_stop(self, run_id: UUID) -> None:
        if stop_event := self.stop_events.get(run_id):
            stop_event.set()
        if provider := self.providers.get(run_id):
            provider.pause()

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


agent_runtime = AgentRuntime()


def event_payload(event: AgentEvent) -> dict:
    return {
        "id": str(event.id),
        "agent_run_id": str(event.agent_run_id),
        "event_type": event.event_type,
        "content": event.content,
        "tool_call_id": event.tool_call_id,
        "metadata": event.event_metadata,
        "created_at": event.created_at.isoformat(),
    }


def provider_from_settings(settings: Settings) -> AgentProvider:
    provider = settings.agent_provider.lower()
    if provider == "openhands":
        from app.services.openhands_provider import (
            OpenHandsAgentProvider,
            OpenHandsConfigurationError,
        )

        try:
            return OpenHandsAgentProvider(
                base_url=settings.openhands_llm_base_url,
                api_key=settings.openhands_llm_api_key,
                model=settings.openhands_llm_model,
                max_iterations=settings.openhands_max_iterations,
            )
        except OpenHandsConfigurationError as error:
            raise AgentConfigurationError(str(error)) from error
    if provider == "openai" or (provider == "auto" and settings.openai_api_key):
        if settings.openai_api_key:
            return OpenAIResponsesProvider(settings.openai_api_key, settings.openai_model)
    return MockAgentProvider()


class AgentService:
    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        runtime: AgentRuntime = agent_runtime,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.settings = settings
        self.runtime = runtime

    async def create_run(
        self,
        workspace_id: UUID,
        user_id: UUID,
        prompt: str,
        working_dir: str | None = None,
        worktree_id: UUID | None = None,
    ) -> AgentRun:
        workspace = await self._get_workspace(workspace_id, user_id)
        active = await self.session.execute(
            select(AgentRun.id).where(
                AgentRun.workspace_id == workspace.id,
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        if active.scalar_one_or_none() is not None:
            raise AgentRunAlreadyActiveError

        provider = provider_from_settings(self.settings)
        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user_id,
            worktree_id=worktree_id,
            prompt=prompt,
            status="thinking",
            provider=provider.provider_name,
            model=provider.model_name,
        )
        self.session.add(run)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(run)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="agent_run_start",
            target_type="agent_run",
            target_id=run.id,
            metadata={"provider": run.provider, "model": run.model},
        )

        stop_event = asyncio.Event()
        task = asyncio.create_task(self._execute_run(run.id, stop_event, working_dir))
        self.runtime.register_run(run.id, task, stop_event)
        return run

    async def list_runs(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AgentRun]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(AgentRun)
            .where(AgentRun.workspace_id == workspace.id, AgentRun.user_id == user_id)
            .order_by(AgentRun.started_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(AgentRun)
            .options(selectinload(AgentRun.events))
            .where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id,
                AgentRun.user_id == user_id,
            )
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise AgentRunNotFoundError
        run.events.sort(key=lambda event: event.created_at)
        return run

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
    ) -> list[AgentEvent]:
        run = await self.get_run(workspace_id, run_id, user_id)
        return list(run.events)

    async def stop_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        run = await self.get_run(workspace_id, run_id, user_id)
        if run.status in ACTIVE_RUN_STATUSES:
            self.runtime.request_stop(run.id)
            run.status = "stopped"
            run.completed_at = datetime.now(UTC)
            self.session.add(run)
            await self.session.flush()
            event = AgentEvent(
                agent_run_id=run.id,
                event_type="status",
                content="stopped",
                event_metadata={"status": "stopped"},
            )
            self.session.add(event)
            await self.session.flush()
            await self.session.commit()
            await self.session.refresh(event)
            await self.session.refresh(run)
            await self.runtime.publish(run.id, event_payload(event))
            await self._record_run_finished(self.session, run)
        return run

    async def create_file_change(
        self,
        run_id: UUID,
        path: str,
        status: str,
        tool_call_id: str | None = None,
        session: AsyncSession | None = None,
    ) -> AgentFileChange:
        session = session or self.session
        file_change = AgentFileChange(
            agent_run_id=run_id,
            path=path,
            status=status,
            tool_call_id=tool_call_id,
        )
        session.add(file_change)
        await session.flush()
        await session.commit()
        await session.refresh(file_change)
        return file_change

    async def get_file_changes_for_run(self, run_id: UUID) -> list[AgentFileChange]:
        result = await self.session.execute(
            select(AgentFileChange)
            .where(AgentFileChange.agent_run_id == run_id)
            .order_by(AgentFileChange.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_file_changes_for_run_and_tool(
        self,
        run_id: UUID,
        tool_call_id: str,
    ) -> list[AgentFileChange]:
        result = await self.session.execute(
            select(AgentFileChange)
            .where(
                AgentFileChange.agent_run_id == run_id,
                AgentFileChange.tool_call_id == tool_call_id,
            )
            .order_by(AgentFileChange.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_file_changes_for_run_capped(
        self,
        run_id: UUID,
        limit: int = 50,
    ) -> tuple[list[AgentFileChange], int, bool]:
        changes = await self.get_file_changes_for_run(run_id)
        total = len(changes)
        return changes[:limit], total, total > limit

    async def list_file_changes(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
        limit: int = 50,
    ) -> tuple[list[AgentFileChange], int, bool]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(AgentRun).where(AgentRun.id == run_id)
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise AgentRunNotFoundError
        if run.workspace_id != workspace.id:
            raise AgentRunNotInWorkspaceError
        return await self.get_file_changes_for_run_capped(run_id, limit)

    @staticmethod
    def _map_git_status_to_badge(status: str) -> str:
        status_lower = status.lower()
        if status_lower in ("added", "a", "untracked", "?"):
            return "created"
        if status_lower in ("modified", "m", "updated", "updated but unmerged", "u"):
            return "modified"
        if status_lower in ("deleted", "d", "removed"):
            return "deleted"
        if status_lower in ("renamed", "r", "copied", "c"):
            return "renamed"
        return "modified"

    async def _capture_baseline(
        self,
        workspace_id: UUID,
        user_id: UUID,
        session: AsyncSession | None = None,
    ) -> list:
        session = session or self.session
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return []
        cipher = WorkspaceSecretCipher(self.settings.workspace_env_encryption_key)
        runtime = get_daytona_runtime()
        repo_service = RepositoryService(session, cipher, runtime)
        try:
            git_status = await repo_service.status(workspace_id, user_id)
        except Exception:
            return []
        if not git_status.cloned:
            return []
        return git_status.files

    async def _compute_file_changes(
        self,
        workspace_id: UUID,
        baseline: list,
        user_id: UUID,
        run_id: UUID,
        session: AsyncSession | None = None,
    ) -> list[AgentFileChange]:
        session = session or self.session
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return []
        cipher = WorkspaceSecretCipher(self.settings.workspace_env_encryption_key)
        runtime = get_daytona_runtime()
        repo_service = RepositoryService(session, cipher, runtime)
        try:
            current_status = await repo_service.status(workspace_id, user_id)
        except Exception:
            return []
        if not current_status.cloned:
            return []
        baseline_dict = {f.path: f.status for f in baseline}
        changes: list[AgentFileChange] = []
        for file in current_status.files:
            if file.path not in baseline_dict or baseline_dict[file.path] != file.status:
                badge = self._map_git_status_to_badge(file.status)
                changes.append(
                    AgentFileChange(
                        agent_run_id=run_id,
                        path=file.path,
                        status=badge,
                        tool_call_id=None,
                    )
                )
        for change in changes:
            session.add(change)
        if changes:
            await session.flush()
            await session.commit()
        return changes

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _execute_run(
        self,
        run_id: UUID,
        stop_event: asyncio.Event,
        working_dir: str | None = None,
    ) -> None:
        async with self.session_maker() as session:
            try:
                run = await session.get(AgentRun, run_id)
                if run is None:
                    return
                workspace = await session.get(Workspace, run.workspace_id)
                if workspace is None:
                    return

                provider = provider_from_settings(self.settings)
                self.runtime.register_provider(run.id, provider)
                terminal_event_received = False
                tools = WorkspaceTools(
                    workspace=workspace,
                    runtime=get_daytona_runtime(),
                    command_timeout_seconds=self.settings.agent_command_timeout_seconds,
                    max_command_output_chars=self.settings.agent_max_command_output_chars,
                    working_dir=working_dir,
                    allowlist_enabled=self.settings.agent_command_allowlist_enabled,
                    allowed_commands=frozenset(self.settings.agent_command_allowlist),
                )

                baseline = await self._capture_baseline(
                    workspace.id, workspace.owner_id, session
                )

                async for provider_event in provider.stream_response(run.prompt, tools, stop_event):
                    if stop_event.is_set():
                        break
                    if provider_event.event_type == "status":
                        status_value = provider_event.metadata.get("status")
                        if status_value in ACTIVE_RUN_STATUSES + ("completed", "error", "stopped"):
                            run.status = status_value
                            if status_value not in ACTIVE_RUN_STATUSES:
                                run.completed_at = datetime.now(UTC)
                                terminal_event_received = True
                            session.add(run)
                    if (tokens_used := provider_event.metadata.get("tokens_used")) is not None:
                        run.tokens_used = int(tokens_used)
                        session.add(run)
                    await self._persist_event(
                        session,
                        run.id,
                        provider_event.event_type,
                        provider_event.content,
                        provider_event.metadata,
                    )

                await session.refresh(run)
                if terminal_event_received:
                    await self._record_run_finished(session, run, baseline)
                elif run.status in ACTIVE_RUN_STATUSES:
                    run.status = "stopped" if stop_event.is_set() else "completed"
                    run.completed_at = datetime.now(UTC)
                    session.add(run)
                    await session.flush()
                    await self._persist_event(
                        session,
                        run.id,
                        "status",
                        run.status,
                        {"status": run.status},
                    )
                    await self._record_run_finished(session, run, baseline)
            except asyncio.CancelledError:
                await self._mark_stopped(run_id)
            except Exception as error:
                await self._mark_error(run_id, str(error))

    async def _persist_event(
        self,
        session: AsyncSession,
        run_id: UUID,
        event_type: str,
        content: str,
        metadata: dict,
    ) -> AgentEvent:
        raw_tool_call_id = metadata.get("tool_call_id")
        tool_call_id = str(raw_tool_call_id) if raw_tool_call_id is not None else None
        event = AgentEvent(
            agent_run_id=run_id,
            event_type=event_type,
            content=content,
            tool_call_id=tool_call_id,
            event_metadata=metadata,
        )
        session.add(event)
        await session.flush()
        await session.commit()
        await session.refresh(event)
        await self.runtime.publish(run_id, event_payload(event))
        return event

    async def _mark_stopped(self, run_id: UUID) -> None:
        async with self.session_maker() as session:
            run = await session.get(AgentRun, run_id)
            if run is None or run.status not in ACTIVE_RUN_STATUSES:
                return
            run.status = "stopped"
            run.completed_at = datetime.now(UTC)
            session.add(run)
            await session.flush()
            await self._persist_event(session, run.id, "status", "stopped", {"status": "stopped"})
            await session.commit()
            await self._record_run_finished(session, run)

    async def _mark_error(self, run_id: UUID, message: str) -> None:
        async with self.session_maker() as session:
            run = await session.get(AgentRun, run_id)
            if run is None:
                return
            run.status = "error"
            run.error_message = message
            run.completed_at = datetime.now(UTC)
            session.add(run)
            await session.flush()
            await self._persist_event(session, run.id, "error", message, {})
            await session.commit()
            await self._record_run_finished(session, run)

    async def _record_run_finished(
        self,
        session: AsyncSession,
        run: AgentRun,
        baseline: list | None = None,
    ) -> None:
        if baseline is not None:
            await self._compute_file_changes(
                run.workspace_id,
                baseline,
                run.user_id,
                run.id,
                session,
            )
        notification_type = (
            "agent_run_completed" if run.status == "completed" else "agent_run_failed"
        )
        title = "Agent run completed" if run.status == "completed" else "Agent run needs attention"
        message = (
            "The agent run completed successfully."
            if run.status == "completed"
            else f"The agent run finished with status {run.status}."
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
            action="agent_run_finish",
            target_type="agent_run",
            target_id=run.id,
            metadata={"status": run.status, "tokens_used": run.tokens_used},
        )
