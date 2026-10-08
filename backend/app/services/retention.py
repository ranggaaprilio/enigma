"""Bounded growth for the highest-volume tables (Epic 05).

``agent_events`` gains a row for every status/output/tool event of every run, ``command_runs``
keeps each run's full log in ``output`` and ``notifications`` is append-only, so each grows
without bound. A retention pass, off unless a window is configured, removes what is older than
its window:

- the events of finished agent runs, stamping ``agent_runs.events_pruned_at``. The run row
  stays, and so do ``tokens_used`` / ``estimated_cost``, which usage and budgets read;
- the ``output`` of finished command runs, stamping ``command_runs.output_pruned_at``;
- notifications read longer ago than the read window, and any older than the hard window --
  except budget warnings, which ``BudgetService._already_warned`` de-duplicates against.

Active runs are never touched. Work goes in small batches, one transaction each, and the scans
lock their rows ``FOR UPDATE SKIP LOCKED``, so passes on several replicas split the work instead
of colliding; no lock needs to outlive a transaction, which keeps it PgBouncer-safe.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import AgentEvent, AgentRun, CommandRun, Notification
from app.services.agents import ACTIVE_RUN_STATUSES

logger = structlog.get_logger(__name__)


@dataclass
class RetentionResult:
    agent_runs: int = 0
    command_runs: int = 0
    notifications: int = 0


def retention_enabled(settings: Settings) -> bool:
    return any(
        (
            settings.retention_agent_events_days,
            settings.retention_command_output_days,
            settings.retention_read_notifications_days,
            settings.retention_notifications_days,
        )
    )


def _cutoff(days: int) -> datetime | None:
    return datetime.now(UTC) - timedelta(days=days) if days else None


def expired_agent_runs(cutoff: datetime, limit: int) -> Select:
    return (
        select(AgentRun.id)
        .where(
            AgentRun.events_pruned_at.is_(None),
            AgentRun.completed_at.is_not(None),
            AgentRun.completed_at < cutoff,
            AgentRun.status.not_in(ACTIVE_RUN_STATUSES),
        )
        .order_by(AgentRun.completed_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def expired_command_runs(cutoff: datetime, limit: int) -> Select:
    return (
        select(CommandRun.id)
        .where(
            CommandRun.output_pruned_at.is_(None),
            CommandRun.completed_at.is_not(None),
            CommandRun.completed_at < cutoff,
            CommandRun.status != "running",
        )
        .order_by(CommandRun.completed_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


def expired_notifications(
    read_cutoff: datetime | None, hard_cutoff: datetime | None, limit: int
) -> Select:
    conditions = []
    if read_cutoff is not None:
        conditions.append(
            and_(Notification.read_at.is_not(None), Notification.read_at < read_cutoff)
        )
    if hard_cutoff is not None:
        conditions.append(Notification.created_at < hard_cutoff)
    # Nothing is read before it exists, so both conditions imply `created_at` is before the
    # later cutoff -- a range the created_at index can serve.
    newest = max(cutoff for cutoff in (read_cutoff, hard_cutoff) if cutoff is not None)
    return (
        select(Notification.id)
        .where(
            Notification.created_at < newest,
            # The record budget warnings de-duplicate against (forever, for a "total" budget).
            Notification.notification_type != "budget_warning",
            or_(*conditions),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def _in_batches(
    session_maker: async_sessionmaker[AsyncSession],
    batch_size: int,
    scan: Callable[[int], Select],
    prune: Callable[[AsyncSession, Sequence[UUID]], Awaitable[None]],
) -> int:
    total = 0
    while True:
        async with session_maker() as session:
            ids = (await session.scalars(scan(batch_size))).all()
            if ids:
                await prune(session, ids)
                await session.commit()
        total += len(ids)
        if len(ids) < batch_size:
            return total


async def prune_agent_events(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Delete the events of finished runs older than the window; returns the runs pruned."""
    cutoff = _cutoff(settings.retention_agent_events_days)
    if cutoff is None:
        return 0

    async def prune(session: AsyncSession, run_ids: Sequence[UUID]) -> None:
        await session.execute(delete(AgentEvent).where(AgentEvent.agent_run_id.in_(run_ids)))
        await session.execute(
            update(AgentRun).where(AgentRun.id.in_(run_ids)).values(events_pruned_at=func.now())
        )

    return await _in_batches(
        session_maker,
        settings.retention_batch_size,
        lambda limit: expired_agent_runs(cutoff, limit),
        prune,
    )


async def prune_command_output(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Empty the output of finished command runs older than the window; returns runs pruned."""
    cutoff = _cutoff(settings.retention_command_output_days)
    if cutoff is None:
        return 0

    async def prune(session: AsyncSession, run_ids: Sequence[UUID]) -> None:
        await session.execute(
            update(CommandRun)
            .where(CommandRun.id.in_(run_ids))
            .values(output="", output_pruned_at=func.now())
        )

    return await _in_batches(
        session_maker,
        settings.retention_batch_size,
        lambda limit: expired_command_runs(cutoff, limit),
        prune,
    )


async def prune_notifications(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """Delete notifications past either window; returns how many were deleted."""
    read_cutoff = _cutoff(settings.retention_read_notifications_days)
    hard_cutoff = _cutoff(settings.retention_notifications_days)
    if read_cutoff is None and hard_cutoff is None:
        return 0

    async def prune(session: AsyncSession, ids: Sequence[UUID]) -> None:
        await session.execute(delete(Notification).where(Notification.id.in_(ids)))

    return await _in_batches(
        session_maker,
        settings.retention_batch_size,
        lambda limit: expired_notifications(read_cutoff, hard_cutoff, limit),
        prune,
    )


async def run_retention_pass(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> RetentionResult:
    result = RetentionResult(
        agent_runs=await prune_agent_events(session_maker, settings),
        command_runs=await prune_command_output(session_maker, settings),
        notifications=await prune_notifications(session_maker, settings),
    )
    if result.agent_runs or result.command_runs or result.notifications:
        logger.info(
            "retention_pass_pruned",
            agent_runs=result.agent_runs,
            command_runs=result.command_runs,
            notifications=result.notifications,
        )
    return result


async def run_retention(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Prune now, then every ``retention_interval_seconds``, until cancelled."""
    while True:
        try:
            await run_retention_pass(session_maker, settings)
        except Exception:
            logger.exception("retention_pass_failed")
        await asyncio.sleep(settings.retention_interval_seconds)


def start_retention(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> asyncio.Task | None:
    """Start the retention loop in the background, or nothing when no window is configured."""
    if not retention_enabled(settings):
        return None
    return asyncio.create_task(run_retention(session_maker, settings))
