import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.core.config import Settings
from app.core.lifecycle import lifecycle
from app.core.metrics import (
    llm_request_seconds,
    llm_time_to_first_token_seconds,
    observe_llm_usage,
    observe_queue_wait,
    observe_run_finished,
    observe_run_start,
)
from app.core.middleware import current_request_id
from app.core.pricing import estimate_cost
from app.core.tracing import current_traceparent
from app.models import AgentEvent, AgentFileChange, AgentRun, AgentSession, Workspace
from app.services.access import require_workspace_access
from app.services.agent_providers import AgentProvider, MockAgentProvider, OpenAIResponsesProvider
from app.services.agent_tools import WorkspaceTools
from app.services.audit import AuditLogService
from app.services.budgets import BudgetService
from app.services.capacity import ACTIVE_RUN_STATUSES, enforce_queue_depth, enforce_run_caps
from app.services.jobs import (
    ENQUEUE_FAILED,
    JOB_TIMEOUT,
    RunDispatcher,
    RunQueueUnavailableError,
    get_run_dispatcher,
)
from app.services.leases import db_now, lease_deadline
from app.services.notifications import NotificationService
from app.services.repositories import RepositoryService
from app.services.runtime import (
    EventBus,
    InMemoryEventBus,
    InMemoryRunRegistry,
    RunLock,
    RunRegistry,
    event_bus,
    run_registry,
)
from app.services.runtime import run_lock as default_run_lock
from app.services.sandbox import get_sandbox_runtime
from app.services.workspaces import WorkspaceSecretCipher

TERMINAL_RUN_STATUSES = ("completed", "error", "stopped")
# Partial unique index backing the one-active-run-per-workspace rule across replicas.
ONE_ACTIVE_RUN_INDEX = "uq_agent_runs_one_active_per_workspace"
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


class RunClaim(NamedTuple):
    # "claimed" | "already_claimed" (active, owned by an earlier delivery) | "already_terminal"
    # | "missing"
    outcome: str
    working_dir: str | None = None


class AgentRuntime:
    """Agent-run view over the process-local run state seam (see ``app.services.runtime``)."""

    def __init__(
        self, registry: RunRegistry | None = None, bus: EventBus | None = None
    ) -> None:
        self.registry = registry if registry is not None else InMemoryRunRegistry()
        self.bus = bus if bus is not None else InMemoryEventBus()

    @staticmethod
    def _topic(run_id: UUID) -> str:
        return f"agent_run:{run_id}"

    def register_run(self, run_id: UUID, task: asyncio.Task, stop_event: asyncio.Event) -> None:
        self.registry.register(run_id, task, stop_event)

    def forget_run(self, run_id: UUID) -> None:
        self.registry.forget(run_id)

    def register_provider(self, run_id: UUID, provider: AgentProvider) -> None:
        self.registry.register_provider(run_id, provider)

    def request_stop(self, run_id: UUID) -> None:
        self.registry.request_stop(run_id)

    def subscribe(self, run_id: UUID) -> asyncio.Queue[dict]:
        return self.bus.subscribe(self._topic(run_id))

    def unsubscribe(self, run_id: UUID, queue: asyncio.Queue[dict]) -> None:
        self.bus.unsubscribe(self._topic(run_id), queue)

    async def publish(self, run_id: UUID, payload: dict) -> None:
        await self.bus.publish(self._topic(run_id), payload)

    async def wait_ready(self, run_id: UUID) -> bool:
        return await self.bus.wait_ready(self._topic(run_id))

    # Control signals (stop) travel over the bus too, so they reach the run on whichever worker
    # executes it. The executing side applies them to its local registry.

    @staticmethod
    def _control_topic(run_id: UUID) -> str:
        return f"agent_run_control:{run_id}"

    async def publish_control(self, run_id: UUID, action: str) -> None:
        await self.bus.publish(self._control_topic(run_id), {"action": action})

    @asynccontextmanager
    async def watch_control(self, run_id: UUID) -> AsyncIterator[None]:
        """Apply control signals for ``run_id`` to this process's registry while inside."""
        topic = self._control_topic(run_id)
        queue = self.bus.subscribe(topic)
        try:
            await self.bus.wait_ready(topic)
            watcher = asyncio.create_task(self._apply_control(run_id, queue))
            try:
                yield
            finally:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
        finally:
            self.bus.unsubscribe(topic, queue)

    async def _apply_control(self, run_id: UUID, queue: asyncio.Queue[dict]) -> None:
        while True:
            message = await queue.get()
            if message.get("action") == "stop":
                self.registry.request_stop(run_id)


