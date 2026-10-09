"""Epic 06: per-team and global concurrent-run caps, and queue-depth backpressure."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.models import AgentRun, CommandRun, CustomCommand, Project, Team, User, Workspace
from app.services.agents import ACTIVE_RUN_STATUSES, AgentRuntime, AgentService
from app.services.capacity import RunCapacityExceededError, run_capacity_exceeded_handler
from app.services.commands import CommandRuntime, CommandService
from app.services.runtime import InMemoryEventBus, InMemoryRunRegistry
from tests.test_job_queue import (
    FakeDispatcher,
    db_session,
    make_run,
    make_user_and_workspace,
    queue_settings,
)
from tests.test_worker_jobs import FakeDaytona

__all__ = ["db_session"]


def agent_service(session, session_maker, **overrides) -> AgentService:
    return AgentService(
        session,
        session_maker,
        queue_settings(**overrides),
        runtime=AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
        dispatcher=FakeDispatcher(),
    )


async def make_team(session, user: User) -> Team:
    project = Project(id=uuid4(), name="P", slug=f"p-{uuid4()}", created_by=user.id)
    session.add(project)
    await session.flush()
    team = Team(id=uuid4(), project_id=project.id, name="T", slug=f"t-{uuid4()}")
    session.add(team)
    await session.flush()
    return team


async def seed_workspace(session, team: Team | None = None) -> tuple[User, Workspace]:
    user, workspace = make_user_and_workspace()
    session.add(user)
    await session.flush()
    workspace.team_id = team.id if team is not None else None
    session.add(workspace)
    await session.flush()
    return user, workspace


async def active_run_count(session) -> int:
    return await session.scalar(
        select(func.count(AgentRun.id)).where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
    )


async def queue_depth(session) -> int:
    agents = await session.scalar(
        select(func.count(AgentRun.id)).where(
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            AgentRun.enqueued_at.is_not(None),
            AgentRun.claimed_at.is_(None),
        )
    )
    commands = await session.scalar(
        select(func.count(CommandRun.id)).where(
            CommandRun.status == "running",
            CommandRun.enqueued_at.is_not(None),
            CommandRun.claimed_at.is_(None),
        )
    )
    return agents + commands


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_caps_and_backpressure_are_off_by_default():
    settings = Settings(_env_file=None)
    assert settings.run_cap_global == 0
    assert settings.run_cap_per_team == 0
    assert settings.run_queue_max_depth == 0


# ---------------------------------------------------------------------------
# Per-team cap
# ---------------------------------------------------------------------------


async def test_team_cap_rejects_a_run_once_the_team_is_at_its_limit(db_session):
    session, session_maker = db_session
    owner, busy = await seed_workspace(session)
    team = await make_team(session, owner)
    busy.team_id = team.id
    user, idle = await seed_workspace(session, team)
    session.add(make_run(owner, busy, "executing"))
    await session.flush()

    service = agent_service(session, session_maker, run_cap_per_team=1)
    with pytest.raises(RunCapacityExceededError) as exc:
        await service.create_run(idle.id, user.id, "hello")

    assert exc.value.scope == "team"
    assert exc.value.retry_after == 15
    assert await session.scalar(
        select(func.count(AgentRun.id)).where(AgentRun.workspace_id == idle.id)
    ) == 0


async def test_team_cap_admits_a_run_below_the_limit(db_session):
    session, session_maker = db_session
    owner, busy = await seed_workspace(session)
    team = await make_team(session, owner)
    busy.team_id = team.id
    user, idle = await seed_workspace(session, team)
    session.add(make_run(owner, busy, "executing"))
    await session.flush()

    run = await agent_service(session, session_maker, run_cap_per_team=2).create_run(
        idle.id, user.id, "hello"
    )
    assert run.status == "thinking"


async def test_finished_runs_do_not_count_toward_the_team_cap(db_session):
    session, session_maker = db_session
    owner, busy = await seed_workspace(session)
    team = await make_team(session, owner)
    busy.team_id = team.id
    user, idle = await seed_workspace(session, team)
    session.add(make_run(owner, busy, "completed"))
    await session.flush()

    run = await agent_service(session, session_maker, run_cap_per_team=1).create_run(
        idle.id, user.id, "hello"
    )
    assert run.status == "thinking"


async def test_a_workspace_without_a_team_is_not_subject_to_the_team_cap(db_session):
    session, session_maker = db_session
    other_user, other = await seed_workspace(session)
    user, workspace = await seed_workspace(session)
    session.add(make_run(other_user, other, "executing"))
    await session.flush()

    run = await agent_service(session, session_maker, run_cap_per_team=1).create_run(
        workspace.id, user.id, "hello"
    )
    assert run.status == "thinking"


# ---------------------------------------------------------------------------
# Global cap
# ---------------------------------------------------------------------------


async def test_global_cap_rejects_a_run_once_the_platform_is_at_its_limit(db_session):
    session, session_maker = db_session
    other_user, other = await seed_workspace(session)
    user, workspace = await seed_workspace(session)
    session.add(make_run(other_user, other, "executing"))
    await session.flush()
    # The suite shares the dev database, so cap relative to whatever is already active.
    cap = await active_run_count(session)

    with pytest.raises(RunCapacityExceededError) as exc:
        await agent_service(session, session_maker, run_cap_global=cap).create_run(
            workspace.id, user.id, "hello"
        )
    assert exc.value.scope == "global"


async def test_global_cap_admits_a_run_below_the_limit(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    cap = await active_run_count(session) + 1

    run = await agent_service(session, session_maker, run_cap_global=cap).create_run(
        workspace.id, user.id, "hello"
    )
    assert run.status == "thinking"


# ---------------------------------------------------------------------------
# Queue-depth backpressure
# ---------------------------------------------------------------------------


async def test_queue_backpressure_rejects_when_unclaimed_runs_reach_the_threshold(db_session):
    session, session_maker = db_session
    other_user, other = await seed_workspace(session)
    user, workspace = await seed_workspace(session)
    waiting = make_run(other_user, other, "thinking")
    waiting.enqueued_at = datetime.now(UTC)
    session.add(waiting)
    await session.flush()
    depth = await queue_depth(session)

    with pytest.raises(RunCapacityExceededError) as exc:
        await agent_service(session, session_maker, run_queue_max_depth=depth).create_run(
            workspace.id, user.id, "hello"
        )
    assert exc.value.scope == "queue"


async def test_claimed_runs_are_not_queue_depth(db_session):
    session, session_maker = db_session
    other_user, other = await seed_workspace(session)
    user, workspace = await seed_workspace(session)
    running = make_run(other_user, other, "executing")
    running.enqueued_at = datetime.now(UTC)
    running.claimed_at = datetime.now(UTC)
    session.add(running)
    await session.flush()
    max_depth = await queue_depth(session) + 1

    run = await agent_service(session, session_maker, run_queue_max_depth=max_depth).create_run(
        workspace.id, user.id, "hello"
    )
    assert run.status == "thinking"


async def test_inline_mode_ignores_queue_backpressure(db_session):
    session, session_maker = db_session
    other_user, other = await seed_workspace(session)
    user, workspace = await seed_workspace(session)
    waiting = make_run(other_user, other, "thinking")
    waiting.enqueued_at = datetime.now(UTC)
    session.add(waiting)
    await session.flush()

    service = agent_service(
        session, session_maker, run_execution_mode="inline", run_queue_max_depth=1
    )
    run = await service.create_run(workspace.id, user.id, "hello")
    task = service.runtime.registry.get_task(run.id)
    assert task is not None
    await task


async def test_command_runs_are_refused_under_queue_backpressure(db_session):
    session, session_maker = db_session
    other_user, other = await seed_workspace(session)
    user, workspace = await seed_workspace(session)
    waiting = make_run(other_user, other, "thinking")
    waiting.enqueued_at = datetime.now(UTC)
    session.add(waiting)
    command = CustomCommand(workspace_id=workspace.id, name="ls", command="ls")
    session.add(command)
    await session.flush()
    depth = await queue_depth(session)

    service = CommandService(
        session,
        session_maker,
        queue_settings(run_queue_max_depth=depth),
        runtime=CommandRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
        daytona_runtime=FakeDaytona(),
        dispatcher=FakeDispatcher(),
    )
    with pytest.raises(RunCapacityExceededError) as exc:
        await service.create_run(workspace.id, command.id, user.id, confirmed=True)
    assert exc.value.scope == "queue"


# ---------------------------------------------------------------------------
# HTTP mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("scope", "status_code"), [("team", 429), ("global", 429), ("queue", 503)])
async def test_capacity_errors_map_to_retryable_responses(scope, status_code):
    response = await run_capacity_exceeded_handler(None, RunCapacityExceededError(scope, 15))

    assert response.status_code == status_code
    assert response.headers["Retry-After"] == "15"
    assert json.loads(response.body)["scope"] == scope


# ---------------------------------------------------------------------------
# Two replicas
# ---------------------------------------------------------------------------


async def _race_two_creates(settings: Settings, team: bool) -> list[str]:
    """Two engines ("replicas") create a run in two different workspaces at the same moment."""
    db_url = get_settings().database_url
    engine_a = create_async_engine(db_url)
    engine_b = create_async_engine(db_url)
    user_a, ws_a = make_user_and_workspace()
    user_b, ws_b = make_user_and_workspace()
    project = Project(id=uuid4(), name="P", slug=f"p-{uuid4()}", created_by=user_a.id)
    team_row = Team(id=uuid4(), project_id=project.id, name="T", slug=f"t-{uuid4()}")
    try:
        async with async_sessionmaker(engine_a, expire_on_commit=False)() as setup:
            setup.add_all([user_a, user_b])
            await setup.flush()
            if team:
                setup.add(project)
                await setup.flush()
                setup.add(team_row)
                await setup.flush()
                ws_a.team_id = ws_b.team_id = team_row.id
            setup.add_all([ws_a, ws_b])
            await setup.commit()

        start = asyncio.Event()

        async def create(engine, user, workspace) -> str:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                service = AgentService(
                    session,
                    maker,
                    settings,
                    runtime=AgentRuntime(InMemoryRunRegistry(), InMemoryEventBus()),
                    dispatcher=FakeDispatcher(),
                )
                await start.wait()
                try:
                    await service.create_run(workspace.id, user.id, "hello")
                except RunCapacityExceededError as error:
                    return error.scope
                return "admitted"

        tasks = [
            asyncio.create_task(create(engine_a, user_a, ws_a)),
            asyncio.create_task(create(engine_b, user_b, ws_b)),
        ]
        await asyncio.sleep(0)
        if not team:
            # The suite shares the dev database, so cap relative to what is active right now,
            # as late as possible: a run elsewhere starting or ending mid-race skews the count.
            async with async_sessionmaker(engine_a)() as probe:
                settings.run_cap_global = await active_run_count(probe) + 1
        start.set()
        return sorted(await asyncio.gather(*tasks))
    finally:
        async with async_sessionmaker(engine_a)() as cleanup:
            ids = [ws_a.id, ws_b.id]
            await cleanup.execute(delete(AgentRun).where(AgentRun.workspace_id.in_(ids)))
            await cleanup.execute(delete(Workspace).where(Workspace.id.in_(ids)))
            await cleanup.execute(delete(Project).where(Project.id == project.id))
            await cleanup.execute(delete(User).where(User.id.in_([user_a.id, user_b.id])))
            await cleanup.commit()
        await engine_a.dispose()
        await engine_b.dispose()


async def test_team_cap_holds_across_two_replicas():
    results = await _race_two_creates(queue_settings(run_cap_per_team=1), team=True)
    assert results == ["admitted", "team"]


async def test_global_cap_holds_across_two_replicas():
    results = await _race_two_creates(queue_settings(), team=False)
    assert results == ["admitted", "global"]
