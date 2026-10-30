"""Run-execution worker (Epic 02): ``uv run arq app.worker.WorkerSettings``.

The API enqueues a run id (``app.services.jobs``); a worker claims the run in Postgres and
executes it with the same service code the inline path uses. Delivery is at-least-once, execution
at-most-once: only the first delivery wins the conditional claim, and a later delivery of a run
that was claimed but never finished means its worker died, so it is ended rather than re-run.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import structlog
from arq.worker import Retry, func
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.lifecycle import lifecycle
from app.core.logging import configure_logging
from app.core.tracing import configure_tracing, run_span, shutdown_tracing
from app.db.session import async_session_maker, engine
from app.models import AgentRun, CommandRun, SpecPhase, SpecWorkflow
from app.services.agents import ACTIVE_RUN_STATUSES, AgentService, agent_runtime
from app.services.commands import CommandService, command_runtime
from app.services.jobs import (
    AGENT_RUN_JOB,
    COMMAND_RUN_JOB,
    DEAD_LETTER,
    JOB_TIMEOUT,
    SPEC_FINALIZE_JOB,
    WORKER_ERROR,
    WORKER_LOST,
    ArqDispatcher,
    arq_redis_settings,
    deserialize_job_payload,
    serialize_job_payload,
    set_run_dispatcher,
)
from app.services.leases import heartbeat
from app.services.reaper import run_reaper
from app.services.reconciliation import reconcile_interrupted_runs
from app.services.retention import start_retention
from app.services.runtime import RedisEventBus, event_bus
from app.services.sandbox import close_all
from app.services.sandbox_maintenance import start_sandbox_maintenance
from app.services.speckit import (
    _TERMINAL_RUN_STATUSES,
    PHASE_FINALIZE_TIMEOUT_SECONDS,
    SpecKitService,
)
from app.worker_metrics import run_redis_probe, start_if_possible

logger = structlog.get_logger(__name__)

WORKER_LOST_MESSAGE = "The worker running this run stopped before it finished."
DEAD_LETTER_MESSAGE = "The run could not be started after repeated attempts."
WORKER_ERROR_MESSAGE = "The worker failed while running this command."
# arq only retries a job that raises Retry (or is cancelled); any other exception drops it.
CLAIM_RETRY_SECONDS = 5.0
# A running agent job re-reads its run this often, so a stop whose control message was lost
# (the bus is at-most-once) still halts the agent.
RUN_STATUS_POLL_SECONDS = 5.0
# Command runs: the sandbox enforces the command's own timeout; this margin is the handler's.
COMMAND_TIMEOUT_MARGIN_SECONDS = 60
# Extra finalizer tries beyond its polling budget, so a redelivery after a worker crash (which
# also counts as a try) still reaches the handler instead of arq failing the job silently.
FINALIZE_RETRY_SLACK = 5


async def _claim(claim, fail, *, job_try: int, settings: Settings, log) -> Any | None:
    """Run ``claim()``; on error retry the job, or dead-letter the run on the last try.

    Returns ``None`` once the run was dead-lettered.
    """
    try:
        return await claim()
    except Exception:
        if job_try < settings.worker_max_tries:
            log.warning("job_claim_failed_retrying", exc_info=True)
            raise Retry(defer=CLAIM_RETRY_SECONDS) from None
        log.exception("job_dead_letter")
        try:
            await fail(DEAD_LETTER, DEAD_LETTER_MESSAGE)
        except Exception:
            log.exception("job_dead_letter_record_failed")
        return None


@asynccontextmanager
async def _holding_lease(
    renew: Callable[[], Awaitable[bool]],
    settings: Settings,
    on_lost: Callable[[], None] | None = None,
) -> AsyncIterator[None]:
    """Keep renewing the run's lease (taken at claim) while the job executes it."""
    beat = asyncio.create_task(
        heartbeat(renew, interval=settings.run_heartbeat_seconds, on_lost=on_lost)
    )
    try:
        yield
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)


async def _status(session_maker, model, run_id: UUID) -> str | None:
    async with session_maker() as session:
        return await session.scalar(select(model.status).where(model.id == run_id))


async def _correlation(session_maker, model, run_id: UUID) -> dict[str, str]:
    """The request id (and trace context) stamped on the run at creation (Epic 09).

    Bound into the job's log context so a run reads as one story across API and worker. The
    job payload carries only the run id, so this comes off the row after the claim.
    """
    async with session_maker() as session:
        row = (
            await session.execute(
                select(model.request_id, model.traceparent).where(model.id == run_id)
            )
        ).first()
    found: dict[str, str] = {}
    if row is not None and row.request_id:
        found["request_id"] = row.request_id
    if row is not None and row.traceparent:
        found["traceparent"] = row.traceparent
    return found


