"""Epic 02: worker job handlers, called directly with an arq-style ``ctx`` (no live worker)."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from arq.worker import Retry
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import (
    AgentEvent,
    AgentRun,
    CommandRun,
    CustomCommand,
    SpecPhase,
    User,
    Workspace,
)
from app.services.agent_providers import AgentProvider, ProviderEvent
from app.services.agents import AgentRuntime, AgentService
from app.services.commands import CommandRuntime, CommandService
from app.services.jobs import (
    DEAD_LETTER,
    ENQUEUE_FAILED,
    JOB_TIMEOUT,
    WORKER_ERROR,
    WORKER_LOST,
    RunQueueUnavailableError,
)
from app.services.runtime import InMemoryEventBus, InMemoryRunRegistry
from app.services.speckit import SpecKitService
from app.worker import (
    finalize_spec_phase_job,
    run_agent_job,
    run_command_job,
    spec_finalize_max_tries,
)
from tests.test_job_queue import (
    FakeDispatcher,
    db_session,
    make_run,
    make_user_and_workspace,
    queue_settings,
)
from tests.test_specs import FakeRuntime, _fake_create_run, make_user_workspace, make_workflow

__all__ = ["db_session"]


def make_ctx(session_maker, job_try: int = 1, **settings_overrides) -> dict:
    settings = queue_settings(worker_id="worker-a", **settings_overrides)
    return {
        "job_try": job_try,
        "settings": settings,
        "session_maker": session_maker,
        "agent_runtime": AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
    }


async def seed_run(session, status: str = "thinking", **fields) -> AgentRun:
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    run = make_run(user, workspace, status)
    for name, value in fields.items():
        setattr(run, name, value)
    session.add(run)
    await session.commit()
    return run


async def event_count(session, run_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(AgentEvent).where(AgentEvent.agent_run_id == run_id)
    )


async def test_agent_job_claims_and_runs_to_completion(db_session):
    session, session_maker = db_session
    run = await seed_run(session)

    outcome = await run_agent_job(make_ctx(session_maker, job_try=2), str(run.id))

    assert outcome == "completed"
    await session.refresh(run)
    assert run.status == "completed"
    assert run.worker_id == "worker-a"
    assert run.attempt == 2
    assert run.claimed_at is not None
    assert await event_count(session, run.id) > 0


async def test_redelivered_job_does_not_execute_twice(db_session):
    session, session_maker = db_session
    run = await seed_run(session)

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "completed"
    events_after_first = await event_count(session, run.id)

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "already_terminal"
    assert await event_count(session, run.id) == events_after_first


async def test_run_stopped_while_queued_is_never_executed(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="stopped", completed_at=datetime.now(UTC))

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "already_terminal"
    assert await event_count(session, run.id) == 0


async def test_missing_run_is_a_clean_noop(db_session):
    _, session_maker = db_session
    assert await run_agent_job(make_ctx(session_maker), str(uuid4())) == "missing"


async def test_redelivery_of_a_claimed_active_run_marks_it_worker_lost(db_session):
    """A worker died mid-run and arq redelivered: never re-run side effects, end the run."""
    session, session_maker = db_session
    run = await seed_run(
        session,
        status="executing",
        claimed_at=datetime.now(UTC),
        worker_id="worker-dead",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=5),
    )

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == WORKER_LOST

    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == WORKER_LOST
    assert run.completed_at is not None
    events = (
        await session.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == run.id))
    ).all()
    assert [event.event_type for event in events] == ["error"]


async def test_failure_before_claim_is_retried_then_dead_lettered(db_session, monkeypatch):
    """arq only retries on Retry/CancelledError: a bare exception would drop the job and leave
    the run `thinking` forever, so the handler must convert it."""
    session, session_maker = db_session
    run = await seed_run(session)

    async def broken_claim(*_args, **_kwargs):
        raise ConnectionError("db blip")

    monkeypatch.setattr(AgentService, "claim_run", broken_claim)

    with pytest.raises(Retry):
        await run_agent_job(make_ctx(session_maker, job_try=1), str(run.id))
    await session.refresh(run)
    assert run.status == "thinking"

    final_try = make_ctx(session_maker, job_try=3)
    assert final_try["settings"].worker_max_tries == 3
    assert await run_agent_job(final_try, str(run.id)) == DEAD_LETTER
    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == DEAD_LETTER


async def test_dead_letter_that_cannot_reach_the_db_does_not_raise(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_run(session)

    async def broken(*_args, **_kwargs):
        raise ConnectionError("db down")

    monkeypatch.setattr(AgentService, "claim_run", broken)
    monkeypatch.setattr(AgentService, "fail_active_run", broken)

    assert await run_agent_job(make_ctx(session_maker, job_try=3), str(run.id)) == DEAD_LETTER


async def test_command_claim_failure_is_retried(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_command_run(session)

    async def broken_claim(*_args, **_kwargs):
        raise ConnectionError("db blip")

    monkeypatch.setattr(CommandService, "claim_run", broken_claim)
    with pytest.raises(Retry):
        await run_command_job(command_ctx(session_maker), str(run.id))


class HangingProvider(AgentProvider):
    provider_name = "mock"
    model_name = "mock-model"

    async def stream_response(self, prompt, tools, stop_event):
        yield ProviderEvent("status", "executing", {"status": "executing"})
        await asyncio.sleep(3600)


async def test_run_exceeding_its_timeout_is_recorded_as_job_timeout(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_run(session)

    async def hanging_provider(*_args, **_kwargs):
        return HangingProvider()

    monkeypatch.setattr("app.services.agents.provider_for_run", hanging_provider)
    ctx = make_ctx(session_maker, agent_run_timeout_seconds=1)

    assert await run_agent_job(ctx, str(run.id)) == JOB_TIMEOUT

    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == JOB_TIMEOUT
    assert ctx["agent_runtime"].registry.active_run_ids() == []


async def test_concurrent_double_delivery_claims_exactly_once():
    """Two workers race on the same job over separate connections; one claim wins."""
    settings = get_settings()
    engines = [create_async_engine(settings.database_url) for _ in range(2)]
    user, workspace = make_user_and_workspace()
    run = make_run(user, workspace, "thinking")
    try:
        async with async_sessionmaker(engines[0], expire_on_commit=False)() as setup:
            setup.add_all([user, workspace])
            await setup.flush()
            setup.add(run)
            await setup.commit()

        async def claim(engine, worker_id):
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                service = AgentService(session, maker, queue_settings())
                return await service.claim_run(run.id, worker_id, 1)

        claims = await asyncio.gather(claim(engines[0], "w1"), claim(engines[1], "w2"))
        assert sorted(c.outcome for c in claims) == ["already_claimed", "claimed"]
    finally:
        async with async_sessionmaker(engines[0])() as cleanup:
            await cleanup.execute(delete(AgentRun).where(AgentRun.workspace_id == workspace.id))
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()
        for engine in engines:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Command runs
# ---------------------------------------------------------------------------


class FakeDaytona:
    async def close(self) -> None:
        return None


async def seed_command_run(session, status: str = "running", **fields) -> CommandRun:
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    command = CustomCommand(workspace_id=workspace.id, name=f"c-{uuid4().hex[:6]}", command="ls")
    session.add(command)
    await session.flush()
    run = CommandRun(
        custom_command_id=command.id,
        workspace_id=workspace.id,
        user_id=user.id,
        status=status,
        output="",
        **fields,
    )
    session.add(run)
    await session.commit()
    return run


def command_ctx(session_maker, **kwargs) -> dict:
    ctx = make_ctx(session_maker, **kwargs)
    ctx["command_runtime"] = CommandRuntime(InMemoryRunRegistry(), InMemoryEventBus())
    ctx["daytona_runtime"] = FakeDaytona()
    return ctx


@pytest.fixture
def fake_command_execution(monkeypatch):
    executed: list = []

    async def execute(self, run_id, confirmed):
        executed.append(run_id)
        async with self.session_maker() as session:
            run = await session.get(CommandRun, run_id)
            await self._mark_finished(session, run, "completed", 0)

    monkeypatch.setattr(CommandService, "_execute_run", execute)
    return executed


async def test_queue_mode_command_run_is_enqueued_not_started(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_command_run(session, status="completed")
    workspace = await session.get(Workspace, run.workspace_id)
    command = await session.get(CustomCommand, run.custom_command_id)
    dispatcher = FakeDispatcher()
    runtime = CommandRuntime(InMemoryRunRegistry(), InMemoryEventBus())
    service = CommandService(
        session,
        session_maker,
        queue_settings(),
        runtime=runtime,
        daytona_runtime=FakeDaytona(),
        dispatcher=dispatcher,
    )

    created = await service.create_run(workspace.id, command.id, run.user_id, confirmed=True)

    assert created.status == "running"
    assert dispatcher.command_runs == [created.id]
    assert runtime.get_task(created.id) is None
    await session.refresh(created)
    assert created.enqueued_at is not None


async def test_command_enqueue_failure_ends_the_run(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session, status="completed")
    workspace = await session.get(Workspace, run.workspace_id)
    service = CommandService(
        session,
        session_maker,
        queue_settings(),
        runtime=CommandRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
        daytona_runtime=FakeDaytona(),
        dispatcher=FakeDispatcher(fail=True),
    )

    with pytest.raises(RunQueueUnavailableError):
        await service.create_run(workspace.id, run.custom_command_id, run.user_id, True)

    failed = (
        await session.scalars(
            select(CommandRun).where(
                CommandRun.workspace_id == workspace.id, CommandRun.id != run.id
            )
        )
    ).one()
    await session.refresh(failed)
    assert failed.status == "error"
    assert failed.error_reason == ENQUEUE_FAILED


async def test_command_job_runs_once_across_redelivery(db_session, fake_command_execution):
    session, session_maker = db_session
    run = await seed_command_run(session)

    assert await run_command_job(command_ctx(session_maker), str(run.id)) == "completed"
    assert await run_command_job(command_ctx(session_maker), str(run.id)) == "already_terminal"

    assert fake_command_execution == [run.id]
    await session.refresh(run)
    assert run.worker_id == "worker-a"
    assert run.claimed_at is not None


async def test_command_job_redelivered_after_claim_is_worker_lost(
    db_session, fake_command_execution
):
    session, session_maker = db_session
    run = await seed_command_run(session, claimed_at=datetime.now(UTC), worker_id="gone")
    # Claimed before leases existed: no live lease, so the redelivery may end it.
    ctx = command_ctx(session_maker)
    frames = ctx["command_runtime"].subscribe(run.id)

    assert await run_command_job(ctx, str(run.id)) == WORKER_LOST

    assert fake_command_execution == []
    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == WORKER_LOST
    frame = frames.get_nowait()
    assert frame == {"type": "status", "status": "error", "exit_code": None, "reason": WORKER_LOST}


# ---------------------------------------------------------------------------
# Spec Kit phase finalizer
# ---------------------------------------------------------------------------


async def seed_running_phase(session, phase_name: str, run_status: str):
    user, workspace = await make_user_workspace(
        session, owner_email=f"spec-job-{uuid4()}@example.com"
    )
    workflow = await make_workflow(session, workspace, user, phase_status={phase_name: "running"})
    run = make_run(user, workspace, run_status)
    session.add(run)
    await session.flush()
    phase = (
        await session.execute(
            select(SpecPhase).where(
                SpecPhase.workflow_id == workflow.id, SpecPhase.phase == phase_name
            )
        )
    ).scalar_one()
    phase.agent_run_id = run.id
    phase.artifact_path = f"{workflow.spec_dir}/spec.md"
    session.add(phase)
    await session.commit()
    return workflow, phase, run


def spec_ctx(session_maker, **kwargs) -> dict:
    ctx = make_ctx(session_maker, **kwargs)
    ctx["daytona_runtime"] = FakeRuntime()
    ctx["dispatcher"] = FakeDispatcher()
    return ctx


async def test_queue_mode_run_phase_enqueues_the_finalizer_instead_of_a_task(
    db_session, monkeypatch
):
    from app.services.runtime import run_registry

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"q-{uuid4()}@example.com")
    await session.commit()
    monkeypatch.setattr("app.services.agents.AgentService.create_run", _fake_create_run())
    dispatcher = FakeDispatcher()
    service = SpecKitService(
        session, session_maker, queue_settings(), runtime=FakeRuntime(), dispatcher=dispatcher
    )

    workflow = await service.start_workflow(workspace.id, user.id, "Add Auth", "We need login")
    constitution = next(p for p in workflow.phases if p.phase == "constitution")

    assert dispatcher.spec_finalizes == [(constitution.id, constitution.agent_run_id)]
    assert run_registry.get_task(constitution.id) is None


async def test_finalizer_job_retries_while_the_run_is_still_active(db_session):
    session, session_maker = db_session
    _, phase, run = await seed_running_phase(session, "specify", "executing")

    with pytest.raises(Retry):
        await finalize_spec_phase_job(spec_ctx(session_maker), str(phase.id), str(run.id))
    await session.refresh(phase)
    assert phase.status == "running"


async def test_finalizer_job_moves_a_completed_review_phase_to_awaiting_review(db_session):
    session, session_maker = db_session
    workflow, phase, run = await seed_running_phase(session, "specify", "completed")

    outcome = await finalize_spec_phase_job(spec_ctx(session_maker), str(phase.id), str(run.id))

    assert outcome == "awaiting_review"
    await session.refresh(phase)
    await session.refresh(workflow)
    assert phase.status == "awaiting_review"
    assert workflow.status == "awaiting_review"

    # Redelivery after the phase moved on is a no-op.
    again = await finalize_spec_phase_job(spec_ctx(session_maker), str(phase.id), str(run.id))
    assert again == "stale"


async def test_finalizer_job_marks_the_phase_failed_when_the_run_errored(db_session):
    session, session_maker = db_session
    _, phase, run = await seed_running_phase(session, "plan", "error")

    assert (
        await finalize_spec_phase_job(spec_ctx(session_maker), str(phase.id), str(run.id))
        == "error"
    )
    await session.refresh(phase)
    assert phase.status == "error"


async def test_finalizer_job_gives_up_on_its_final_try(db_session):
    session, session_maker = db_session
    _, phase, run = await seed_running_phase(session, "plan", "executing")
    ctx = spec_ctx(session_maker, job_try=spec_finalize_max_tries(queue_settings()))

    assert await finalize_spec_phase_job(ctx, str(phase.id), str(run.id)) == "error"
    await session.refresh(phase)
    assert phase.status == "error"


async def test_constitution_finalizer_job_chains_specify_through_the_queue(db_session):
    """The chained specify run must be enqueued too, not started as a task on the worker."""
    session, session_maker = db_session
    workflow, phase, run = await seed_running_phase(session, "constitution", "completed")
    ctx = spec_ctx(session_maker)

    assert await finalize_spec_phase_job(ctx, str(phase.id), str(run.id)) == "completed"

    specify = (
        await session.execute(
            select(SpecPhase).where(
                SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "specify"
            )
        )
    ).scalar_one()
    await session.refresh(specify)
    assert specify.status == "running"
    assert ctx["dispatcher"].agent_runs == [specify.agent_run_id]
    assert ctx["dispatcher"].spec_finalizes == [(specify.id, specify.agent_run_id)]


# ---------------------------------------------------------------------------
# Stop over the control channel
# ---------------------------------------------------------------------------


class StoppableProvider(AgentProvider):
    """Streams until told to stop, like a real provider honouring ``stop_event``."""

    provider_name = "mock"
    model_name = "mock-model"

    async def stream_response(self, prompt, tools, stop_event):
        yield ProviderEvent("status", "executing", {"status": "executing"})
        while not stop_event.is_set():
            await asyncio.sleep(0.01)


async def test_stop_from_another_process_stops_the_run_on_the_worker(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_run(session)

    async def stoppable(*_args, **_kwargs):
        return StoppableProvider()

    monkeypatch.setattr("app.services.agents.provider_for_run", stoppable)
    bus = InMemoryEventBus()  # stands in for Redis shared by both processes
    ctx = make_ctx(session_maker)
    ctx["agent_runtime"] = AgentRuntime(InMemoryRunRegistry(), bus)
    api_runtime = AgentRuntime(InMemoryRunRegistry(), bus)
    events = api_runtime.subscribe(run.id)

    job = asyncio.create_task(run_agent_job(ctx, str(run.id)))
    while (await asyncio.wait_for(events.get(), 2.0)).get("content") != "executing":
        pass
    await api_runtime.publish_control(run.id, "stop")

    assert await asyncio.wait_for(job, 2.0) == "stopped"


async def test_a_stop_that_lands_before_the_watch_still_stops_the_run(db_session, monkeypatch):
    """The API stopped the run between the worker's claim and its control subscription."""
    session, session_maker = db_session
    run = await seed_run(session)
    real_claim = AgentService.claim_run

    async def claim_then_api_stops(self, run_id, worker_id, attempt):
        claim = await real_claim(self, run_id, worker_id, attempt)
        async with self.session_maker() as other:
            row = await other.get(AgentRun, run_id)
            row.status = "stopped"
            row.completed_at = datetime.now(UTC)
            await other.commit()
        return claim

    async def stoppable(*_args, **_kwargs):
        return StoppableProvider()

    monkeypatch.setattr(AgentService, "claim_run", claim_then_api_stops)
    monkeypatch.setattr("app.services.agents.provider_for_run", stoppable)

    outcome = await asyncio.wait_for(run_agent_job(make_ctx(session_maker), str(run.id)), 2.0)

    assert outcome == "stopped"


