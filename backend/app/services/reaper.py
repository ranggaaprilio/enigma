"""Continuously recover runs whose worker died (Epic 04).

A worker holds a lease on every run it executes (``app.services.leases``). If the worker is
killed, OOMs, or loses the database for longer than the lease, the lease runs out while the run
is still active. Every worker runs this reaper: it ends such runs as ``error`` /
``interrupted_worker_died`` -- persisting and publishing the terminal event and notifying the
user -- which also frees the workspace's single active-run slot. It generalises the startup sweep
in ``app.services.reconciliation`` into a continuous safety net.

It only ever *expires* leases: the end-the-run UPDATE re-checks ``lease_expires_at`` against
Postgres' clock, so a lease renewed after the scan is left alone, and reapers on several workers
racing over one run end it exactly once. A run with no lease (inline mode) is never touched.

It also ends queued runs that no worker can ever claim, as ``error`` / ``job_lost``: a run that
was enqueued but never claimed, whose arq job is gone from Redis (a flush, or arq dropping it
after its last try). Age alone cannot tell such a run from one waiting behind a busy pool, so
Redis decides; a run whose job is still there is left waiting however old it is, and a Redis
error skips the run rather than ending it. The UPDATE re-checks ``claimed_at IS NULL``, so a
claim racing the reaper wins.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

import structlog
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import reaper_lost_jobs_total, reaper_runs_ended_total, reaper_sweeps_total
from app.models import AgentRun, CommandRun
from app.services.agents import ACTIVE_RUN_STATUSES, AgentRuntime, AgentService, agent_runtime
from app.services.commands import CommandRuntime, CommandService, command_runtime
from app.services.jobs import JOB_LOST, agent_run_job_id, command_run_job_id, job_is_known
from app.services.leases import db_now

logger = structlog.get_logger(__name__)

INTERRUPTED_WORKER_DIED = "interrupted_worker_died"
INTERRUPTED_WORKER_DIED_MESSAGE = "The worker running this run stopped responding. Please retry."
JOB_LOST_MESSAGE = "The run was lost from the queue before a worker started it. Please retry."


@dataclass
class ReapResult:
    agent_run_ids: list[UUID] = field(default_factory=list)
    command_run_ids: list[UUID] = field(default_factory=list)


async def reap_expired_leases(
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    agent_runtime: AgentRuntime = agent_runtime,
    command_runtime: CommandRuntime = command_runtime,
) -> ReapResult:
    """End every active run whose lease has expired; returns the runs this call ended."""
    result = ReapResult()
    # Scan in a short session of its own, so it doesn't sit idle-in-transaction while each run
    # is ended on its own connection.
    async with session_maker() as scan:
        agent_ids = (
            await scan.scalars(
                select(AgentRun.id).where(
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                    AgentRun.lease_expires_at < db_now(),
                )
            )
        ).all()
        command_ids = (
            await scan.scalars(
                select(CommandRun.id).where(
                    CommandRun.status == "running",
                    CommandRun.lease_expires_at < db_now(),
                )
            )
        ).all()

    async with session_maker() as session:
        agents = AgentService(session, session_maker, settings, runtime=agent_runtime)
        commands = CommandService(session, session_maker, settings, runtime=command_runtime)

        for run_id in agent_ids:
            if await _reap(agents, run_id):
                result.agent_run_ids.append(run_id)
                reaper_runs_ended_total.labels(kind="agent").inc()
        for run_id in command_ids:
            if await _reap(commands, run_id):
                result.command_run_ids.append(run_id)
                reaper_runs_ended_total.labels(kind="command").inc()

    if result.agent_run_ids or result.command_run_ids:
        logger.warning(
            "expired_run_leases_reaped",
            agent_runs=[str(run_id) for run_id in result.agent_run_ids],
            command_runs=[str(run_id) for run_id in result.command_run_ids],
        )
    return result


async def _reap(service: AgentService | CommandService, run_id: UUID) -> bool:
    # One run failing to close out must not keep the others wedged until the next pass.
    try:
        return await service.fail_active_run(
            run_id, INTERRUPTED_WORKER_DIED, INTERRUPTED_WORKER_DIED_MESSAGE, lease_expired=True
        )
    except Exception:
        logger.exception("run_reap_failed", run_id=str(run_id))
        return False


async def reap_lost_jobs(
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: ArqRedis,
    *,
    agent_runtime: AgentRuntime = agent_runtime,
    command_runtime: CommandRuntime = command_runtime,
) -> ReapResult:
    """End every queued, never-claimed run whose job Redis no longer holds."""
    result = ReapResult()
    async with session_maker() as scan:
        # `enqueued_at` is the API's clock; the grace dwarfs any skew.
        cutoff = db_now() - timedelta(seconds=settings.run_unclaimed_grace_seconds)
        agent_ids = (
            await scan.scalars(
                select(AgentRun.id).where(
                    AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                    AgentRun.claimed_at.is_(None),
                    AgentRun.enqueued_at < cutoff,
                )
            )
        ).all()
        command_ids = (
            await scan.scalars(
                select(CommandRun.id).where(
                    CommandRun.status == "running",
                    CommandRun.claimed_at.is_(None),
                    CommandRun.enqueued_at < cutoff,
                )
            )
        ).all()

    async with session_maker() as session:
        agents = AgentService(session, session_maker, settings, runtime=agent_runtime)
        commands = CommandService(session, session_maker, settings, runtime=command_runtime)

        for run_id in agent_ids:
            if await _reap_if_lost(agents, redis, settings, agent_run_job_id(run_id), run_id):
                result.agent_run_ids.append(run_id)
                reaper_lost_jobs_total.labels(kind="agent").inc()
        for run_id in command_ids:
            if await _reap_if_lost(commands, redis, settings, command_run_job_id(run_id), run_id):
                result.command_run_ids.append(run_id)
                reaper_lost_jobs_total.labels(kind="command").inc()

    if result.agent_run_ids or result.command_run_ids:
        logger.warning(
            "lost_run_jobs_reaped",
            agent_runs=[str(run_id) for run_id in result.agent_run_ids],
            command_runs=[str(run_id) for run_id in result.command_run_ids],
        )
    return result


async def _reap_if_lost(
    service: AgentService | CommandService,
    redis: ArqRedis,
    settings: Settings,
    job_id: str,
    run_id: UUID,
) -> bool:
    # A Redis error is not evidence of a loss: skip the run until the next pass.
    try:
        if await job_is_known(redis, settings.run_queue_name, job_id):
            return False
        return await service.fail_active_run(run_id, JOB_LOST, JOB_LOST_MESSAGE, unclaimed=True)
    except Exception:
        logger.exception("lost_run_reap_failed", run_id=str(run_id))
        return False


async def run_reaper(
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    redis: ArqRedis | None = None,
    agent_runtime: AgentRuntime = agent_runtime,
    command_runtime: CommandRuntime = command_runtime,
) -> None:
    """Reap now, then every ``run_reaper_interval_seconds``, until cancelled.

    Lost jobs are reaped only when given the queue's ``redis``.
    """
    while True:
        try:
            await reap_expired_leases(
                session_maker,
                settings,
                agent_runtime=agent_runtime,
                command_runtime=command_runtime,
            )
            if redis is not None:
                await reap_lost_jobs(
                    session_maker,
                    settings,
                    redis,
                    agent_runtime=agent_runtime,
                    command_runtime=command_runtime,
                )
            reaper_sweeps_total.labels(result="ok").inc()
        except Exception:
            reaper_sweeps_total.labels(result="error").inc()
            logger.exception("run_reaper_pass_failed")
        await asyncio.sleep(settings.run_reaper_interval_seconds)