agent_runtime = AgentRuntime(run_registry, event_bus)


def event_payload(event: AgentEvent, request_id: str | None = None) -> dict:
    """A stream frame for ``event``; ``request_id`` is the run's (Epic 09), when it has one."""
    payload = {
        "id": str(event.id),
        "agent_run_id": str(event.agent_run_id),
        "event_type": event.event_type,
        "content": event.content,
        "tool_call_id": event.tool_call_id,
        "metadata": event.event_metadata,
        "created_at": event.created_at.isoformat(),
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


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


async def provider_for_run(session: AsyncSession, settings: Settings) -> AgentProvider:
    """Resolve the agent provider for a run: the stored config wins, the environment is fallback.

    A ``platform_llm_settings`` row always drives the OpenHands provider -- it is the only
    provider that accepts a base URL (``OpenAIResponsesProvider`` hardcodes api.openai.com), so
    an OpenAI-*compatible* endpoint has to go through LiteLLM. With no row we defer entirely to
    ``provider_from_settings``, which stays sync and env-only so its existing unit tests and
    every env-configured deployment are untouched.

    With nothing configured at all the run is refused rather than answered by the mock provider,
    unless ``agent_allow_mock_provider`` opts in.
    """
    # Imported lazily: app.services.platform_llm imports app.models and app.services.workspaces,
    # and this module sits below both -- a module-level import risks a cycle. Matches the
    # existing lazy import of openhands_provider above.
    from app.services.openhands_provider import (
        OpenHandsAgentProvider,
        OpenHandsConfigurationError,
    )
    from app.services.platform_llm import (
        LlmSettingsDecryptionError,
        PlatformLlmSettingsService,
    )

    service = PlatformLlmSettingsService(
        session=session,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        settings=settings,
    )
    try:
        resolved = await service.resolve()
    except LlmSettingsDecryptionError as error:
        raise AgentConfigurationError(str(error)) from error

    if resolved is not None and resolved.source == "database":
        try:
            return OpenHandsAgentProvider(
                base_url=resolved.base_url,
                api_key=resolved.api_key,
                model=resolved.model,
                max_iterations=settings.openhands_max_iterations,
            )
        except OpenHandsConfigurationError as error:
            raise AgentConfigurationError(str(error)) from error

    provider = provider_from_settings(settings)
    if isinstance(provider, MockAgentProvider) and not settings.agent_allow_mock_provider:
        raise AgentConfigurationError(
            "No LLM is configured. A platform owner must set the LLM base URL and model in "
            "Settings before agent runs can start."
        )
    return provider


class AgentService:
    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        runtime: AgentRuntime = agent_runtime,
        run_lock: RunLock = default_run_lock,
        dispatcher: RunDispatcher | None = None,
        leave_active_on_cancel: bool = False,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.settings = settings
        self.runtime = runtime
        self.run_lock = run_lock
        self._dispatcher = dispatcher
        # On a worker a cancel means shutdown or timeout, never a user stop: leave the row active
        # for whoever handles that (the shutdown sweep, the timeout handler) to finish it.
        self.leave_active_on_cancel = leave_active_on_cancel

    @property
    def dispatcher(self) -> RunDispatcher:
        return self._dispatcher if self._dispatcher is not None else get_run_dispatcher()

    async def _find_active_run_id(self, workspace_id: UUID) -> UUID | None:
        active = await self.session.execute(
            select(AgentRun.id).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        return active.scalar_one_or_none()

    async def create_run(
        self,
        workspace_id: UUID,
        user_id: UUID,
        prompt: str,
        working_dir: str | None = None,
        worktree_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> AgentRun:
        lifecycle.ensure_accepting_work()
        workspace = await self._get_workspace(workspace_id, user_id, require_drive=True)
        # Block/warn before spending: an enforcing budget over its limit stops the run here.
        # Kept outside the locks: a budget warning notifies, and a notification commits, which
        # would release the advisory locks below before the run is inserted.
        await BudgetService(self.session).assert_within_budget(workspace, user_id)

        # Check-then-insert under the per-workspace lock so two concurrent requests cannot both
        # pass the "no active run" guard: the in-process lock serialises this replica, and a
        # transaction-scoped Postgres advisory lock (released at the commit below) serialises
        # replicas. It fails closed -- a lock error fails the request before anything is
        # inserted. The partial unique index stays the guarantee if the lock is ever bypassed.
        lock_key = f"agent_run:workspace:{workspace.id}"
        async with self.run_lock.hold(lock_key):
            await self.session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
            )
            if await self._find_active_run_id(workspace.id) is not None:
                raise AgentRunAlreadyActiveError

            # Fair use (Epic 06): team/global concurrency caps, then queue backpressure.
            await enforce_run_caps(self.session, self.settings, workspace)
            await enforce_queue_depth(self.session, self.settings)

            provider = await provider_for_run(self.session, self.settings)
            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user_id,
                worktree_id=worktree_id,
                session_id=session_id,
                prompt=prompt,
                status="thinking",
                provider=provider.provider_name,
                model=provider.model_name,
                working_dir=working_dir,
                request_id=current_request_id(),
                traceparent=current_traceparent(),
            )
            try:
                # Savepoint: a violation rolls back only this insert, leaving the session usable.
                async with self.session.begin_nested():
                    self.session.add(run)
                    await self.session.flush()
            except IntegrityError as error:
                if ONE_ACTIVE_RUN_INDEX in str(error.orig):
                    raise AgentRunAlreadyActiveError from None
                raise

            if session_id is not None:
                agent_session = await self.session.get(AgentSession, session_id)
                if agent_session is not None:
                    # Auto-title from the first turn's prompt, and bump updated_at so the
                    # session sorts as recently active (onupdate only fires when a column
                    # actually changes).
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

        if self.settings.run_execution_mode == "queue":
            await self._enqueue_run(run)
            return run

        stop_event = asyncio.Event()
        task = asyncio.create_task(self._execute_run(run.id, stop_event, working_dir))
        self.runtime.register_run(run.id, task, stop_event)
        return run

    async def _enqueue_run(self, run: AgentRun) -> None:
        run.enqueued_at = datetime.now(UTC)
        self.session.add(run)
        await self.session.commit()
        try:
            await self.dispatcher.enqueue_agent_run(run.id)
        except Exception as error:
            # The committed `thinking` row would otherwise block the workspace forever.
            await self.fail_active_run(
                run.id, ENQUEUE_FAILED, "The run could not be queued for execution."
            )
            raise RunQueueUnavailableError from error
        await self.session.refresh(run)

    async def claim_run(self, run_id: UUID, worker_id: str, attempt: int) -> RunClaim:
        """Atomically take ownership of a queued run; at most one delivery ever wins.

        ``claimed_at IS NULL`` is the guard: arq delivers at least once, but only the first
        claim of an active run succeeds, so a redelivered job can never re-run the agent.
        The claim also takes the run's lease, which the worker then keeps renewing.
        """
        async with self.session_maker() as session:
            claimed = await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                    AgentRun.claimed_at.is_(None),
                )
                .values(
                    claimed_at=datetime.now(UTC),
                    worker_id=worker_id,
                    attempt=attempt,
                    heartbeat_at=db_now(),
                    lease_expires_at=lease_deadline(self.settings.run_lease_seconds),
                )
                .returning(AgentRun.working_dir, AgentRun.enqueued_at, AgentRun.claimed_at)
            )
            row = claimed.first()
            if row is not None:
                await session.commit()
                observe_queue_wait("agent", row.enqueued_at, row.claimed_at)
                return RunClaim("claimed", row.working_dir)
            status = await session.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
            if status is None:
                return RunClaim("missing")
            if status in ACTIVE_RUN_STATUSES:
                return RunClaim("already_claimed")
            return RunClaim("already_terminal")

    async def renew_lease(self, run_id: UUID, worker_id: str) -> bool:
        """Extend the lease of a run this worker owns; False once it is no longer ours to run.

        Only the owning worker matches, and only while the run is active: the reaper never takes
        over a live lease, and an owner can never revive a run that was already ended.
        """
        async with self.session_maker() as session:
            renewed = await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.worker_id == worker_id,
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .values(
                    heartbeat_at=db_now(),
                    lease_expires_at=lease_deadline(self.settings.run_lease_seconds),
                )
                .returning(AgentRun.id)
            )
            if renewed.scalar_one_or_none() is None:
                return False
            await session.commit()
            return True

    async def execute_claimed_run(
        self,
        run_id: UUID,
        working_dir: str | None,
        stop_event: asyncio.Event,
        timeout_seconds: float,
    ) -> str:
        """Run a claimed job to its end on this worker; returns the run's final status."""
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._execute_run(run_id, stop_event, working_dir)
        except TimeoutError:
            await self.fail_active_run(
                run_id, JOB_TIMEOUT, f"The run exceeded its {timeout_seconds:g}s time limit."
            )
            return JOB_TIMEOUT
        async with self.session_maker() as session:
            status = await session.scalar(select(AgentRun.status).where(AgentRun.id == run_id))
        return status or "missing"

    async def fail_active_run(
        self,
        run_id: UUID,
        reason: str,
        message: str,
        *,
        lease_expired: bool = False,
        unclaimed: bool = False,
    ) -> bool:
        """End a still-active run as ``error``/``reason``; a no-op if it already finished.

        The UPDATE is conditional on the run being active, so it never overwrites a terminal
        status written concurrently (e.g. a stop). ``lease_expired`` (the reaper, a redelivered
        job) additionally requires that no worker holds a live lease *at write time* -- it ran
        out, or was never stamped (claimed before leases existed) -- so a renewal that raced the
        check wins. ``unclaimed`` (the lost-job reaper) requires that no worker has claimed the
        run, so a claim that raced the check wins. Returns whether this call ended the run.
        """
        async with self.session_maker() as session:
            conditions = [AgentRun.id == run_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES)]
            if lease_expired:
                conditions.append(
                    or_(AgentRun.lease_expires_at.is_(None), AgentRun.lease_expires_at < db_now())
                )
            if unclaimed:
                conditions.append(AgentRun.claimed_at.is_(None))
            result = await session.execute(
                update(AgentRun)
                .where(*conditions)
                .values(
                    status="error",
                    error_reason=reason,
                    error_message=message,
                    completed_at=datetime.now(UTC),
                    lease_expires_at=None,
                )
                .returning(AgentRun.id)
            )
            if result.scalar_one_or_none() is None:
                return False  # nothing written; closing the session ends the transaction
            await session.flush()
            await self._persist_event(
                session, run_id, "error", message, {"status": "error", "reason": reason}
            )
            run = await session.get(AgentRun, run_id)
            await self._record_run_finished(session, run)
            await session.commit()
            return True

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
        *,
        since: datetime | None = None,
    ) -> list[AgentEvent]:
        """The run's persisted events in stream order, optionally only those at or after
        ``since`` (inclusive: an event sharing the caller's high-water mark may be unsent)."""
        await self._get_workspace(workspace_id, user_id)
        run_exists = await self.session.scalar(
            select(AgentRun.id).where(
                AgentRun.id == run_id,
                AgentRun.workspace_id == workspace_id,
            )
        )
        if run_exists is None:
            raise AgentRunNotFoundError
        query = select(AgentEvent).where(AgentEvent.agent_run_id == run_id)
        if since is not None:
            query = query.where(AgentEvent.created_at >= since)
        result = await self.session.execute(query.order_by(AgentEvent.created_at, AgentEvent.id))
        return list(result.scalars().all())

    async def stop_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        """Stop an active run. Idempotent: stopping a run that already ended (or a second click)
        changes nothing and returns the run as it is."""
        run = await self.get_run(workspace_id, run_id, user_id, require_drive=True)
        if await self._transition(self.session, run.id, "stopped"):
            self.runtime.request_stop(run.id)
            await self._persist_event(
                self.session, run.id, "status", "stopped", {"status": "stopped"}
            )
            # The run may be executing on a worker process; after the commit, so the worker
            # sees `stopped` when it unwinds instead of writing its own terminal status.
            await self.runtime.publish_control(run.id, "stop")
            await self.session.refresh(run)
            await self._record_run_finished(self.session, run)
        else:
            await self.session.refresh(run)  # it ended concurrently; report how
        return run

    async def _transition(
        self, session: AsyncSession, run_id: UUID, status: str, **values
    ) -> bool:
        """Move an active run to ``status``; False, writing nothing, once it has already ended.

        The run state machine: ``thinking``/``executing`` move freely between each other and to
        a terminal status (``completed``/``error``/``stopped``), and a terminal status is final.
        Conditional on the row still being active, so concurrent actors -- a stop on another
        replica, the reaper, the executing worker -- cannot clobber each other, and a replayed
        transition is a no-op. Ending a run also releases its lease. In-memory ORM copies of the
        run are left stale; callers refresh what they read afterwards.
        """
        if status in TERMINAL_RUN_STATUSES:
            values.setdefault("completed_at", datetime.now(UTC))
            values["lease_expires_at"] = None
        result = await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES))
            .values(status=status, **values)
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

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
        result = await self.session.execute(select(AgentRun).where(AgentRun.id == run_id))
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
        runtime = get_sandbox_runtime()
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

    async def _build_session_context(self, session: AsyncSession, run: AgentRun) -> str | None:
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

                provider = await provider_for_run(session, self.settings)
                # The provider is resolved twice -- create_run stamps run.provider/run.model,
                # then this runs. Now that the config is editable at runtime, an owner saving
                # new settings in between would leave the run recording a model that never
                # ran, and estimate_cost keys off exactly these two columns. Re-stamp with
                # what will actually execute.
                if (run.provider, run.model) != (provider.provider_name, provider.model_name):
                    run.provider = provider.provider_name
                    run.model = provider.model_name
                    session.add(run)
                    await session.flush()
                self.runtime.register_provider(run.id, provider)
                terminal_event_received = False
                tools = WorkspaceTools(
                    workspace=workspace,
                    runtime=get_sandbox_runtime(),
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
                    prompt_for_provider = f"{context}\n\n---\nCurrent request:\nUser: {run.prompt}"

                llm_labels = {"provider": run.provider, "model": run.model or "unknown"}
                llm_started = time.perf_counter()
                first_event_at: datetime | None = None
                async for provider_event in provider.stream_response(
                    prompt_for_provider, tools, stop_event
                ):
                    # Every provider opens with a synthetic "thinking" status before any
                    # model call; the first *content* event is the first token.
                    if first_event_at is None and provider_event.event_type != "status":
                        first_event_at = datetime.now(UTC)
                        llm_time_to_first_token_seconds.labels(**llm_labels).observe(
                            time.perf_counter() - llm_started
                        )
                        observe_run_start("agent", run.enqueued_at, first_event_at)
                    if stop_event.is_set():
                        break
                    if provider_event.event_type == "status":
                        status_value = provider_event.metadata.get("status")
                        if status_value in ACTIVE_RUN_STATUSES + TERMINAL_RUN_STATUSES:
                            if not await self._transition(session, run.id, status_value):
                                # Ended elsewhere (stopped on another replica, reaped):
                                # that outcome stands, so stop the provider too.
                                stop_event.set()
                                break
                            if status_value in TERMINAL_RUN_STATUSES:
                                terminal_event_received = True
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

                llm_request_seconds.labels(**llm_labels).observe(
                    time.perf_counter() - llm_started
                )

                await session.refresh(run)
                if terminal_event_received:
                    await self._record_run_finished(
                        session, run, baseline, worktree_path=working_dir
                    )
                elif run.status in ACTIVE_RUN_STATUSES:
                    final_status = "stopped" if stop_event.is_set() else "completed"
                    if await self._transition(session, run.id, final_status):
                        await self._persist_event(
                            session,
                            run.id,
                            "status",
                            final_status,
                            {"status": final_status},
                        )
                        await session.refresh(run)
                        await self._record_run_finished(
                            session, run, baseline, worktree_path=working_dir
                        )
            except asyncio.CancelledError:
                if lifecycle.draining or self.leave_active_on_cancel:
                    # Shutdown (or a worker-side timeout) cancelled us: leave the row active for
                    # the reconciliation sweep / timeout handler to record why it ended.
                    raise
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
        # The run is already in this session's identity map on the execution path, so this
        # is a lookup, not a query; the terminal paths pay one primary-key read.
        run = await session.get(AgentRun, run_id)
        await self.runtime.publish(
            run_id, event_payload(event, run.request_id if run is not None else None)
        )
        return event

    async def _mark_stopped(self, run_id: UUID) -> None:
        async with self.session_maker() as session:
            if not await self._transition(session, run_id, "stopped"):
                return
            await self._persist_event(session, run_id, "status", "stopped", {"status": "stopped"})
            run = await session.get(AgentRun, run_id)
            await self._record_run_finished(session, run)

    async def _mark_error(self, run_id: UUID, message: str) -> None:
        async with self.session_maker() as session:
            if not await self._transition(session, run_id, "error", error_message=message):
                return
            await self._persist_event(session, run_id, "error", message, {"status": "error"})
            run = await session.get(AgentRun, run_id)
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
        observe_run_finished(
            "agent", run.status, run.error_reason, run.started_at, run.completed_at
        )
        observe_llm_usage(
            run.provider,
            run.model,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cost_usd=float(run.estimated_cost) if run.estimated_cost is not None else None,
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
