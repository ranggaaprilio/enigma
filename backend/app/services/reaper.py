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
"""

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import reaper_runs_ended_total, reaper_sweeps_total
from app.models import AgentRun, CommandRun
from app.services.agents import ACTIVE_RUN_STATUSES, AgentRuntime, AgentService, agent_runtime
from app.services.commands import CommandRuntime, CommandService, command_runtime
from app.services.leases import db_now

logger = structlog.get_logger(__name__)

INTERRUPTED_WORKER_DIED = "interrupted_worker_died"
INTERRUPTED_WORKER_DIED_MESSAGE = "The worker running this run stopped responding. Please retry."


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


async def run_reaper(
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    agent_runtime: AgentRuntime = agent_runtime,
    command_runtime: CommandRuntime = command_runtime,
) -> None:
    """Reap now, then every ``run_reaper_interval_seconds``, until cancelled."""
    while True:
        try:
            await reap_expired_leases(
                session_maker,
                settings,
                agent_runtime=agent_runtime,
                command_runtime=command_runtime,
            )
            reaper_sweeps_total.labels(result="ok").inc()
        except Exception:
            reaper_sweeps_total.labels(result="error").inc()
            logger.exception("run_reaper_pass_failed")
        await asyncio.sleep(settings.run_reaper_interval_seconds)
