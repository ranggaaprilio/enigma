"""Plan 5: a verification run never leaves a user stuck (stop, reaper, no orphan fix agents)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import VerificationRun
from app.services.verification.fixer import AgentFixer
from app.services.verification.orchestrator import (
    ACTIVE_STATUSES,
    PlanCriteria,
    Step,
    VerificationOrchestrator,
)
from tests.sandbox.fakes import FakeSandboxRuntime
from tests.test_verification_fixer import FakeFixer, _fails_until_fixed
from tests.test_verification_orchestrator import _cleanup, _run_status, _seed_with_criteria


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class StoppingFixer(FakeFixer):
    """FakeFixer that records which agent runs the orchestrator stopped."""

    def __init__(self, maker, **kwargs) -> None:
        super().__init__(maker, **kwargs)
        self.stopped: list[UUID] = []
        self.status_error: Exception | None = None

    async def stop(self, run, agent_run_id: UUID) -> None:
        self.stopped.append(agent_run_id)

    async def status(self, agent_run_id: UUID) -> str | None:
        if self.status_error is not None:
            raise self.status_error
        return await super().status(agent_run_id)


def _orchestrator(maker, fixer) -> VerificationOrchestrator:
    return VerificationOrchestrator(
        PlanCriteria("ignored"),
        session_maker=maker,
        runtime=FakeSandboxRuntime(exec_handler=_fails_until_fixed({"fixed": False})),
        fixer=fixer,
    )


async def _fixing_with_agent(maker):
    """A run parked in ``fixing`` whose (fake) agent run is still working."""
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 3}
    )
    fixer = StoppingFixer(maker)
    orch = _orchestrator(maker, fixer)
    assert await orch.advance(run.id) is Step.CONTINUE  # test fails -> fixing
    assert await orch.advance(run.id) is Step.WAIT  # fix agent started
    [(agent_run_id, _)] = fixer.started
    return workspace, run, user_id, fixer, orch, agent_run_id


async def test_ending_a_fixing_run_stops_its_fix_agent(maker) -> None:
    workspace, run, user_id, fixer, orch, agent_run_id = await _fixing_with_agent(maker)
    try:
        assert await orch.end(run.id, "rejected", ACTIVE_STATUSES) == "fixing"
        assert await _run_status(maker, run.id) == "rejected"
        assert fixer.stopped == [agent_run_id]
        # Ending it again changes nothing and stops nothing twice.
        assert await orch.end(run.id, "error", ACTIVE_STATUSES) is None
        assert fixer.stopped == [agent_run_id]
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_ending_a_testing_run_stops_no_agent(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 2}
    )
    fixer = StoppingFixer(maker)
    orch = _orchestrator(maker, fixer)
    try:
        assert await orch.end(run.id, "rejected", ACTIVE_STATUSES) == "testing"
        assert fixer.stopped == []
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_fail_stops_the_fix_agent(maker) -> None:
    workspace, run, user_id, fixer, orch, agent_run_id = await _fixing_with_agent(maker)
    try:
        assert await orch.fail(run.id) is True
        assert await _run_status(maker, run.id) == "error"
        assert fixer.stopped == [agent_run_id]
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_crashing_fix_step_stops_the_fix_agent(maker) -> None:
    workspace, run, user_id, fixer, orch, agent_run_id = await _fixing_with_agent(maker)
    fixer.status_error = RuntimeError("db blip")
    try:
        with pytest.raises(RuntimeError):
            await orch.advance(run.id)
        assert await _run_status(maker, run.id) == "error"
        assert fixer.stopped == [agent_run_id]
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_run_ended_while_its_fix_agent_starts_stops_that_agent(maker) -> None:
    """The fix-start race: the run is stopped between ``start()`` and recording the agent."""
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 3}
    )

    class RacingFixer(StoppingFixer):
        async def start(self, workspace, run, failing) -> UUID:
            agent_run_id = await super().start(workspace, run, failing)
            async with self.maker() as session:  # a human pressed Stop meanwhile
                await session.execute(
                    update(VerificationRun)
                    .where(VerificationRun.id == run.id)
                    .values(status="rejected")
                )
                await session.commit()
            return agent_run_id

    fixer = RacingFixer(maker)
    orch = _orchestrator(maker, fixer)
    try:
        assert await orch.advance(run.id) is Step.CONTINUE  # -> fixing
        await orch.advance(run.id)
        [(agent_run_id, _)] = fixer.started
        assert fixer.stopped == [agent_run_id]
        async with maker() as session:
            row = await session.get(VerificationRun, run.id)
            assert row.status == "rejected"
            assert row.agent_run_id is None
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_agent_fixer_stop_uses_the_system_stop_and_never_raises() -> None:
    calls: list[tuple] = []

    class Service:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def stop_active_run(self, run_id):
            calls.append(run_id)
            raise RuntimeError("agent row gone")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    fixer = AgentFixer(session_maker=lambda: _Session(), service_factory=Service)
    # No author: it was deleted after the fix started. The stop must not depend on it.
    run = VerificationRun(workspace_id=uuid4(), plan_ref="p", status="fixing", criteria={})
    agent_run_id = uuid4()

    await fixer.stop(run, agent_run_id)  # swallowed and logged

    assert calls == [agent_run_id]


# --- the reaper: verification runs whose queue job is gone --------------------------------


def _queue_redis():
    import fakeredis

    return fakeredis.aioredis.FakeRedis(server=fakeredis.FakeServer())


async def _age(maker, run_id, seconds: float) -> None:
    from datetime import UTC, datetime, timedelta

    async with maker() as session:
        await session.execute(
            update(VerificationRun)
            .where(VerificationRun.id == run_id)
            .values(updated_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        await session.commit()


async def _reap(maker, redis, fixer):
    from app.services.reaper import reap_lost_verification_jobs
    from tests.test_job_queue import queue_settings

    return await reap_lost_verification_jobs(
        maker,
        queue_settings(),
        redis,
        orchestrator=_orchestrator(maker, fixer),
    )


async def test_reaper_ends_a_verification_run_whose_job_is_gone(maker) -> None:
    workspace, run, user_id, fixer, _orch, agent_run_id = await _fixing_with_agent(maker)
    await _age(maker, run.id, 3600)
    try:
        reaped = await _reap(maker, _queue_redis(), fixer)

        assert run.id in reaped  # the dev DB is shared: other rows may be reaped too
        assert await _run_status(maker, run.id) == "error"
        assert fixer.stopped == [agent_run_id]  # its fix agent goes with it
    finally:
        await _cleanup(maker, workspace, user_id)


@pytest.mark.parametrize("job_state", ["queued", "deferred", "in_progress"])
async def test_reaper_leaves_a_verification_run_whose_job_is_known(maker, job_state) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.jobs import verification_run_job_id
    from tests.test_job_queue import queue_settings

    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest"}
    )
    await _age(maker, run.id, 3600)
    redis = _queue_redis()
    job_id = verification_run_job_id(run.id)
    if job_state == "in_progress":
        await redis.set(f"arq:in-progress:{job_id}", b"1")
    else:
        offset = timedelta(seconds=60 if job_state == "deferred" else -60)
        score = (datetime.now(UTC) + offset).timestamp() * 1000
        await redis.zadd(queue_settings().run_queue_name, {job_id: score})
    try:
        assert run.id not in await _reap(maker, redis, StoppingFixer(maker))
        assert await _run_status(maker, run.id) == "testing"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_reaper_leaves_a_fresh_verification_run_alone(maker) -> None:
    """Inside the grace window the job may simply not be enqueued yet."""
    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "pytest"}
    )
    try:
        assert run.id not in await _reap(maker, _queue_redis(), StoppingFixer(maker))
        assert await _run_status(maker, run.id) == "planning"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_reaper_skips_verification_runs_when_redis_fails(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest"}
    )
    await _age(maker, run.id, 3600)

    class BrokenRedis:
        def __getattr__(self, _name):
            async def fail(*_args, **_kwargs):
                raise ConnectionError("redis down")

            return fail

    try:
        assert run.id not in await _reap(maker, BrokenRedis(), StoppingFixer(maker))
        assert await _run_status(maker, run.id) == "testing"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_reaper_ignores_finished_verification_runs(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(maker, "review", {"test_command": "x"})
    await _age(maker, run.id, 3600)
    try:
        assert run.id not in await _reap(maker, _queue_redis(), StoppingFixer(maker))
        assert await _run_status(maker, run.id) == "review"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_run_reaper_includes_verification_runs_in_queue_mode(monkeypatch) -> None:
    import asyncio

    from app.services import reaper
    from tests.test_job_queue import queue_settings

    calls: list[str] = []

    async def noop(*_args, **_kwargs):
        calls.append("other")
        return reaper.ReapResult()

    async def verification(*_args, **_kwargs):
        calls.append("verification")
        raise asyncio.CancelledError  # end the loop after one pass

    monkeypatch.setattr(reaper, "reap_expired_leases", noop)
    monkeypatch.setattr(reaper, "reap_lost_jobs", noop)
    monkeypatch.setattr(reaper, "reap_lost_verification_jobs", verification)

    with pytest.raises(asyncio.CancelledError):
        await reaper.run_reaper(None, queue_settings(), redis=_queue_redis())
    assert calls[-1] == "verification"