async def test_stop_run_signals_the_executing_worker(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="executing")
    runtime = AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus())
    control = runtime.bus.subscribe(f"agent_run_control:{run.id}")
    service = AgentService(session, session_maker, queue_settings(), runtime=runtime)

    stopped = await service.stop_run(run.workspace_id, run.id, run.user_id)

    assert stopped.status == "stopped"
    assert control.get_nowait() == {"action": "stop"}


# ---------------------------------------------------------------------------
# WebSocket catch-up (closing the subscribe gap across processes)
# ---------------------------------------------------------------------------


async def test_catch_up_sends_missed_events_while_the_run_is_still_active(db_session):
    """Events persisted between the history read and the live subscription are not lost."""
    from app.api.agents import catch_up

    session, session_maker = db_session
    run = await seed_run(session, status="executing")
    seen = AgentEvent(agent_run_id=run.id, event_type="output_text", content="a")
    missed = AgentEvent(agent_run_id=run.id, event_type="output_text", content="b")
    session.add_all([seen, missed])
    await session.commit()

    payloads, ended = await catch_up(
        session_maker, queue_settings(), run.workspace_id, run.id, run.user_id, {str(seen.id)}
    )

    assert not ended
    assert [payload["id"] for payload in payloads] == [str(missed.id)]


