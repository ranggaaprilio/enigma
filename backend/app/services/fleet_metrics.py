"""Epic 09: fleet-wide gauges sampled from Postgres, and the Redis ping probe.

Workers observe what they execute; the API has no view of the fleet except through Postgres,
which is authoritative for queue depth, active runs and spend. A sampler task on each API
replica reads those aggregates every ``METRICS_SAMPLE_INTERVAL_SECONDS`` and sets gauges; a
scrape never touches the database. Per-team series are bounded: the busiest
``METRICS_TENANT_LABEL_LIMIT`` teams are labelled by id, the rest summed into ``other``, and
every series is rebuilt per sample, so the label set cannot grow without bound or go stale.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import (
    fleet_active_runs,
    fleet_cost_usd_last_hour,
    fleet_queue_depth,
    fleet_sample_timestamp_seconds,
    fleet_tokens_last_hour,
    redis_ping_errors_total,
    redis_ping_seconds,
)
from app.models import AgentRun, Workspace
from app.services.capacity import ACTIVE_RUN_STATUSES, waiting_run_counts
from app.services.redis import get_redis_client

logger = structlog.get_logger(__name__)

NO_TEAM = "none"
OTHER_TEAMS = "other"
SPEND_WINDOW = timedelta(hours=1)


def bound_teams(
    per_team: dict[UUID | None, tuple[float, ...]], limit: int
) -> dict[str, tuple[float, ...]]:
    """Map team ids to label values, keeping the ``limit`` busiest and folding the rest.

    Rank is by the tuple's components in order (active runs first). Team-less workspaces are
    always their own ``none`` series and never count against the limit.
    """
    # Ties break on the id, so equal-ranked teams never swap labels between samples.
    ranked = sorted(
        ((team, values) for team, values in per_team.items() if team is not None),
        key=lambda item: (item[1], str(item[0])),
        reverse=True,
    )
    labelled: dict[str, tuple[float, ...]] = {}
    for team, values in ranked[:limit]:
        labelled[str(team)] = values
    rest = ranked[limit:]
    if rest:
        width = len(rest[0][1])
        labelled[OTHER_TEAMS] = tuple(
            sum(values[index] for _, values in rest) for index in range(width)
        )
    if None in per_team:
        labelled[NO_TEAM] = per_team[None]
    return labelled


def active_runs_by_team():
    """``(team_id, active agent runs)`` per team; served by ``ix_agent_runs_status``."""
    return (
        select(Workspace.team_id, func.count(AgentRun.id))
        .join(Workspace, Workspace.id == AgentRun.workspace_id)
        .where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        .group_by(Workspace.team_id)
    )


def spend_by_team(since: datetime):
    """``(team_id, tokens, cost)`` of runs finished since ``since``.

    Retention windows are whole days, so nothing finished within the hour has been pruned: the
    ``events_pruned_at IS NULL`` predicate changes no result and lets the partial retention
    index (``ix_agent_runs_events_retention``) serve the scan instead of the whole table.
    ``tests/test_fleet_metrics.py`` checks the plan.
    """
    return (
        select(
            Workspace.team_id,
            func.coalesce(func.sum(AgentRun.tokens_used), 0),
            func.coalesce(func.sum(AgentRun.estimated_cost), 0),
        )
        .join(Workspace, Workspace.id == AgentRun.workspace_id)
        .where(AgentRun.completed_at >= since, AgentRun.events_pruned_at.is_(None))
        .group_by(Workspace.team_id)
    )


class FleetMetricsSampler:
    """One sample = three aggregate queries, then the gauges are rebuilt from scratch."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession], settings: Settings):
        self._session_maker = session_maker
        self._settings = settings

    async def sample_once(self) -> None:
        async with self._session_maker() as session:
            waiting_agents, waiting_commands = await waiting_run_counts(session)
            per_team = await self._per_team(session)
        teams = bound_teams(per_team, self._settings.metrics_tenant_label_limit)

        fleet_queue_depth.labels(kind="agent").set(waiting_agents)
        fleet_queue_depth.labels(kind="command").set(waiting_commands)
        for gauge in (fleet_active_runs, fleet_tokens_last_hour, fleet_cost_usd_last_hour):
            gauge.clear()
        for team, (active, tokens, cost) in teams.items():
            fleet_active_runs.labels(team=team).set(active)
            fleet_tokens_last_hour.labels(team=team).set(tokens)
            fleet_cost_usd_last_hour.labels(team=team).set(cost)
        fleet_sample_timestamp_seconds.set(time.time())

    async def _per_team(self, session: AsyncSession) -> dict[UUID | None, tuple[float, ...]]:
        """``{team_id: (active_runs, tokens_last_hour, cost_last_hour)}``; 0.0 where absent."""
        active_rows = await session.execute(active_runs_by_team())
        spend_rows = await session.execute(spend_by_team(datetime.now(UTC) - SPEND_WINDOW))
        per_team: dict[UUID | None, list[float]] = {}
        for team_id, active in active_rows:
            per_team.setdefault(team_id, [0.0, 0.0, 0.0])[0] = float(active)
        for team_id, tokens, cost in spend_rows:
            entry = per_team.setdefault(team_id, [0.0, 0.0, 0.0])
            entry[1], entry[2] = float(tokens), float(cost)
        return {team: tuple(values) for team, values in per_team.items()}

    async def run(self) -> None:
        """Sample now, then every interval, until cancelled; a failed sample keeps the last."""
        while True:
            try:
                await self.sample_once()
            except Exception:
                logger.warning("fleet_metrics_sample_failed", exc_info=True)
            try:
                await ping_redis("api")
            except Exception:
                logger.warning("redis_ping_probe_failed", exc_info=True)
            await asyncio.sleep(self._settings.metrics_sample_interval_seconds)


def start_fleet_sampler(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> asyncio.Task:
    return asyncio.create_task(FleetMetricsSampler(session_maker, settings).run())


async def ping_redis(role: str, client_factory: Callable[[], Redis] = get_redis_client) -> bool:
    """Time one PING on a fresh client; a failure counts and returns False, never raises."""
    client = client_factory()
    started = time.perf_counter()
    try:
        await client.ping()
    except Exception:
        redis_ping_errors_total.labels(role=role).inc()
        return False
    finally:
        await client.aclose()
    redis_ping_seconds.labels(role=role).observe(time.perf_counter() - started)
    return True
