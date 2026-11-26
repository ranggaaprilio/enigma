"""Plan 2 Task 5: verification runs execute off-request, inline or on the arq queue.

Inline mode drives the run in a task registered in ``run_registry`` (so the shutdown drain
covers it); queue mode enqueues it, and the worker job advances the run and defers with
``Retry`` while a fix agent run is in progress, never holding a worker slot while waiting.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from arq import Retry
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.models import AgentRun, VerificationRun
from app.services.jobs import RunQueueUnavailableError
from app.services.runtime import run_registry
from app.services.sandbox.base import RuntimeCommandResult
from app.services.verification import execution
from app.worker import run_verification_job, verification_max_tries
from tests.sandbox.fakes import FakeSandboxRuntime
from tests.test_verification_orchestrator import _cleanup, _run_status, _seed_with_criteria


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _passing_runtime() -> FakeSandboxRuntime:
    return FakeSandboxRuntime(
        exec_handler=lambda _c: RuntimeCommandResult(exit_code=0, stdout="ok", stderr="")
    )


class RecordingDispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.enqueued: list = []

    async def enqueue_verification_run(self, run_id) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.enqueued.append(run_id)


async def test_inline_start_drives_the_run_in_a_registered_task(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "pytest", "max_iterations": 1}
    )
    settings = Settings(run_execution_mode="inline", verification_poll_seconds=0.01)
    try:
        await execution.start_verification_run(
            run.id, settings, session_maker=maker, runtime=_passing_runtime()
        )
        task = run_registry.get_task(run.id)
        assert task is not None  # the shutdown drain can see (and wait for) it
        await task

        assert await _run_status(maker, run.id) == "review"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_queue_start_enqueues_instead_of_running(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "pytest", "max_iterations": 1}
    )
    dispatcher = RecordingDispatcher()
    try:
        await execution.start_verification_run(
            run.id,
            Settings(run_execution_mode="queue"),
            session_maker=maker,
            dispatcher=dispatcher,
        )
        assert dispatcher.enqueued == [run.id]
        assert run_registry.get_task(run.id) is None
        assert await _run_status(maker, run.id) == "planning"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_failed_enqueue_ends_the_run_instead_of_wedging_it(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "pytest", "max_iterations": 1}
    )
    try:
        with pytest.raises(RunQueueUnavailableError):
            await execution.start_verification_run(
                run.id,
                Settings(run_execution_mode="queue"),
                session_maker=maker,
                dispatcher=RecordingDispatcher(fail=True),
            )
        assert await _run_status(maker, run.id) == "error"
    finally:
        await _cleanup(maker, workspace, user_id)


def _ctx(maker, job_try: int = 1) -> dict:
    return {
        "settings": Settings(run_execution_mode="queue", verification_poll_seconds=0.01),
        "session_maker": maker,
        "sandbox_runtime": _passing_runtime(),
        "job_try": job_try,
    }


async def test_job_drives_a_run_to_the_gate(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "pytest", "max_iterations": 1}
    )
    try:
        assert await run_verification_job(_ctx(maker), str(run.id)) == "review"
    finally:
        await _cleanup(maker, workspace, user_id)


async def _fixing_with_active_agent(maker):
    workspace, run, user_id = await _seed_with_criteria(
        maker, "fixing", {"test_command": "pytest", "max_iterations": 2}
    )
    async with maker() as session:
        agent_run = AgentRun(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user_id,
            prompt="fix",
            status="thinking",
            provider="mock",
            model="mock",
        )
        session.add(agent_run)
        await session.flush()
        row = await session.get(VerificationRun, run.id)
        row.agent_run_id = agent_run.id
        await session.commit()
    return workspace, run, user_id


async def test_job_defers_while_the_fix_agent_run_is_active(maker) -> None:
    workspace, run, user_id = await _fixing_with_active_agent(maker)
    try:
        with pytest.raises(Retry):
            await run_verification_job(_ctx(maker), str(run.id))
        assert await _run_status(maker, run.id) == "fixing"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_job_ends_the_run_when_it_runs_out_of_tries(maker) -> None:
    workspace, run, user_id = await _fixing_with_active_agent(maker)
    ctx = _ctx(maker)
    ctx["job_try"] = verification_max_tries(ctx["settings"])
    try:
        assert await run_verification_job(ctx, str(run.id)) == "error"
        assert await _run_status(maker, run.id) == "error"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_job_on_a_vanished_run_is_a_no_op(maker) -> None:
    assert await run_verification_job(_ctx(maker), str(uuid4())) == "stale"


async def test_a_job_that_fails_before_any_step_ends_the_run(maker, monkeypatch) -> None:
    """An error outside a step (entering the loop, reading the row) must still end the run,
    or it holds the workspace's guard forever: queue mode has no startup sweep for it."""
    from app.services.verification.orchestrator import VerificationOrchestrator

    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "pytest", "max_iterations": 1}
    )

    async def broken_enter(self, workspace_id, run_id):
        raise ConnectionError("db blip")

    monkeypatch.setattr(VerificationOrchestrator, "_enter", broken_enter)
    try:
        assert await run_verification_job(_ctx(maker), str(run.id)) == "error"
        assert await _run_status(maker, run.id) == "error"
    finally:
        await _cleanup(maker, workspace, user_id)