async def test_catch_up_reports_a_run_that_already_ended(db_session):
    from app.api.agents import catch_up

    session, session_maker = db_session
    run = await seed_run(session, status="completed", completed_at=datetime.now(UTC))
    final = AgentEvent(
        agent_run_id=run.id,
        event_type="status",
        content="completed",
        event_metadata={"status": "completed"},
    )
    session.add(final)
    await session.commit()

    payloads, ended = await catch_up(
        session_maker, queue_settings(), run.workspace_id, run.id, run.user_id, set()
    )

    assert ended
    assert [payload["id"] for payload in payloads] == [str(final.id)]


# ---------------------------------------------------------------------------
# Review hardening
# ---------------------------------------------------------------------------


async def test_a_stop_whose_control_message_was_lost_is_still_honoured(db_session, monkeypatch):
    """The bus is at-most-once: the worker also notices the stop by re-reading its run."""
    import app.worker as worker_module

    session, session_maker = db_session
    run = await seed_run(session)

    async def stoppable(*_args, **_kwargs):
        return StoppableProvider()

    monkeypatch.setattr("app.services.agents.provider_for_run", stoppable)
    monkeypatch.setattr(worker_module, "RUN_STATUS_POLL_SECONDS", 0.05)
    ctx = make_ctx(session_maker)
    job = asyncio.create_task(run_agent_job(ctx, str(run.id)))
    while ctx["agent_runtime"].registry.get_task(run.id) is None:
        await asyncio.sleep(0.01)

    async with session_maker() as other:  # the API's stop, with no control message
        row = await other.get(AgentRun, run.id)
        row.status = "stopped"
        row.completed_at = datetime.now(UTC)
        await other.commit()

    assert await asyncio.wait_for(job, 2.0) == "stopped"