async def _stop_when_inactive(session_maker, runtime, run_id: UUID) -> None:
    while True:
        await asyncio.sleep(RUN_STATUS_POLL_SECONDS)
        try:
            status = await _status(session_maker, AgentRun, run_id)
        except Exception:
            logger.warning("run_status_poll_failed", run_id=str(run_id), exc_info=True)
            continue
        if status not in ACTIVE_RUN_STATUSES:
            runtime.request_stop(run_id)
            return


async def run_agent_job(ctx: dict[str, Any], run_id: str) -> str:
    settings: Settings = ctx["settings"]
    session_maker = ctx["session_maker"]
    runtime = ctx["agent_runtime"]
    job_try: int = ctx.get("job_try", 1)
    run_uuid = UUID(run_id)
    log = logger.bind(run_id=run_id, job_try=job_try, worker_id=settings.worker_id)

    async with session_maker() as session:
        service = AgentService(
            session, session_maker, settings, runtime=runtime, leave_active_on_cancel=True
        )
        claim = await _claim(
            lambda: service.claim_run(run_uuid, settings.worker_id, job_try),
            lambda reason, message: service.fail_active_run(run_uuid, reason, message),
            job_try=job_try,
            settings=settings,
            log=log,
        )
        if claim is None:
            return DEAD_LETTER
        if claim.outcome == "already_claimed":
            # A live lease means the first delivery is still running it: leave it be.
            if await service.fail_active_run(
                run_uuid, WORKER_LOST, WORKER_LOST_MESSAGE, lease_expired=True
            ):
                log.warning("agent_job_worker_lost")
                return WORKER_LOST
            log.info("agent_job_redelivered_while_running")
            return "already_claimed"
        if claim.outcome != "claimed":
            log.info("agent_job_skipped", outcome=claim.outcome)
            return claim.outcome

        correlation = await _correlation(session_maker, AgentRun, run_uuid)
        traceparent = correlation.pop("traceparent", None)
        structlog.contextvars.bind_contextvars(**correlation)
        log = log.bind(**correlation)
        with run_span(
            "agent_run", traceparent, run_id=run_id, worker_id=settings.worker_id, job_try=job_try
        ):
            stop_event = asyncio.Event()
            runtime.register_run(run_uuid, asyncio.current_task(), stop_event)
            try:
                # A lost lease means the reaper already ended the run: stop executing it.
                async with _holding_lease(
                    lambda: service.renew_lease(run_uuid, settings.worker_id),
                    settings,
                    on_lost=lambda: runtime.request_stop(run_uuid),
                ), runtime.watch_control(run_uuid):
                    # Stopped between the claim and the watch: that signal never reached us.
                    status = await _status(session_maker, AgentRun, run_uuid)
                    if status not in ACTIVE_RUN_STATUSES:
                        return status or "missing"
                    poller = asyncio.create_task(
                        _stop_when_inactive(session_maker, runtime, run_uuid)
                    )
                    try:
                        return await service.execute_claimed_run(
                            run_uuid,
                            claim.working_dir,
                            stop_event,
                            settings.agent_run_timeout_seconds,
                        )
                    finally:
                        poller.cancel()
                        await asyncio.gather(poller, return_exceptions=True)
            finally:
                runtime.forget_run(run_uuid)
                structlog.contextvars.unbind_contextvars(*correlation)


