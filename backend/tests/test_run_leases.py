"""Epic 04: run leases, heartbeats, the reaper, conditional transitions, cross-replica lock."""

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.models import AgentEvent, AgentRun, CommandRun, Notification, User, Workspace
from app.services.agent_providers import AgentProvider, ProviderEvent
from app.services.agents import AgentRunAlreadyActiveError, AgentRuntime, AgentService
from app.services.commands import CommandRuntime, CommandService
from app.services.jobs import JOB_LOST, agent_run_job_id, command_run_job_id
from app.services.leases import heartbeat
from app.services.reaper import INTERRUPTED_WORKER_DIED, reap_expired_leases, reap_lost_jobs
from app.services.runtime import InMemoryEventBus, InMemoryRunLock, InMemoryRunRegistry
from app.worker import run_agent_job, run_command_job
from tests.test_job_queue import (
    FakeDispatcher,
    db_session,
    make_run,
    make_user_and_workspace,
    queue_settings,
)
from tests.test_worker_jobs import (
    FakeDaytona,
    StoppableProvider,
    command_ctx,
    event_count,
    make_ctx,
    seed_command_run,
    seed_run,
)

__all__ = ["db_session"]


def agent_service(session, session_maker, **overrides) -> AgentService:
    return AgentService(
        session,
        session_maker,
        queue_settings(**overrides),
        runtime=AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
        dispatcher=FakeDispatcher(),
    )


def command_service(session, session_maker) -> CommandService:
    return CommandService(
        session,
        session_maker,
        queue_settings(),
        runtime=CommandRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
        sandbox_runtime=FakeDaytona(),
    )


def ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def ahead(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def events_of(session, run_id) -> list[AgentEvent]:
    return list(
        (
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.agent_run_id == run_id)
                .order_by(AgentEvent.created_at)
            )
        ).all()
    )


async def notification_count(session, user_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.user_id == user_id)
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_lease_settings_have_sane_defaults():
    settings = Settings()
    assert settings.run_lease_seconds == 60
    assert settings.run_heartbeat_seconds == 15
    assert settings.run_reaper_interval_seconds == 30
    assert settings.run_heartbeat_seconds < settings.run_lease_seconds


def test_heartbeat_must_renew_before_the_lease_expires():
    with pytest.raises(ValidationError, match="run_heartbeat_seconds"):
        Settings(run_lease_seconds=10, run_heartbeat_seconds=10)


# ---------------------------------------------------------------------------
# Conditional state transitions
# ---------------------------------------------------------------------------


async def test_stop_is_idempotent(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="executing")
    service = agent_service(session, session_maker)

    first = await service.stop_run(run.workspace_id, run.id, run.user_id)
    second = await service.stop_run(run.workspace_id, run.id, run.user_id)

    assert first.status == second.status == "stopped"
    events = await events_of(session, run.id)
    assert [(e.event_type, e.content) for e in events] == [("status", "stopped")]
    assert await notification_count(session, run.user_id) == 1


async def test_stopping_a_finished_run_is_a_clean_noop(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="completed", completed_at=datetime.now(UTC))

    result = await agent_service(session, session_maker).stop_run(
        run.workspace_id, run.id, run.user_id
    )

    assert result.status == "completed"
    assert await event_count(session, run.id) == 0


class CompletesAfterStopProvider(AgentProvider):
    """Reports ``completed`` after the run was stopped elsewhere (stop signal lost)."""

    provider_name = "mock"
    model_name = "mock-model"

    def __init__(self, stopped: asyncio.Event) -> None:
        self.stopped = stopped

    async def stream_response(self, prompt, tools, stop_event):
        yield ProviderEvent("status", "executing", {"status": "executing"})
        await self.stopped.wait()
        yield ProviderEvent("output_text", "late output", {})
        yield ProviderEvent("status", "completed", {"status": "completed"})