async def test_finalizer_enqueue_failure_also_ends_the_agent_run(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"fq-{uuid4()}@example.com")
    await session.commit()

    class FinalizeDown(FakeDispatcher):
        async def enqueue_spec_finalize(self, phase_id, run_id) -> None:
            raise ConnectionError("redis is down")

    service = SpecKitService(
        session, session_maker, queue_settings(), runtime=FakeRuntime(), dispatcher=FinalizeDown()
    )
    with pytest.raises(RunQueueUnavailableError):
        await service.start_workflow(workspace.id, user.id, "Add Auth", "We need login")

    runs = (
        await session.scalars(select(AgentRun).where(AgentRun.workspace_id == workspace.id))
    ).all()
    assert len(runs) == 1
    await session.refresh(runs[0])
    assert runs[0].status == "error"
    assert runs[0].error_reason == ENQUEUE_FAILED


async def test_command_job_records_an_escaping_error_instead_of_leaving_it_running(
    db_session, monkeypatch
):
    session, session_maker = db_session
    run = await seed_command_run(session)

    async def explode(self, run_id, confirmed):
        raise RuntimeError("bookkeeping failed")

    monkeypatch.setattr(CommandService, "_execute_run", explode)

    assert await run_command_job(command_ctx(session_maker), str(run.id)) == WORKER_ERROR
    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == WORKER_ERROR


