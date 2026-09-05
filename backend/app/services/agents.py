import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.pricing import estimate_cost
from app.models import AgentEvent, AgentFileChange, AgentRun, AgentSession, Workspace
from app.services.access import require_workspace_access
from app.services.agent_providers import AgentProvider, MockAgentProvider, OpenAIResponsesProvider
from app.services.agent_tools import WorkspaceTools
from app.services.audit import AuditLogService
from app.services.budgets import BudgetService
from app.services.daytona_runtime import get_daytona_runtime
from app.services.notifications import NotificationService
from app.services.repositories import RepositoryService
from app.services.workspaces import WorkspaceSecretCipher

ACTIVE_RUN_STATUSES = ("thinking", "executing")
SESSION_TITLE_MAX_CHARS = 80


class AgentRunAlreadyActiveError(Exception):
    pass


class AgentRunNotFoundError(Exception):
    pass


class AgentRunNotInWorkspaceError(Exception):
    pass


class AgentConfigurationError(Exception):
    pass


class AgentSessionNotFoundError(Exception):
    pass


class AgentSessionArchivedError(Exception):
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
        session_id: UUID | None = None,
    ) -> AgentRun:
        workspace = await self._get_workspace(workspace_id, user_id, require_drive=True)
        active = await self.session.execute(
            select(AgentRun.id).where(
                AgentRun.workspace_id == workspace.id,
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        if active.scalar_one_or_none() is not None:
            raise AgentRunAlreadyActiveError

        # Block/warn before spending: an enforcing budget over its limit stops the run here.
        await BudgetService(self.session).assert_within_budget(workspace, user_id)

        provider = provider_from_settings(self.settings)
        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user_id,
            worktree_id=worktree_id,
            session_id=session_id,
            prompt=prompt,
            status="thinking",
            provider=provider.provider_name,
            model=provider.model_name,
        )
        self.session.add(run)
        await self.session.flush()

        if session_id is not None:
            agent_session = await self.session.get(AgentSession, session_id)
            if agent_session is not None:
                # Auto-title from the first turn's prompt, and bump updated_at so the session
                # sorts as recently active (onupdate only fires when a column actually changes).
                if not agent_session.title:
                    agent_session.title = prompt[:SESSION_TITLE_MAX_CHARS]
                agent_session.updated_at = datetime.now(UTC)
                self.session.add(agent_session)

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

    # ------------------------------------------------------------------
    # Sessions (multi-turn conversations grouping runs)
    # ------------------------------------------------------------------

    async def _get_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        *,
        require_drive: bool = False,
    ) -> AgentSession:
        await self._get_workspace(workspace_id, user_id, require_drive=require_drive)
        result = await self.session.execute(
            select(AgentSession).where(
                AgentSession.id == session_id,
                AgentSession.workspace_id == workspace_id,
            )
        )
        agent_session = result.scalar_one_or_none()
        if agent_session is None:
            raise AgentSessionNotFoundError
        return agent_session

    async def create_session(
        self,
        workspace_id: UUID,
        user_id: UUID,
        title: str | None = None,
    ) -> AgentSession:
        workspace = await self._get_workspace(workspace_id, user_id, require_drive=True)
        agent_session = AgentSession(
            workspace_id=workspace.id,
            user_id=user_id,
            title=title,
            status="active",
        )
        self.session.add(agent_session)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(agent_session)
        return agent_session

    async def list_sessions(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> list[AgentSession]:
        await self._get_workspace(workspace_id, user_id)
        stmt = select(AgentSession).where(AgentSession.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(AgentSession.status == status)
        stmt = stmt.order_by(AgentSession.updated_at.desc()).offset(offset).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_session_detail(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
    ) -> tuple[AgentSession, list[AgentRun]]:
        agent_session = await self._get_session(workspace_id, session_id, user_id)
        result = await self.session.execute(
            select(AgentRun)
            .where(AgentRun.session_id == agent_session.id)
            .order_by(AgentRun.started_at.asc(), AgentRun.id.asc())
        )
        return agent_session, list(result.scalars().all())

    async def update_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        title: str | None | object = ...,
        status: str | object = ...,
    ) -> AgentSession:
        agent_session = await self._get_session(
            workspace_id, session_id, user_id, require_drive=True
        )
        if title is not ...:
            agent_session.title = title  # type: ignore[assignment]
        if status is not ...:
            agent_session.status = status  # type: ignore[assignment]
        agent_session.updated_at = datetime.now(UTC)
        self.session.add(agent_session)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(agent_session)
        return agent_session

    async def delete_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
    ) -> None:
        agent_session = await self._get_session(
            workspace_id, session_id, user_id, require_drive=True
        )
        # ON DELETE SET NULL detaches the runs; their history/events/file-changes survive.
        await self.session.delete(agent_session)
        await self.session.commit()

    async def append_turn(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        prompt: str,
        working_dir: str | None = None,
        worktree_id: UUID | None = None,
    ) -> AgentRun:
        # `_get_session` already enforces drive access on the workspace, and `create_run`
        # re-checks it, so no separate `_get_workspace` call is needed here.
        agent_session = await self._get_session(
            workspace_id, session_id, user_id, require_drive=True
        )
        if agent_session.status == "archived":
            raise AgentSessionArchivedError
        return await self.create_run(
            workspace_id,
            user_id,
            prompt,
            working_dir=working_dir,
            worktree_id=worktree_id,
            session_id=session_id,
        )

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
            .where(AgentRun.workspace_id == workspace.id)
            .order_by(AgentRun.started_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_run(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
        *,
        require_drive: bool = False,
    ) -> AgentRun:
        await self._get_workspace(workspace_id, user_id, require_drive=require_drive)
        result = await self.session.execute(
            select(AgentRun)
            .options(selectinload(AgentRun.events))
            .where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id,
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
        run = await self.get_run(workspace_id, run_id, user_id, require_drive=True)
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
        session: AsyncSession | None = None,
        *,
        worktree_path: str | None = None,
    ) -> list:
        session = session or self.session
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return []
        git_status = await self._read_git_status(session, workspace, worktree_path)
        if git_status is None or not git_status.cloned:
            return []
        return git_status.files

    async def _read_git_status(
        self,
        session: AsyncSession,
        workspace: Workspace,
        worktree_path: str | None,
    ):
        """Read git status for a run's working directory, or ``None`` if it cannot be read.

        Always reads as ``workspace.owner_id``: ``RepositoryService`` is owner-scoped, while a
        run's ``user_id`` may be any team member allowed to drive the workspace. Passing the
        run's user here made every non-owner run's status lookup raise, which the caller then
        swallowed — leaving the run with no recorded file changes at all.
        """
        cipher = WorkspaceSecretCipher(self.settings.workspace_env_encryption_key)
        runtime = get_daytona_runtime()
        repo_service = RepositoryService(session, cipher, runtime)
        try:
            return await repo_service.status(
                workspace.id, workspace.owner_id, worktree_path=worktree_path
            )
        except Exception:
            return None

    async def _compute_file_changes(
        self,
        workspace_id: UUID,
        baseline: list,
        run_id: UUID,
        session: AsyncSession | None = None,
        *,
        worktree_path: str | None = None,
    ) -> list[AgentFileChange]:
        session = session or self.session
        workspace = await session.get(Workspace, workspace_id)
        if workspace is None:
            return []
        current_status = await self._read_git_status(session, workspace, worktree_path)
        if current_status is None or not current_status.cloned:
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

    async def _get_workspace(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        require_drive: bool = False,
    ) -> Workspace:
        workspace, _role = await require_workspace_access(
            self.session, workspace_id, user_id, require_drive=require_drive
        )
        return workspace

    async def _build_session_context(
        self, session: AsyncSession, run: AgentRun
    ) -> str | None:
        """Assemble a transcript of prior turns in this run's session.

        Returns None for one-shot runs (no session) or a session's first turn, so those
        runs receive their raw prompt unchanged. Otherwise returns a chronological
        transcript of prior turns, keeping the most recent turns within a char budget.
        """
        if run.session_id is None:
            return None
        result = await session.execute(
            select(AgentRun)
            .where(AgentRun.session_id == run.session_id, AgentRun.id != run.id)
            .order_by(AgentRun.started_at.asc(), AgentRun.id.asc())
        )
        prior = list(result.scalars().all())
        if not prior:
            return None

        budget = self.settings.agent_session_context_char_budget
        blocks: list[str] = []
        used = 0
        # Walk newest-first so we keep the freshest turns when the budget is tight, then
        # reverse to restore chronological order for the transcript.
        for turn in reversed(prior):
            block = await self._format_turn_block(session, turn)
            if blocks and used + len(block) > budget:
                break
            blocks.append(block)
            used += len(block)
        blocks.reverse()
        return "\n\n".join(blocks)

    async def _format_turn_block(self, session: AsyncSession, turn: AgentRun) -> str:
        # output_text events are token-streamed fragments; concatenate in created_at order.
        text_result = await session.execute(
            select(AgentEvent.content)
            .where(
                AgentEvent.agent_run_id == turn.id,
                AgentEvent.event_type == "output_text",
            )
            .order_by(AgentEvent.created_at.asc())
        )
        assistant = "".join(text_result.scalars().all()).strip()
        cap = self.settings.agent_session_context_per_turn_char_cap
        if len(assistant) > cap:
            assistant = assistant[:cap] + "\n...[truncated]"

        files_result = await session.execute(
            select(AgentFileChange.path, AgentFileChange.status)
            .where(AgentFileChange.agent_run_id == turn.id)
            .order_by(AgentFileChange.created_at.asc())
        )
        files = list(files_result.all())

        parts = [f"User: {turn.prompt}"]
        if assistant:
            parts.append(f"Assistant: {assistant}")
        if turn.status == "error" and turn.error_message:
            parts.append(f"(Previous turn ended with error: {turn.error_message})")
        if files:
            summary = ", ".join(f"{path} ({status})" for path, status in files[:30])
            parts.append(f"Files changed: {summary}")
        return "\n".join(parts)

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
                    deny_dangerous=self.settings.agent_command_deny_dangerous,
                )

                # Read the baseline in the run's own working directory: a worktree-scoped run
                # edits ``worktrees/<slug>/``, so statting the base clone would both miss every
                # change the run makes and attribute pre-existing base-clone dirt to it.
                baseline = await self._capture_baseline(
                    workspace.id, session, worktree_path=working_dir
                )

                context = await self._build_session_context(session, run)
                prompt_for_provider = run.prompt
                if context:
                    prompt_for_provider = (
                        f"{context}\n\n---\nCurrent request:\nUser: {run.prompt}"
                    )

                async for provider_event in provider.stream_response(
                    prompt_for_provider, tools, stop_event
                ):
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
                    if (input_tokens := provider_event.metadata.get("input_tokens")) is not None:
                        run.input_tokens = int(input_tokens)
                        session.add(run)
                    if (output_tokens := provider_event.metadata.get("output_tokens")) is not None:
                        run.output_tokens = int(output_tokens)
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
                    await self._record_run_finished(
                        session, run, baseline, worktree_path=working_dir
                    )
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
                    await self._record_run_finished(
                        session, run, baseline, worktree_path=working_dir
                    )
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
            await self._persist_event(
                session, run.id, "error", message, {"status": "error"}
            )
            await session.commit()
            await self._record_run_finished(session, run)

    async def _record_run_finished(
        self,
        session: AsyncSession,
        run: AgentRun,
        baseline: list | None = None,
        *,
        worktree_path: str | None = None,
    ) -> None:
        if baseline is not None:
            await self._compute_file_changes(
                run.workspace_id,
                baseline,
                run.id,
                session,
                worktree_path=worktree_path,
            )
        self._apply_estimated_cost(run)
        session.add(run)
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
            metadata={
                "status": run.status,
                "tokens_used": run.tokens_used,
                "input_tokens": run.input_tokens,
                "output_tokens": run.output_tokens,
                "estimated_cost": (
                    float(run.estimated_cost) if run.estimated_cost is not None else None
                ),
            },
        )

    def _apply_estimated_cost(self, run: AgentRun) -> None:
        """Derive and store run.estimated_cost from the captured token counts + pricing map."""
        run.estimated_cost = estimate_cost(
            run.provider,
            run.model,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            total_tokens=run.tokens_used,
            overrides=self.settings.model_pricing_overrides,
        )
