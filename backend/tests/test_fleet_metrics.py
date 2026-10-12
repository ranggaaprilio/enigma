"""Epic 09: fleet gauges sampled from Postgres with a bounded per-team label set."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import fakeredis
from prometheus_client import REGISTRY
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.metrics import DbPoolCollector
from app.models import AgentRun
from app.services.fleet_metrics import (
    NO_TEAM,
    OTHER_TEAMS,
    FleetMetricsSampler,
    bound_teams,
    ping_redis,
    spend_by_team,
)
from tests.test_job_queue import db_session, make_run, queue_settings
from tests.test_run_capacity import make_team, seed_workspace
from tests.test_worker_jobs import seed_command_run

__all__ = ["db_session"]


def sample(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def test_bound_teams_keeps_the_busiest_and_folds_the_rest() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    per_team = {a: (3.0, 10.0, 0.5), b: (1.0, 50.0, 2.0), c: (2.0, 0.0, 0.0), None: (1.0, 0.0, 0.0)}

    assert bound_teams(per_team, 2) == {
        str(a): (3.0, 10.0, 0.5),
        str(c): (2.0, 0.0, 0.0),
        OTHER_TEAMS: (1.0, 50.0, 2.0),
        NO_TEAM: (1.0, 0.0, 0.0),
    }
    assert OTHER_TEAMS not in bound_teams(per_team, 3)
    assert set(bound_teams(per_team, 0)) == {OTHER_TEAMS, NO_TEAM}
    assert bound_teams({}, 5) == {}


async def test_sampler_labels_the_busiest_teams_and_rebuilds_series_each_pass(db_session):
    session, session_maker = db_session
    owner, _ = await seed_workspace(session)
    busy, quiet, overflow = [await make_team(session, owner) for _ in range(3)]
    # Two active runs for `busy` need two workspaces (one active run per workspace).
    busy_runs = []
    for team, count in ((busy, 2), (quiet, 1), (overflow, 1)):
        for _ in range(count):
            user, workspace = await seed_workspace(session, team)
            run = make_run(user, workspace, "executing")
            session.add(run)
            if team is busy:
                busy_runs.append(run)
    teamless_user, teamless_workspace = await seed_workspace(session)
    session.add(make_run(teamless_user, teamless_workspace, "thinking"))
    spender, spender_workspace = await seed_workspace(session, quiet)
    spent = make_run(spender, spender_workspace, "completed")
    spent.completed_at = datetime.now(UTC)
    spent.tokens_used = 1234
    spent.estimated_cost = Decimal("0.25")
    session.add(spent)
    await session.commit()

    sampler = FleetMetricsSampler(session_maker, queue_settings(metrics_tenant_label_limit=2))
    await sampler.sample_once()

    assert sample("fleet_active_runs", team=str(busy.id)) == 2
    assert sample("fleet_active_runs", team=str(quiet.id)) == 1
    assert sample("fleet_active_runs", team=str(overflow.id)) is None
    assert sample("fleet_active_runs", team=OTHER_TEAMS) >= 1
    assert sample("fleet_active_runs", team=NO_TEAM) >= 1
    assert sample("fleet_tokens_last_hour", team=str(quiet.id)) == 1234
    assert sample("fleet_cost_usd_last_hour", team=str(quiet.id)) == 0.25
    assert sample("fleet_sample_timestamp_seconds") > 0

    # The busy team goes idle: its series is dropped, not left at zero.
    for run in busy_runs:
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
    await session.commit()
    await sampler.sample_once()
    # ...and with no active runs it no longer ranks in the top two: it folds into "other".
    assert sample("fleet_active_runs", team=str(busy.id)) is None
    assert sample("fleet_tokens_last_hour", team=str(busy.id)) is None
    assert sample("fleet_active_runs", team=str(overflow.id)) == 1


async def test_sampler_queue_depth_counts_waiting_runs_by_kind(db_session):
    session, session_maker = db_session
    sampler = FleetMetricsSampler(session_maker, queue_settings())
    await sampler.sample_once()
    agents = sample("fleet_queue_depth", kind="agent")
    commands = sample("fleet_queue_depth", kind="command")

    user, workspace = await seed_workspace(session)
    waiting = make_run(user, workspace, "thinking")
    waiting.enqueued_at = datetime.now(UTC)
    session.add(waiting)
    await seed_command_run(session, enqueued_at=datetime.now(UTC))
    await session.commit()
    await sampler.sample_once()

    assert sample("fleet_queue_depth", kind="agent") == agents + 1
    assert sample("fleet_queue_depth", kind="command") == commands + 1


def test_pool_collector_reports_this_process_pool_but_nothing_under_null_pool() -> None:
    assert sample("db_pool_size") is not None
    assert sample("db_pool_checked_out") >= 0

    unpooled = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        assert list(DbPoolCollector(unpooled).collect()) == []
    finally:
        unpooled.sync_engine.dispose()


async def test_ping_redis_observes_latency_or_counts_the_failure() -> None:
    server = fakeredis.FakeServer()
    ok_before = sample("redis_ping_seconds_count", role="probe") or 0
    errors_before = sample("redis_ping_errors_total", role="probe") or 0

    assert await ping_redis("probe", lambda: fakeredis.aioredis.FakeRedis(server=server)) is True
    assert sample("redis_ping_seconds_count", role="probe") == ok_before + 1

    server.connected = False
    assert await ping_redis("probe", lambda: fakeredis.aioredis.FakeRedis(server=server)) is False
    assert sample("redis_ping_errors_total", role="probe") == errors_before + 1
    assert sample("redis_ping_seconds_count", role="probe") == ok_before + 1


async def test_spend_window_is_served_by_the_retention_index(db_session):
    """Every replica runs this every few seconds: it must not scan the whole run table."""
    session, _ = db_session
    # The run-table access path alone: with a handful of rows the planner joins from
    # workspaces first, which says nothing about how the window predicate is served.
    window = select(AgentRun.id).where(spend_by_team(datetime.now(UTC)).whereclause)
    statement = window.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join((await session.execute(text(f"EXPLAIN {statement}"))).scalars().all())
    assert "ix_agent_runs_events_retention" in plan, plan