async def _no_baseline(*_args, **_kwargs) -> list:
    return []


async def test_a_late_provider_completion_cannot_overwrite_a_stop(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_run(session)
    stopped = asyncio.Event()

    async def provider(*_args, **_kwargs):
        return CompletesAfterStopProvider(stopped)

    monkeypatch.setattr("app.services.agents.provider_for_run", provider)
    monkeypatch.setattr(AgentService, "_capture_baseline", _no_baseline)
    service = agent_service(session, session_maker)
    events = service.runtime.subscribe(run.id)

    task = asyncio.create_task(service._execute_run(run.id, asyncio.Event()))
    while (await asyncio.wait_for(events.get(), 2.0)).get("content") != "executing":
        pass
    # Another replica stops the run; this process never sees the control message.
    async with session_maker() as other:
        await agent_service(other, session_maker).stop_run(run.workspace_id, run.id, run.user_id)
    stopped.set()
    await asyncio.wait_for(task, 2.0)

    await session.refresh(run)
    assert run.status == "stopped"
    statuses = [e.content for e in await events_of(session, run.id) if e.event_type == "status"]
    assert "completed" not in statuses


async def test_mark_error_never_overwrites_a_terminal_run(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="stopped", completed_at=datetime.now(UTC))

    await agent_service(session, session_maker)._mark_error(run.id, "boom")

    await session.refresh(run)
    assert run.status == "stopped"
    assert run.error_message is None
    assert await event_count(session, run.id) == 0


async def test_terminal_transition_releases_the_lease(db_session):
    session, session_maker = db_session
    run = await seed_run(
        session, status="executing", worker_id="worker-a", lease_expires_at=ahead(60)
    )

    await agent_service(session, session_maker).stop_run(run.workspace_id, run.id, run.user_id)

    await session.refresh(run)
    assert run.lease_expires_at is None


async def test_command_finish_never_overwrites_a_reaped_run(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session)
    service = command_service(session, session_maker)
    frames = service.runtime.subscribe(run.id)

    async with session_maker() as worker_session:
        stale = await worker_session.get(CommandRun, run.id)  # loaded while still running
        await service.fail_active_run(run.id, INTERRUPTED_WORKER_DIED, "gone")
        frames.get_nowait()  # the reaper's own terminal frame
        await service._mark_finished(worker_session, stale, "completed", 0)

    await session.refresh(run)
    assert run.status == "error"
    assert run.exit_code is None
    assert frames.empty()


# ---------------------------------------------------------------------------
# Leases and heartbeats
# ---------------------------------------------------------------------------


async def test_claim_stamps_a_lease(db_session):
    session, session_maker = db_session
    run = await seed_run(session)

    claim = await agent_service(session, session_maker).claim_run(run.id, "worker-a", 1)

    assert claim.outcome == "claimed"
    await session.refresh(run)
    assert run.heartbeat_at is not None
    assert run.lease_expires_at > ahead(50)


async def test_only_the_owning_worker_can_renew_its_lease(db_session):
    session, session_maker = db_session
    run = await seed_run(
        session, status="executing", worker_id="worker-a", lease_expires_at=ahead(1)
    )
    service = agent_service(session, session_maker)

    assert await service.renew_lease(run.id, "worker-b") is False
    await session.refresh(run)
    assert run.lease_expires_at < ahead(5)

    assert await service.renew_lease(run.id, "worker-a") is True
    await session.refresh(run)
    assert run.lease_expires_at > ahead(50)


async def test_a_finished_run_cannot_be_renewed(db_session):
    session, session_maker = db_session
    run = await seed_run(
        session, status="error", worker_id="worker-a", completed_at=datetime.now(UTC)
    )

    assert await agent_service(session, session_maker).renew_lease(run.id, "worker-a") is False


async def test_command_claim_stamps_a_lease_the_owner_can_renew(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session)
    service = command_service(session, session_maker)

    assert await service.claim_run(run.id, "worker-a", 1) == "claimed"
    await session.refresh(run)
    assert run.lease_expires_at > ahead(50)

    assert await service.renew_lease(run.id, "worker-b") is False
    assert await service.renew_lease(run.id, "worker-a") is True


async def test_heartbeat_renews_until_the_lease_is_lost():
    results = iter([True, RuntimeError("db blip"), True, False])
    calls = 0
    lost: list[bool] = []

    async def renew() -> bool:
        nonlocal calls
        calls += 1
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    await asyncio.wait_for(
        heartbeat(renew, interval=0.001, on_lost=lambda: lost.append(True)), 1.0
    )

    assert calls == 4  # a failed renewal is retried, not fatal
    assert lost == [True]


async def test_a_worker_that_lost_its_lease_stops_the_run(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_run(session)

    async def stoppable(*_args, **_kwargs):
        return StoppableProvider()

    monkeypatch.setattr("app.services.agents.provider_for_run", stoppable)
    ctx = make_ctx(session_maker, run_heartbeat_seconds=0.05, run_lease_seconds=1)
    events = ctx["agent_runtime"].subscribe(run.id)

    job = asyncio.create_task(run_agent_job(ctx, str(run.id)))
    while (await asyncio.wait_for(events.get(), 2.0)).get("content") != "executing":
        pass
    # The reaper decided this worker was dead (e.g. a long DB partition) and ended the run.
    async with session_maker() as other:
        await other.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(status="error", error_reason=INTERRUPTED_WORKER_DIED, lease_expires_at=None)
        )
        await other.commit()

    assert await asyncio.wait_for(job, 2.0) == "error"


async def test_a_command_job_that_lost_its_lease_is_cancelled(db_session, monkeypatch):
    session, session_maker = db_session
    run = await seed_command_run(session)
    started = asyncio.Event()

    async def endless(self, run_id, confirmed):
        started.set()
        await asyncio.Event().wait()  # a sandbox command that never finishes

    monkeypatch.setattr(CommandService, "_execute_run", endless)
    ctx = command_ctx(session_maker, run_heartbeat_seconds=0.05, run_lease_seconds=1)

    job = asyncio.create_task(run_command_job(ctx, str(run.id)))
    await asyncio.wait_for(started.wait(), 2.0)
    await command_service(session, session_maker).fail_active_run(
        run.id, INTERRUPTED_WORKER_DIED, "gone"
    )

    # Cancelled, like a shutdown: arq re-queues it and the redelivery finds the run terminal.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 2.0)


async def test_redelivery_leaves_a_run_with_a_live_lease_to_its_worker(db_session):
    """arq can redeliver a job whose first delivery is still running (e.g. a Redis hiccup)."""
    session, session_maker = db_session
    run = await seed_run(
        session,
        status="executing",
        claimed_at=datetime.now(UTC),
        worker_id="worker-alive",
        lease_expires_at=ahead(30),
    )
    command = await seed_command_run(
        session, claimed_at=datetime.now(UTC), worker_id="worker-alive", lease_expires_at=ahead(30)
    )

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "already_claimed"
    assert await run_command_job(command_ctx(session_maker), str(command.id)) == "already_claimed"

    await session.refresh(run)
    await session.refresh(command)
    assert (run.status, command.status) == ("executing", "running")
    assert await event_count(session, run.id) == 0


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


def runtimes() -> tuple[AgentRuntime, CommandRuntime]:
    return (
        AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
        CommandRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
    )


async def reap(session_maker):
    agent_rt, command_rt = runtimes()
    return await reap_expired_leases(
        session_maker, queue_settings(), agent_runtime=agent_rt, command_runtime=command_rt
    )


async def test_reaper_ends_a_run_whose_lease_expired(db_session):
    session, session_maker = db_session
    run = await seed_run(
        session,
        status="executing",
        claimed_at=ago(120),
        worker_id="dead",
        lease_expires_at=ago(30),
    )
    agent_rt, command_rt = runtimes()
    frames = agent_rt.subscribe(run.id)

    result = await reap_expired_leases(
        session_maker, queue_settings(), agent_runtime=agent_rt, command_runtime=command_rt
    )

    assert result.agent_run_ids == [run.id]
    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == INTERRUPTED_WORKER_DIED
    assert run.completed_at is not None
    assert run.lease_expires_at is None
    frame = frames.get_nowait()
    assert frame["event_type"] == "error"
    assert frame["metadata"] == {"status": "error", "reason": INTERRUPTED_WORKER_DIED}
    assert await notification_count(session, run.user_id) == 1


async def test_reaper_frees_the_workspace_for_a_new_run(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="executing", worker_id="dead", lease_expires_at=ago(30))

    await reap(session_maker)

    new_run = await agent_service(session, session_maker).create_run(
        run.workspace_id, run.user_id, "again"
    )
    assert new_run.status == "thinking"


async def test_reaper_leaves_live_and_unleased_runs_alone(db_session):
    session, session_maker = db_session
    live = await seed_run(
        session, status="executing", worker_id="alive", lease_expires_at=ahead(30)
    )
    inline = await seed_run(session, status="executing")  # inline mode: never leased
    command = await seed_command_run(session, worker_id="alive", lease_expires_at=ahead(30))

    result = await reap(session_maker)

    assert live.id not in result.agent_run_ids
    assert inline.id not in result.agent_run_ids
    assert command.id not in result.command_run_ids
    for row in (live, inline, command):
        await session.refresh(row)
    assert (live.status, inline.status, command.status) == ("executing", "executing", "running")


async def test_reaper_ends_a_command_run_whose_lease_expired(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session, worker_id="dead", lease_expires_at=ago(30))
    agent_rt, command_rt = runtimes()
    frames = command_rt.subscribe(run.id)

    result = await reap_expired_leases(
        session_maker, queue_settings(), agent_runtime=agent_rt, command_runtime=command_rt
    )

    assert result.command_run_ids == [run.id]
    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == INTERRUPTED_WORKER_DIED
    assert run.lease_expires_at is None
    assert frames.get_nowait() == {
        "type": "status",
        "status": "error",
        "exit_code": None,
        "reason": INTERRUPTED_WORKER_DIED,
    }


async def test_reaper_never_hijacks_a_lease_renewed_after_it_looked(db_session):
    """The expiry is re-checked in the UPDATE, so a renewal racing the reaper wins."""
    session, session_maker = db_session
    run = await seed_run(session, status="executing", worker_id="alive", lease_expires_at=ahead(30))
    command = await seed_command_run(session, worker_id="alive", lease_expires_at=ahead(30))

    agent_ended = await agent_service(session, session_maker).fail_active_run(
        run.id, INTERRUPTED_WORKER_DIED, "gone", lease_expired=True
    )
    command_ended = await command_service(session, session_maker).fail_active_run(
        command.id, INTERRUPTED_WORKER_DIED, "gone", lease_expired=True
    )

    assert (agent_ended, command_ended) == (False, False)
    await session.refresh(run)
    await session.refresh(command)
    assert (run.status, command.status) == ("executing", "running")


async def test_concurrent_reapers_end_a_dead_run_exactly_once():
    settings = get_settings()
    engines = [create_async_engine(settings.database_url) for _ in range(2)]
    user, workspace = make_user_and_workspace()
    run = make_run(user, workspace, "executing")
    run.worker_id = "dead"
    run.lease_expires_at = ago(30)
    try:
        async with async_sessionmaker(engines[0], expire_on_commit=False)() as setup:
            setup.add_all([user, workspace])
            await setup.flush()
            setup.add(run)
            await setup.commit()

        results = await asyncio.gather(
            *(reap(async_sessionmaker(engine, expire_on_commit=False)) for engine in engines)
        )
        assert sorted(run.id in r.agent_run_ids for r in results) == [False, True]
        async with async_sessionmaker(engines[0])() as check:
            assert await event_count(check, run.id) == 1
    finally:
        async with async_sessionmaker(engines[0])() as cleanup:
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()
        for engine in engines:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Reaper: queued runs whose job was lost before any claim
# ---------------------------------------------------------------------------


def queue_redis(server=None) -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(server=server or fakeredis.FakeServer())


async def reap_lost(session_maker, redis, runtimes_pair=None):
    agent_rt, command_rt = runtimes_pair or runtimes()
    return await reap_lost_jobs(
        session_maker, queue_settings(), redis, agent_runtime=agent_rt, command_runtime=command_rt
    )


async def test_reaper_ends_a_queued_run_whose_job_was_lost(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="thinking", enqueued_at=ago(600))
    agent_rt, command_rt = runtimes()
    frames = agent_rt.subscribe(run.id)

    result = await reap_lost(session_maker, queue_redis(), (agent_rt, command_rt))

    assert run.id in result.agent_run_ids
    await session.refresh(run)
    assert run.status == "error"
    assert run.error_reason == JOB_LOST
    assert run.completed_at is not None
    frame = frames.get_nowait()
    assert frame["event_type"] == "error"
    assert frame["metadata"] == {"status": "error", "reason": JOB_LOST}
    assert await notification_count(session, run.user_id) == 1

    new_run = await agent_service(session, session_maker).create_run(
        run.workspace_id, run.user_id, "again"
    )
    assert new_run.status == "thinking"


async def test_reaper_ends_a_queued_command_run_whose_job_was_lost(db_session):
    session, session_maker = db_session
    run = await seed_command_run(session, enqueued_at=ago(600))
    agent_rt, command_rt = runtimes()
    frames = command_rt.subscribe(run.id)

    result = await reap_lost(session_maker, queue_redis(), (agent_rt, command_rt))

    assert run.id in result.command_run_ids
    await session.refresh(run)
    assert (run.status, run.error_reason) == ("error", JOB_LOST)
    assert frames.get_nowait() == {
        "type": "status",
        "status": "error",
        "exit_code": None,
        "reason": JOB_LOST,
    }


@pytest.mark.parametrize("job_state", ["queued", "deferred", "in_progress"])
async def test_reaper_leaves_a_run_whose_job_is_still_in_redis(db_session, job_state):
    """However long a run has waited, a job arq still holds will be delivered."""
    session, session_maker = db_session
    run = await seed_run(session, status="thinking", enqueued_at=ago(3600))
    command = await seed_command_run(session, enqueued_at=ago(3600))
    redis = queue_redis()
    queue = queue_settings().run_queue_name
    for job_id in (agent_run_job_id(run.id), command_run_job_id(command.id)):
        if job_state == "in_progress":
            await redis.set(f"arq:in-progress:{job_id}", b"1")
        else:
            score = ahead(60) if job_state == "deferred" else ago(60)
            await redis.zadd(queue, {job_id: score.timestamp() * 1000})

    result = await reap_lost(session_maker, redis)

    assert run.id not in result.agent_run_ids
    assert command.id not in result.command_run_ids
    await session.refresh(run)
    await session.refresh(command)
    assert (run.status, command.status) == ("thinking", "running")


async def test_reaper_leaves_young_claimed_and_inline_runs_alone(db_session):
    session, session_maker = db_session
    young = await seed_run(session, status="thinking", enqueued_at=ago(5))
    claimed = await seed_run(
        session,
        status="executing",
        enqueued_at=ago(600),
        claimed_at=ago(500),
        worker_id="alive",
        lease_expires_at=ahead(30),
    )
    inline = await seed_run(session, status="executing")  # never enqueued

    result = await reap_lost(session_maker, queue_redis())

    for row in (young, claimed, inline):
        assert row.id not in result.agent_run_ids
        await session.refresh(row)
    assert (young.status, claimed.status, inline.status) == ("thinking", "executing", "executing")


async def test_reaper_skips_lost_jobs_while_redis_is_down(db_session):
    session, session_maker = db_session
    run = await seed_run(session, status="thinking", enqueued_at=ago(600))
    server = fakeredis.FakeServer()
    server.connected = False

    result = await reap_lost(session_maker, queue_redis(server))

    assert run.id not in result.agent_run_ids
    await session.refresh(run)
    assert run.status == "thinking"


async def test_a_claim_racing_the_lost_job_reaper_wins(db_session):
    """``claimed_at IS NULL`` is re-checked in the UPDATE, after the reaper looked."""
    session, session_maker = db_session
    run = await seed_run(session, status="thinking", enqueued_at=ago(600))
    command = await seed_command_run(session, enqueued_at=ago(600))
    await agent_service(session, session_maker).claim_run(run.id, "worker-a", 1)
    await command_service(session, session_maker).claim_run(command.id, "worker-a", 1)

    agent_ended = await agent_service(session, session_maker).fail_active_run(
        run.id, JOB_LOST, "gone", unclaimed=True
    )
    command_ended = await command_service(session, session_maker).fail_active_run(
        command.id, JOB_LOST, "gone", unclaimed=True
    )

    assert (agent_ended, command_ended) == (False, False)
    await session.refresh(run)
    await session.refresh(command)
    assert (run.status, command.status) == ("thinking", "running")


# ---------------------------------------------------------------------------
# Cross-replica single-active-run lock
# ---------------------------------------------------------------------------


@pytest.fixture
async def committed_workspace():
    """A user + workspace committed for real, so separate connections ("replicas") see it."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    user, workspace = make_user_and_workspace()
    async with async_sessionmaker(engine, expire_on_commit=False)() as setup:
        setup.add_all([user, workspace])
        await setup.commit()
    try:
        yield user, workspace
    finally:
        async with async_sessionmaker(engine)() as cleanup:
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()
        await engine.dispose()


async def _create_run_as_replica(engine, user, workspace):
    """Each "replica" has its own engine and its own in-process lock."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        service = AgentService(
            session,
            maker,
            queue_settings(),
            runtime=AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
            run_lock=InMemoryRunLock(),
            dispatcher=FakeDispatcher(),
        )
        try:
            return await service.create_run(workspace.id, user.id, "race")
        except AgentRunAlreadyActiveError as error:
            return error