# ---------------------------------------------------------------------------
# Worker entrypoint
# ---------------------------------------------------------------------------


def test_worker_settings_register_every_job_and_follow_settings():
    from app.services.jobs import AGENT_RUN_JOB, COMMAND_RUN_JOB, SPEC_FINALIZE_JOB
    from app.worker import WorkerSettings

    names = {function.name for function in WorkerSettings.functions}
    assert names == {AGENT_RUN_JOB, COMMAND_RUN_JOB, SPEC_FINALIZE_JOB}
    settings = get_settings()
    assert WorkerSettings.queue_name == settings.run_queue_name
    assert WorkerSettings.max_jobs == settings.worker_max_jobs
    assert WorkerSettings.max_tries == settings.worker_max_tries
    # Graceful stop: let running jobs finish within the same grace the API gives its runs.
    assert WorkerSettings.job_completion_wait == int(settings.shutdown_run_grace_seconds)
    finalize = next(f for f in WorkerSettings.functions if f.name == SPEC_FINALIZE_JOB)
    assert finalize.max_tries == spec_finalize_max_tries(settings)


async def test_worker_refuses_to_start_outside_queue_mode(monkeypatch):
    import app.worker as worker_module

    monkeypatch.setattr(
        worker_module, "get_settings", lambda: queue_settings(run_execution_mode="inline")
    )
    with pytest.raises(RuntimeError, match="RUN_EXECUTION_MODE=queue"):
        await worker_module.startup({"redis": object()})


