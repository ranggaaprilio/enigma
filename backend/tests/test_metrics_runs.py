"""Epic 09: run-lifecycle, LLM, lease and reaper metrics observed where runs execute."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from prometheus_client import REGISTRY

from app.services.leases import heartbeat
from app.services.reaper import run_reaper
from app.worker import run_agent_job
from tests.test_job_queue import db_session, queue_settings
from tests.test_run_leases import reap, runtimes
from tests.test_worker_jobs import make_ctx, seed_command_run, seed_run

__all__ = ["db_session"]


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


async def test_a_finished_agent_run_is_counted_timed_and_costed(db_session):
    session, session_maker = db_session
    run = await seed_run(session, enqueued_at=ago(2))
    finished = sample("runs_finished_total", kind="agent", status="completed", reason="none")
    duration = sample("run_duration_seconds_count", kind="agent", status="completed")
    queue_wait = sample("run_queue_wait_seconds_count", kind="agent")
    start = sample("run_start_latency_seconds_count", kind="agent")
    llm = sample("llm_request_seconds_count", provider="mock", model="mock-agent")
    ttft = sample("llm_time_to_first_token_seconds_count", provider="mock", model="mock-agent")
    tokens_in = sample("llm_tokens_total", provider="mock", model="mock-agent", kind="input")
    tokens_out = sample("llm_tokens_total", provider="mock", model="mock-agent", kind="output")

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "completed"

    await session.refresh(run)
    assert sample("runs_finished_total", kind="agent", status="completed", reason="none") == (
        finished + 1
    )
    assert sample("run_duration_seconds_count", kind="agent", status="completed") == duration + 1
    assert sample("run_queue_wait_seconds_count", kind="agent") == queue_wait + 1
    # The wait is measured from the seeded enqueue, so it is at least those two seconds.
    assert sample("run_queue_wait_seconds_sum", kind="agent") >= 2.0
    assert sample("run_start_latency_seconds_count", kind="agent") == start + 1
    assert sample("llm_request_seconds_count", provider="mock", model="mock-agent") == llm + 1
    assert (
        sample("llm_time_to_first_token_seconds_count", provider="mock", model="mock-agent")
        == ttft + 1
    )
    assert (
        sample("llm_tokens_total", provider="mock", model="mock-agent", kind="input")
        == tokens_in + run.input_tokens
    )
    assert (
        sample("llm_tokens_total", provider="mock", model="mock-agent", kind="output")
        == tokens_out + run.output_tokens
    )


async def test_an_inline_run_observes_no_queue_wait(db_session):
    """Inline runs were never enqueued: the queue-wait and start-latency series stay put."""
    session, session_maker = db_session
    run = await seed_run(session)  # no enqueued_at
    queue_wait = sample("run_queue_wait_seconds_count", kind="agent")
    start = sample("run_start_latency_seconds_count", kind="agent")

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "completed"

    assert sample("run_queue_wait_seconds_count", kind="agent") == queue_wait
    assert sample("run_start_latency_seconds_count", kind="agent") == start


async def test_reaped_runs_are_counted_by_kind(db_session):
    session, session_maker = db_session
    await seed_run(
        session, status="executing", claimed_at=ago(120), worker_id="dead", lease_expires_at=ago(30)
    )
    await seed_command_run(session, worker_id="dead", lease_expires_at=ago(30))
    agents = sample("reaper_runs_ended_total", kind="agent")
    commands = sample("reaper_runs_ended_total", kind="command")
    died = {"kind": "agent", "status": "error", "reason": "interrupted_worker_died"}
    errored = sample("runs_finished_total", **died)

    result = await reap(session_maker)

    assert len(result.agent_run_ids) >= 1 and len(result.command_run_ids) >= 1
    assert sample("reaper_runs_ended_total", kind="agent") == agents + len(result.agent_run_ids)
    assert sample("reaper_runs_ended_total", kind="command") == commands + len(
        result.command_run_ids
    )
    assert sample("runs_finished_total", **died) == errored + len(result.agent_run_ids)


async def test_reaper_sweeps_are_counted(db_session):
    _, session_maker = db_session
    agent_rt, command_rt = runtimes()
    ok = sample("reaper_sweeps_total", result="ok")
    task = asyncio.create_task(
        run_reaper(
            session_maker,
            queue_settings(run_reaper_interval_seconds=60),
            agent_runtime=agent_rt,
            command_runtime=command_rt,
        )
    )
    try:
        for _ in range(100):
            if sample("reaper_sweeps_total", result="ok") > ok:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert sample("reaper_sweeps_total", result="ok") == ok + 1


async def test_a_lost_lease_is_counted():
    lost = sample("run_lease_lost_total")
    calls: list[str] = []

    async def renew() -> bool:
        return False

    await heartbeat(renew, interval=0.001, on_lost=lambda: calls.append("lost"))

    assert calls == ["lost"]
    assert sample("run_lease_lost_total") == lost + 1