async def run_command_job(ctx: dict[str, Any], run_id: str) -> str:
    settings: Settings = ctx["settings"]
    session_maker = ctx["session_maker"]
    job_try: int = ctx.get("job_try", 1)
    run_uuid = UUID(run_id)
    log = logger.bind(command_run_id=run_id, job_try=job_try, worker_id=settings.worker_id)

    async with session_maker() as session:
        service = CommandService(
            session,
            session_maker,
            settings,
            runtime=ctx["command_runtime"],
            sandbox_runtime=ctx.get("sandbox_runtime"),
        )
        outcome = await _claim(
            lambda: service.claim_run(run_uuid, settings.worker_id, job_try),
            lambda reason, message: service.fail_active_run(run_uuid, reason, message),
            job_try=job_try,
            settings=settings,
            log=log,
        )
        if outcome is None:
            return DEAD_LETTER
        if outcome == "already_claimed":
            # A live lease means the first delivery is still running it: leave it be.
            if await service.fail_active_run(
                run_uuid, WORKER_LOST, WORKER_LOST_MESSAGE, lease_expired=True
            ):
                log.warning("command_job_worker_lost")
                return WORKER_LOST
            log.info("command_job_redelivered_while_running")
            return "already_claimed"
        if outcome != "claimed":
            log.info("command_job_skipped", outcome=outcome)
            return outcome

        correlation = await _correlation(session_maker, CommandRun, run_uuid)
        traceparent = correlation.pop("traceparent", None)
        structlog.contextvars.bind_contextvars(**correlation)
        log = log.bind(**correlation)
        # Confirmation was enforced when the API accepted the run. arq treats any exception as
        # final, so whatever escapes is recorded on the run here rather than leaving it running.
        with run_span(
            "command_run", traceparent, run_id=run_id, worker_id=settings.worker_id, job_try=job_try
        ):
            timeout = settings.command_run_max_timeout_seconds + COMMAND_TIMEOUT_MARGIN_SECONDS
            try:
                # Commands have no stop signal: a lost lease (the run was reaped) cancels the job,
                # and arq's redelivery then finds the run terminal.
                job = asyncio.current_task()
                async with (
                    _holding_lease(
                        lambda: service.renew_lease(run_uuid, settings.worker_id),
                        settings,
                        on_lost=lambda: job.cancel(),
                    ),
                    asyncio.timeout(timeout),
                ):
                    await service._execute_run(run_uuid, confirmed=True)
            except TimeoutError:
                await service.fail_active_run(
                    run_uuid, JOB_TIMEOUT, f"The command exceeded its {timeout}s time limit."
                )
                return JOB_TIMEOUT
            except Exception:
                log.exception("command_job_failed")
                await service.fail_active_run(run_uuid, WORKER_ERROR, WORKER_ERROR_MESSAGE)
                return WORKER_ERROR
            finally:
                structlog.contextvars.unbind_contextvars(*correlation)
    return await _status(session_maker, CommandRun, run_uuid) or "missing"


def spec_finalize_max_tries(settings: Settings) -> int:
    """The polls covering the inline finalizer's wait, the finalizing try, and crash slack."""
    polls = math.ceil(PHASE_FINALIZE_TIMEOUT_SECONDS / settings.spec_finalize_poll_seconds)
    return polls + 1 + FINALIZE_RETRY_SLACK


async def finalize_spec_phase_job(ctx: dict[str, Any], phase_id: str, run_id: str) -> str:
    """Advance a Spec Kit phase once its agent run is terminal.

    Polls by re-queueing itself (``Retry``) rather than sleeping, so a phase waiting on a
    two-hour run never holds a worker slot.
    """
    settings: Settings = ctx["settings"]
    session_maker = ctx["session_maker"]
    job_try: int = ctx.get("job_try", 1)
    phase_uuid, run_uuid = UUID(phase_id), UUID(run_id)

    async with session_maker() as read:
        phase = await read.get(SpecPhase, phase_uuid)
        # Finalized already, or re-run since with a different agent run: nothing to do.
        if phase is None or phase.status != "running" or phase.agent_run_id != run_uuid:
            return "stale"
        workflow = await read.get(SpecWorkflow, phase.workflow_id)
        if workflow is None:
            return "stale"
        run = await read.get(AgentRun, run_uuid)
        run_status = run.status if run is not None else "error"
        user_id = run.user_id if run is not None else workflow.user_id
        workflow_id, phase_name = workflow.id, phase.phase
        correlation = {"request_id": run.request_id} if run and run.request_id else {}

    # A fresh session: finalizing runs git in the sandbox, too long to hold the read open.
    with structlog.contextvars.bound_contextvars(**correlation):
        async with session_maker() as session:
            service = SpecKitService(
                session,
                session_maker,
                settings,
                runtime=ctx.get("sandbox_runtime"),
                dispatcher=ctx.get("dispatcher"),
            )
            if run_status not in _TERMINAL_RUN_STATUSES:
                if job_try < spec_finalize_max_tries(settings) - FINALIZE_RETRY_SLACK:
                    raise Retry(defer=settings.spec_finalize_poll_seconds)
                await service._mark_phase_failed(
                    workflow_id, phase_name, user_id, "its agent run did not finish in time"
                )
                return "error"
            await service._finalize_phase(
                workflow_id, phase_name, run_uuid, user_id, run_status=run_status
            )
    return await _status(session_maker, SpecPhase, phase_uuid) or "stale"


