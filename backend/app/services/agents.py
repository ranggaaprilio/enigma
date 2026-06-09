import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.models import AgentEvent, AgentRun, Workspace
from app.services.agent_providers import AgentProvider, MockAgentProvider, OpenAIResponsesProvider
from app.services.agent_tools import WorkspaceTools
from app.services.audit import AuditLogService
from app.services.daytona_runtime import get_daytona_runtime
from app.services.notifications import NotificationService
from app.services.workspaces import WorkspaceNotFoundError

ACTIVE_RUN_STATUSES = ("thinking", "executing")


class AgentRunAlreadyActiveError(Exception):
    pass


class AgentRunNotFoundError(Exception):
    pass


class AgentRuntime:
    def __init__(self) -> None:
        self.stop_events: dict[UUID, asyncio.Event] = {}
        self.subscribers: dict[UUID, set[asyncio.Queue[dict]]] = defaultdict(set)
        self.tasks: dict[UUID, asyncio.Task] = {}

    def register_run(self, run_id: UUID, task: asyncio.Task, stop_event: asyncio.Event) -> None:
        self.tasks[run_id] = task
        self.stop_events[run_id] = stop_event
        task.add_done_callback(lambda _task: self.forget_run(run_id))

    def forget_run(self, run_id: UUID) -> None:
        self.tasks.pop(run_id, None)
        self.stop_events.pop(run_id, None)

    def request_stop(self, run_id: UUID) -> None:
        if stop_event := self.stop_events.get(run_id):
            stop_event.set()
        if task := self.tasks.get(run_id):
            task.cancel()

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
        "metadata": event.event_metadata,
        "created_at": event.created_at.isoformat(),
    }


def provider_from_settings(settings: Settings) -> AgentProvider:
    provider = settings.agent_provider.lower()
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

    async def create_run(self, workspace_id: UUID, user_id: UUID, prompt: str) -> AgentRun:
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
        task = asyncio.create_task(self._execute_run(run.id, stop_event))
        self.runtime.register_run(run.id, task, stop_event)
        return run

    async def list_runs(self, workspace_id: UUID, user_id: UUID) -> list[AgentRun]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(AgentRun)
            .where(AgentRun.workspace_id == workspace.id, AgentRun.user_id == user_id)
            .order_by(AgentRun.started_at.desc())
            .limit(25)
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
        self.runtime.request_stop(run.id)
        return run

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _execute_run(self, run_id: UUID, stop_event: asyncio.Event) -> None:
        async with self.session_maker() as session:
            try:
                run = await session.get(AgentRun, run_id)
                if run is None:
                    return
                workspace = await session.get(Workspace, run.workspace_id)
                if workspace is None:
                    return

                provider = provider_from_settings(self.settings)
                tools = WorkspaceTools(
                    workspace=workspace,
                    runtime=get_daytona_runtime(),
                    command_timeout_seconds=self.settings.agent_command_timeout_seconds,
                    max_command_output_chars=self.settings.agent_max_command_output_chars,
                )

                async for provider_event in provider.stream_response(run.prompt, tools, stop_event):
                    if stop_event.is_set():
                        break
                    if provider_event.event_type == "status":
                        status_value = provider_event.metadata.get("status")
                        if status_value in ACTIVE_RUN_STATUSES:
                            run.status = status_value
                            session.add(run)
                    if tokens_used := provider_event.metadata.get("tokens_used"):
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
                if run.status in ACTIVE_RUN_STATUSES:
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
                    await self._record_run_finished(session, run)
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
        event = AgentEvent(
            agent_run_id=run_id,
            event_type=event_type,
            content=content,
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

    async def _record_run_finished(self, session: AsyncSession, run: AgentRun) -> None:
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