async def test_worker_startup_and_shutdown_reconcile_only_its_own_runs(monkeypatch):
    import app.worker as worker_module
    from app.core.lifecycle import lifecycle

    swept: list[str] = []

    async def reconcile(session_maker, *, worker_id=None, **_kwargs):
        swept.append(worker_id)

    async def reaper(*_args, **_kwargs):
        await asyncio.Event().wait()

    class FakeEngine:
        async def dispose(self):
            return None

    monkeypatch.setattr(worker_module, "get_settings", lambda: queue_settings(worker_id="w-9"))
    monkeypatch.setattr(worker_module, "reconcile_interrupted_runs", reconcile)
    monkeypatch.setattr(worker_module, "run_reaper", reaper)
    monkeypatch.setattr(worker_module, "get_daytona_runtime", lambda: FakeDaytona())
    monkeypatch.setattr(worker_module, "engine", FakeEngine())
    monkeypatch.setattr(lifecycle, "draining", False)
    ctx: dict = {"redis": object()}

    await worker_module.startup(ctx)
    assert {"settings", "session_maker", "agent_runtime", "command_runtime"} <= ctx.keys()
    assert not ctx["reaper"].done()  # the lease reaper runs for the worker's lifetime
    await worker_module.shutdown(ctx)

    assert ctx["reaper"].cancelled()
    assert swept == ["w-9", "w-9"]
    assert lifecycle.draining
    assert ctx["retention"] is None  # retention is off unless a window is configured


async def test_worker_runs_retention_when_configured_and_cancels_it_on_shutdown(monkeypatch):
    import app.worker as worker_module
    from app.core.lifecycle import lifecycle

    async def idle(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def reconcile(*_args, **_kwargs):
        return None

    class FakeEngine:
        async def dispose(self):
            return None

    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: queue_settings(worker_id="w-9", retention_command_output_days=7),
    )
    monkeypatch.setattr(worker_module, "reconcile_interrupted_runs", reconcile)
    monkeypatch.setattr(worker_module, "run_reaper", idle)
    monkeypatch.setattr("app.services.retention.run_retention", idle)
    monkeypatch.setattr(worker_module, "get_daytona_runtime", lambda: FakeDaytona())
    monkeypatch.setattr(worker_module, "engine", FakeEngine())
    monkeypatch.setattr(lifecycle, "draining", False)  # shutdown() begins a drain
    ctx: dict = {"redis": object()}

    await worker_module.startup(ctx)
    assert not ctx["retention"].done()
    await worker_module.shutdown(ctx)

    assert ctx["retention"].cancelled()