# --- worker process -----------------------------------------------------------------------


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    settings = get_settings()
    ctx["tracing"] = configure_tracing(settings, "enigma-worker", instance_id=settings.worker_id)
    if settings.run_execution_mode != "queue":
        # Inline mode keeps the in-memory bus, so nothing this worker published would ever
        # reach an API WebSocket.
        raise RuntimeError("app.worker requires RUN_EXECUTION_MODE=queue")
    ctx["settings"] = settings
    ctx["session_maker"] = async_session_maker
    ctx["agent_runtime"] = agent_runtime
    ctx["command_runtime"] = command_runtime
    # Jobs that enqueue further work (the Spec Kit auto-chain) reuse arq's own pool.
    set_run_dispatcher(ArqDispatcher(settings, pool=ctx["redis"]))
    # Runs this worker identity claimed in a previous life (crash, OOM, kill -9) have no owner.
    await reconcile_interrupted_runs(async_session_maker, worker_id=settings.worker_id)
    # Runs any *other* worker left behind are found by their expired leases (Epic 04); runs
    # whose job vanished from the queue before any claim, by asking arq's Redis.
    ctx["reaper"] = asyncio.create_task(
        run_reaper(async_session_maker, settings, redis=ctx["redis"])
    )
    # Epic 05: every worker prunes; SKIP LOCKED batches split the work between them.
    ctx["retention"] = start_retention(async_session_maker, settings)
    ctx["sandbox_upkeep"] = start_sandbox_maintenance(async_session_maker, settings)
    # Epic 09: this worker's run/LLM/reaper metrics on METRICS_PORT, plus its Redis probe.
    ctx["metrics_server"] = start_if_possible(settings)
    ctx["redis_probe"] = asyncio.create_task(run_redis_probe(settings))
    logger.info("worker_started", worker_id=settings.worker_id, queue=settings.run_queue_name)


async def shutdown(ctx: dict[str, Any]) -> None:
    settings: Settings = ctx.get("settings") or get_settings()
    lifecycle.begin_drain()
    for name in ("reaper", "retention", "sandbox_upkeep", "redis_probe"):
        if (task := ctx.get(name)) is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    try:
        # arq has already waited `job_completion_wait` and cancelled what was still running;
        # cancelled runs were left active, so record them as interrupted now rather than when
        # (if ever) this worker id comes back. arq re-queues the jobs, and the redelivery finds
        # the runs terminal.
        await reconcile_interrupted_runs(async_session_maker, worker_id=settings.worker_id)
    except Exception:
        logger.exception("worker_shutdown_reconcile_failed")
    finally:
        set_run_dispatcher(None)  # the pool is arq's; it closes it
        await close_all()
        if isinstance(event_bus, RedisEventBus):
            await event_bus.aclose()
        await engine.dispose()
        # Last, so the drained runs' final values are still scrapable and their spans are
        # exported; both block, so they run off the loop.
        if (metrics_server := ctx.get("metrics_server")) is not None:
            await asyncio.to_thread(metrics_server.stop)
        await asyncio.to_thread(shutdown_tracing, ctx.get("tracing"))


_settings = get_settings()


class WorkerSettings:
    functions = [
        # keep_result=0: results are never read, and the run row is the record anyway.
        func(
            run_agent_job,
            name=AGENT_RUN_JOB,
            timeout=_settings.worker_job_timeout_seconds,
            keep_result=0,
        ),
        func(
            run_command_job,
            name=COMMAND_RUN_JOB,
            # The sandbox enforces the command's own timeout; this is only the backstop.
            timeout=_settings.command_run_max_timeout_seconds + 5 * 60,
            keep_result=0,
        ),
        func(
            finalize_spec_phase_job,
            name=SPEC_FINALIZE_JOB,
            max_tries=spec_finalize_max_tries(_settings),
            keep_result=0,
        ),
    ]
    redis_settings = arq_redis_settings(_settings)
    job_serializer = serialize_job_payload
    job_deserializer = deserialize_job_payload
    queue_name = _settings.run_queue_name
    max_jobs = _settings.worker_max_jobs
    max_tries = _settings.worker_max_tries
    job_timeout = _settings.worker_job_timeout_seconds
    # On SIGTERM stop taking jobs and give running ones this long before cancelling them.
    job_completion_wait = int(_settings.shutdown_run_grace_seconds)
    on_startup = startup
    on_shutdown = shutdown
