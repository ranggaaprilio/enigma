"""Epic 05: retention for agent events, command output and notifications; hot-path indexes."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.models import AgentEvent, AgentRun, Notification, User, Workspace
from app.services.retention import (
    expired_agent_runs,
    expired_command_runs,
    expired_notifications,
    prune_agent_events,
    prune_command_output,
    prune_notifications,
    retention_enabled,
    run_retention_pass,
)
from tests.test_job_queue import db_session, make_run, make_user_and_workspace
from tests.test_worker_jobs import seed_command_run

__all__ = ["db_session"]


def retention_settings(**overrides) -> Settings:
    for field in (
        "retention_agent_events_days",
        "retention_command_output_days",
        "retention_read_notifications_days",
        "retention_notifications_days",
    ):
        overrides.setdefault(field, 0)
    return Settings(**overrides)


def days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# These tests share the developer's database, and the concurrency test commits for real. "Old"
# rows are seeded ~25 years back and windows reach ~22 years back, so a pass can never touch
# real history and exact counts hold.
OLD = 9000
WINDOW = 8000


async def seed_run_with_events(session, user, workspace, *, status, completed_days_ago, events=3):
    run = make_run(user, workspace, status)
    run.tokens_used = 1234
    if completed_days_ago is not None:
        run.completed_at = days_ago(completed_days_ago)
    session.add(run)
    await session.flush()
    session.add_all(
        AgentEvent(agent_run_id=run.id, event_type="output_text", content=f"chunk {i}")
        for i in range(events)
    )
    await session.flush()
    return run


async def event_count(session, run_id) -> int:
    return await session.scalar(
        select(func.count()).select_from(AgentEvent).where(AgentEvent.agent_run_id == run_id)
    )


# --- settings ----------------------------------------------------------------------------


def test_retention_is_off_by_default():
    settings = Settings()

    assert settings.retention_agent_events_days == 0
    assert settings.retention_command_output_days == 0
    assert settings.retention_read_notifications_days == 0
    assert settings.retention_notifications_days == 0
    assert retention_enabled(settings) is False
    assert retention_enabled(retention_settings(retention_command_output_days=7)) is True


@pytest.mark.parametrize("field", ["retention_agent_events_days", "retention_batch_size"])
def test_retention_settings_reject_negatives(field):
    with pytest.raises(ValidationError):
        retention_settings(**{field: -1})


# --- agent events ------------------------------------------------------------------------


async def test_prunes_events_of_old_finished_runs_and_keeps_their_token_totals(db_session):
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    old = await seed_run_with_events(
        session, user, workspace, status="completed", completed_days_ago=OLD
    )
    recent = await seed_run_with_events(
        session, user, workspace, status="completed", completed_days_ago=5
    )
    active = await seed_run_with_events(
        session, user, workspace, status="executing", completed_days_ago=None
    )
    await session.commit()

    pruned = await prune_agent_events(
        session_maker, retention_settings(retention_agent_events_days=WINDOW)
    )

    assert pruned == 1
    assert await event_count(session, old.id) == 0
    assert await event_count(session, recent.id) == 3
    assert await event_count(session, active.id) == 3
    await session.refresh(old)
    await session.refresh(recent)
    assert old.events_pruned_at is not None
    assert recent.events_pruned_at is None
    # Cost analytics reads agent_runs, which retention never deletes.
    assert old.tokens_used == 1234


async def test_never_prunes_an_active_run_even_with_a_stale_completed_at(db_session):
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    run = await seed_run_with_events(
        session, user, workspace, status="thinking", completed_days_ago=OLD
    )
    await session.commit()

    settings = retention_settings(retention_agent_events_days=WINDOW)
    assert await prune_agent_events(session_maker, settings) == 0
    assert await event_count(session, run.id) == 3


async def test_disabled_window_prunes_nothing(db_session):
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    run = await seed_run_with_events(
        session, user, workspace, status="error", completed_days_ago=OLD
    )
    await session.commit()

    assert await prune_agent_events(session_maker, retention_settings()) == 0
    assert await event_count(session, run.id) == 3


async def test_prunes_in_batches_until_done_and_skips_runs_already_pruned(db_session):
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    runs = [
        await seed_run_with_events(
            session, user, workspace, status="stopped", completed_days_ago=OLD
        )
        for _ in range(5)
    ]
    await session.commit()
    settings = retention_settings(retention_agent_events_days=WINDOW, retention_batch_size=2)

    assert await prune_agent_events(session_maker, settings) == 5
    for run in runs:
        assert await event_count(session, run.id) == 0
    assert await prune_agent_events(session_maker, settings) == 0


# --- command output ----------------------------------------------------------------------


async def test_empties_output_of_old_finished_command_runs(db_session):
    session, session_maker = db_session
    old = await seed_command_run(session, status="completed", completed_at=days_ago(OLD))
    recent = await seed_command_run(session, status="failed", completed_at=days_ago(2))
    running = await seed_command_run(session, status="running")
    for run in (old, recent, running):
        run.output = "lots of log output"
    await session.commit()

    pruned = await prune_command_output(
        session_maker, retention_settings(retention_command_output_days=WINDOW)
    )

    assert pruned == 1
    for run in (old, recent, running):
        await session.refresh(run)
    assert (old.output, old.output_pruned_at is not None) == ("", True)
    assert old.status == "completed"
    assert recent.output == "lots of log output" and recent.output_pruned_at is None
    assert running.output == "lots of log output" and running.output_pruned_at is None


# --- notifications -----------------------------------------------------------------------


async def test_prunes_read_notifications_and_any_beyond_the_hard_window(db_session):
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()

    def note(created_days: float, read_days: float | None) -> Notification:
        return Notification(
            user_id=user.id,
            workspace_id=workspace.id,
            notification_type="run_completed",
            title="t",
            message="m",
            created_at=days_ago(created_days),
            read_at=None if read_days is None else days_ago(read_days),
        )

    # Budget warnings are the record `_already_warned` de-duplicates against (forever, for a
    # "total" budget), so no window may delete them.
    budget_warning = note(OLD + 500, OLD + 400)
    budget_warning.notification_type = "budget_warning"
    read_long_ago = note(OLD + 100, OLD)
    read_recently = note(OLD + 100, 1)
    unread_old = note(OLD + 100, None)
    ancient_unread = note(OLD + 500, None)
    session.add_all([budget_warning, read_long_ago, read_recently, unread_old, ancient_unread])
    await session.commit()

    pruned = await prune_notifications(
        session_maker,
        retention_settings(
            retention_read_notifications_days=WINDOW, retention_notifications_days=OLD + 200
        ),
    )

    assert pruned == 2
    remaining = set(
        (
            await session.scalars(select(Notification.id).where(Notification.user_id == user.id))
        ).all()
    )
    assert remaining == {budget_warning.id, read_recently.id, unread_old.id}


# --- the pass ----------------------------------------------------------------------------


async def test_pass_runs_every_enabled_policy(db_session):
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    await seed_run_with_events(session, user, workspace, status="completed", completed_days_ago=OLD)
    command = await seed_command_run(session, status="completed", completed_at=days_ago(OLD))
    command.output = "x"
    await session.commit()

    result = await run_retention_pass(
        session_maker,
        retention_settings(
            retention_agent_events_days=WINDOW, retention_command_output_days=WINDOW
        ),
    )

    assert result.agent_runs == 1
    assert result.command_runs == 1
    assert result.notifications == 0


@pytest.fixture
async def committed_old_runs():
    """Old finished runs committed for real, so separate engines ("replicas") see them."""
    engine = create_async_engine(get_settings().database_url)
    user, workspace = make_user_and_workspace()
    async with async_sessionmaker(engine, expire_on_commit=False)() as setup:
        setup.add_all([user, workspace])
        await setup.flush()
        runs = [
            await seed_run_with_events(
                setup, user, workspace, status="completed", completed_days_ago=OLD
            )
            for _ in range(6)
        ]
        await setup.commit()
    try:
        yield runs
    finally:
        async with async_sessionmaker(engine)() as cleanup:
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()
        await engine.dispose()


async def test_concurrent_passes_on_two_replicas_prune_each_run_once(committed_old_runs):
    run_ids = {run.id for run in committed_old_runs}
    engines = [create_async_engine(get_settings().database_url) for _ in range(2)]
    settings = retention_settings(retention_agent_events_days=WINDOW, retention_batch_size=1)
    try:
        counts = await asyncio.gather(
            *(
                prune_agent_events(async_sessionmaker(engine, expire_on_commit=False), settings)
                for engine in engines
            )
        )
        async with async_sessionmaker(engines[0])() as check:
            pruned = (
                await check.scalars(
                    select(AgentRun.id).where(
                        AgentRun.id.in_(run_ids), AgentRun.events_pruned_at.is_not(None)
                    )
                )
            ).all()
            left = await check.scalar(
                select(func.count())
                .select_from(AgentEvent)
                .where(AgentEvent.agent_run_id.in_(run_ids))
            )
        assert set(pruned) == run_ids
        assert left == 0
        # SKIP LOCKED hands each run to exactly one replica.
        assert sum(counts) == len(run_ids)
    finally:
        for engine in engines:
            await engine.dispose()


# --- EXPLAIN evidence ---------------------------------------------------------------------


def _sql(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


HOT_PATHS = {
    # Retention scans (this epic).
    "ix_agent_runs_events_retention": lambda: _sql(expired_agent_runs(days_ago(30), 100)),
    "ix_command_runs_output_retention": lambda: _sql(expired_command_runs(days_ago(30), 100)),
    "ix_notifications_created_at": lambda: _sql(
        expired_notifications(days_ago(30), days_ago(90), 100)
    ),
    # The notification bell: newest first for one user (NotificationService.list_for_user).
    "ix_notifications_user_created": lambda: _sql(
        select(Notification)
        .where(Notification.user_id == uuid4())
        .order_by(Notification.created_at.desc())
        .limit(50)
    ),
    # Stream backfill from a high-water mark (epic 03).
    "ix_agent_events_run_created_id": lambda: _sql(
        select(AgentEvent)
        .where(AgentEvent.agent_run_id == uuid4(), AgentEvent.created_at >= days_ago(1))
        .order_by(AgentEvent.created_at, AgentEvent.id)
    ),
    # The one-active-run-per-workspace check (agents.py).
    "uq_agent_runs_one_active_per_workspace": lambda: _sql(
        select(AgentRun.id).where(
            AgentRun.workspace_id == uuid4(),
            AgentRun.status.in_(("thinking", "executing")),
        )
    ),
}


@pytest.mark.parametrize("index_name", sorted(HOT_PATHS))
async def test_hot_path_query_is_served_by_its_index(db_session, index_name):
    """The planner *can* use the index (seqscan off: test tables are too small to prefer it)."""
    session, _ = db_session
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        (await session.execute(text("EXPLAIN " + HOT_PATHS[index_name]()))).scalars().all()
    )
    assert index_name in plan, plan


# --- wiring ------------------------------------------------------------------------------


async def test_start_retention_only_runs_when_a_window_is_configured(monkeypatch):
    from app.services import retention as retention_module

    started = asyncio.Event()

    async def fake_loop(session_maker, settings):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(retention_module, "run_retention", fake_loop)

    assert retention_module.start_retention(None, retention_settings()) is None

    task = retention_module.start_retention(None, retention_settings(retention_agent_events_days=7))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _lifespan_retention(monkeypatch, mode: str) -> list[str]:
    from app import main as main_module
    from app.core.lifecycle import lifecycle

    calls: list[str] = []

    async def noop(*_args, **_kwargs):
        return 0

    async def fake_loop(session_maker, settings):
        calls.append("retention.start")
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("retention.stop")

    class Closable:
        async def close(self):
            return None

        async def dispose(self):
            return None

    settings = retention_settings(run_execution_mode=mode, retention_agent_events_days=7)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.services.retention.run_retention", fake_loop)
    monkeypatch.setattr(main_module, "reconcile_interrupted_runs", noop)
    monkeypatch.setattr(main_module, "drain_in_flight_runs", noop)
    monkeypatch.setattr(main_module, "close_run_dispatcher", noop)
    monkeypatch.setattr(main_module, "close_all", Closable().close)
    monkeypatch.setattr(main_module, "engine", Closable())
    monkeypatch.setattr(main_module, "install_sigterm_drain", lambda *a, **k: lambda: None)
    monkeypatch.setattr(lifecycle, "draining", False)

    async with main_module.lifespan(main_module.app):
        await asyncio.sleep(0)
        calls.append("serving")
    return calls


async def test_inline_api_runs_retention_for_its_lifetime(monkeypatch):
    assert await _lifespan_retention(monkeypatch, "inline") == [
        "retention.start",
        "serving",
        "retention.stop",
    ]


async def test_queue_mode_api_leaves_retention_to_the_workers(monkeypatch):
    assert await _lifespan_retention(monkeypatch, "queue") == ["serving"]