async def test_two_replicas_racing_create_run_admit_exactly_one(committed_workspace):
    user, workspace = committed_workspace
    settings = get_settings()
    engines = [create_async_engine(settings.database_url) for _ in range(2)]
    try:
        results = await asyncio.gather(
            *(_create_run_as_replica(engine, user, workspace) for engine in engines)
        )
    finally:
        for engine in engines:
            await engine.dispose()

    assert sorted(type(r).__name__ for r in results) == ["AgentRun", "AgentRunAlreadyActiveError"]


async def test_create_run_serialises_on_the_workspace_advisory_lock(committed_workspace):
    user, workspace = committed_workspace
    settings = get_settings()
    holder_engine = create_async_engine(settings.database_url)
    replica_engine = create_async_engine(settings.database_url)
    try:
        async with holder_engine.connect() as holder:
            await holder.begin()
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"agent_run:workspace:{workspace.id}"},
            )
            attempt = asyncio.create_task(_create_run_as_replica(replica_engine, user, workspace))
            await asyncio.sleep(0.3)
            assert not attempt.done(), "create_run did not wait for the workspace lock"
            await holder.commit()
        created = await asyncio.wait_for(attempt, 5.0)
        assert isinstance(created, AgentRun)
    finally:
        await holder_engine.dispose()
        await replica_engine.dispose()
