"""Startup/shutdown reconciliation of runs orphaned by a dead or departing process."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.models import (
    AgentEvent,
    AgentRun,
    CommandRun,
    CustomCommand,
    SpecPhase,
    SpecWorkflow,
    User,
    Workspace,
)
from app.services.agents import AgentRuntime, AgentService
from app.services.commands import CommandRuntime
from app.services.reconciliation import (
    INTERRUPTED_BY_RESTART,
    INTERRUPTED_MESSAGE,
    reconcile_interrupted_runs,
)
from app.services.runtime import InMemoryEventBus


@pytest.fixture
async def db_session():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with session_maker() as session:
            await session.begin_nested()
            yield session, session_maker
            await session.rollback()
    await engine.dispose()


async def seed_workspace(session) -> tuple[User, Workspace]:
    now = datetime.now(UTC)
    user = User(
        id=uuid4(),
        email=f"reconcile-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug=f"demo-{uuid4().hex[:8]}",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id=f"sandbox-{uuid4().hex[:8]}",
        workspace_path="workspace",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


def agent_run(user, workspace, status: str) -> AgentRun:
    return AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="do things",
        status=status,
        provider="mock",
        model="mock-model",
    )


async def command_run(session, user, workspace, status: str) -> CommandRun:
    command = CustomCommand(workspace_id=workspace.id, name=f"c-{uuid4().hex[:6]}", command="ls")
    session.add(command)
    await session.flush()
    run = CommandRun(
        custom_command_id=command.id,
        workspace_id=workspace.id,
        user_id=user.id,
        status=status,
        output="partial output\n",
    )
    session.add(run)
    await session.flush()
    return run


def runtimes() -> tuple[AgentRuntime, CommandRuntime]:
    bus = InMemoryEventBus()
    return AgentRuntime(bus=bus), CommandRuntime(bus=bus)


async def test_active_agent_runs_become_interrupted_errors_with_a_terminal_event(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    thinking = agent_run(user, workspace, "thinking")
    other_user, other_workspace = await seed_workspace(session)
    executing = agent_run(other_user, other_workspace, "executing")
    done = agent_run(user, workspace, "completed")
    session.add_all([thinking, executing, done])
    await session.commit()

    agents, commands = runtimes()
    result = await reconcile_interrupted_runs(
        session_maker, agent_runtime=agents, command_runtime=commands
    )

    assert {thinking.id, executing.id} <= set(result.agent_run_ids)
    assert done.id not in result.agent_run_ids
    for run in (thinking, executing):
        await session.refresh(run)
        assert run.status == "error"
        assert run.error_reason == INTERRUPTED_BY_RESTART
        assert run.error_message == INTERRUPTED_MESSAGE
        assert run.completed_at is not None
        events = (
            await session.execute(select(AgentEvent).where(AgentEvent.agent_run_id == run.id))
        ).scalars().all()
        assert [(e.event_type, e.event_metadata) for e in events] == [
            ("error", {"status": "error", "reason": INTERRUPTED_BY_RESTART})
        ]
    await session.refresh(done)
    assert done.status == "completed"
    assert done.error_reason is None


async def test_workspace_can_start_a_new_run_after_reconciliation(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    session.add(agent_run(user, workspace, "executing"))
    await session.commit()

    async def noop_execute_run(self, run_id, stop_event, working_dir=None):
        return None

    monkeypatch.setattr(AgentService, "_execute_run", noop_execute_run)
    agents, commands = runtimes()
    await reconcile_interrupted_runs(session_maker, agent_runtime=agents, command_runtime=commands)

    service = AgentService(
        session, session_maker, Settings(agent_allow_mock_provider=True), runtime=agents
    )
    run = await service.create_run(workspace.id, user.id, "try again")
    assert run.status == "thinking"


async def test_running_command_runs_become_interrupted_errors(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    running = await command_run(session, user, workspace, "running")
    completed = await command_run(session, user, workspace, "completed")
    await session.commit()

    agents, commands = runtimes()
    result = await reconcile_interrupted_runs(
        session_maker, agent_runtime=agents, command_runtime=commands
    )

    assert running.id in result.command_run_ids
    assert completed.id not in result.command_run_ids
    await session.refresh(running)
    assert running.status == "error"
    assert running.error_reason == INTERRUPTED_BY_RESTART
    assert running.exit_code is None
    assert running.completed_at is not None
    assert running.output.startswith("partial output\n")
    assert INTERRUPTED_MESSAGE in running.output
    await session.refresh(completed)
    assert completed.status == "completed"


async def test_live_subscribers_receive_terminal_frames(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    run = agent_run(user, workspace, "thinking")
    session.add(run)
    command = await command_run(session, user, workspace, "running")
    await session.commit()

    agents, commands = runtimes()
    agent_queue = agents.subscribe(run.id)
    command_queue = commands.subscribe(command.id)
    await reconcile_interrupted_runs(session_maker, agent_runtime=agents, command_runtime=commands)

    agent_frame = agent_queue.get_nowait()
    assert agent_frame["event_type"] == "error"
    assert agent_frame["metadata"] == {"status": "error", "reason": INTERRUPTED_BY_RESTART}
    assert command_queue.get_nowait() == {
        "type": "status",
        "status": "error",
        "exit_code": None,
        "reason": INTERRUPTED_BY_RESTART,
    }


async def test_running_spec_phases_and_workflows_become_errors(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="Add auth",
        feature_slug=f"001-{uuid4().hex[:6]}",
        description="We need login",
        branch="001-add-auth",
        spec_dir="specs/001-add-auth",
        worktree_path="worktrees/001-add-auth",
        status="running",
        current_phase="specify",
    )
    session.add(workflow)
    await session.flush()
    running_phase = SpecPhase(workflow_id=workflow.id, phase="specify", status="running")
    approved_phase = SpecPhase(workflow_id=workflow.id, phase="constitution", status="approved")
    session.add_all([running_phase, approved_phase])
    await session.commit()

    agents, commands = runtimes()
    result = await reconcile_interrupted_runs(
        session_maker, agent_runtime=agents, command_runtime=commands
    )

    assert running_phase.id in result.spec_phase_ids
    await session.refresh(running_phase)
    await session.refresh(approved_phase)
    await session.refresh(workflow)
    assert running_phase.status == "error"
    assert approved_phase.status == "approved"
    assert workflow.status == "error"


async def test_reconciliation_with_nothing_orphaned_is_a_no_op(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    session.add(agent_run(user, workspace, "stopped"))
    await session.commit()
    # A first pass clears anything a developer's local DB may already hold, so the second
    # pass's assertion is exact.
    agents, commands = runtimes()
    await reconcile_interrupted_runs(session_maker, agent_runtime=agents, command_runtime=commands)

    result = await reconcile_interrupted_runs(
        session_maker, agent_runtime=agents, command_runtime=commands
    )
    assert result.agent_run_ids == []
    assert result.command_run_ids == []
    assert result.spec_phase_ids == []
    assert result.total == 0


def test_command_status_frame_carries_the_reason_only_when_there_is_one():
    from app.services.commands import command_status_frame

    plain = CommandRun(status="completed", exit_code=0)
    interrupted = CommandRun(status="error", exit_code=None, error_reason=INTERRUPTED_BY_RESTART)

    assert command_status_frame(plain) == {"type": "status", "status": "completed", "exit_code": 0}
    assert command_status_frame(interrupted) == {
        "type": "status",
        "status": "error",
        "exit_code": None,
        "reason": INTERRUPTED_BY_RESTART,
    }


async def test_scoped_sweep_only_touches_the_runs_this_process_owned(db_session):
    """Shutdown sweeps only its own runs, so an overlapping replica's live runs survive."""
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    other_user, other_workspace = await seed_workspace(session)
    ours = agent_run(user, workspace, "executing")
    theirs = agent_run(other_user, other_workspace, "executing")
    session.add_all([ours, theirs])
    our_command = await command_run(session, user, workspace, "running")
    their_command = await command_run(session, other_user, other_workspace, "running")
    await session.commit()

    agents, commands = runtimes()
    result = await reconcile_interrupted_runs(
        session_maker,
        agent_runtime=agents,
        command_runtime=commands,
        run_ids={ours.id, our_command.id},
    )

    assert result.agent_run_ids == [ours.id]
    assert result.command_run_ids == [our_command.id]
    for row, expected in ((ours, "error"), (theirs, "executing")):
        await session.refresh(row)
        assert row.status == expected
    for row, expected in ((our_command, "error"), (their_command, "running")):
        await session.refresh(row)
        assert row.status == expected


async def test_scoped_sweep_errors_only_owned_spec_phases(db_session):
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    ours = await seed_workflow(session, user, workspace, phase_status="running")
    theirs = await seed_workflow(session, user, workspace, phase_status="running")
    await session.commit()
    our_phase = await phase_of(session, ours)

    agents, commands = runtimes()
    result = await reconcile_interrupted_runs(
        session_maker, agent_runtime=agents, command_runtime=commands, run_ids={our_phase.id}
    )

    assert result.spec_phase_ids == [our_phase.id]
    for workflow, expected in ((ours, "error"), (theirs, "running")):
        await session.refresh(workflow)
        assert workflow.status == expected


async def test_a_workflow_idle_between_phases_is_not_swept(db_session):
    """`running` on a workflow also means "between phases, waiting for the user"; only a
    workflow with a phase actually in flight is orphaned by a restart."""
    session, session_maker = db_session
    user, workspace = await seed_workspace(session)
    idle = await seed_workflow(session, user, workspace, phase_status="completed")
    await session.commit()

    agents, commands = runtimes()
    await reconcile_interrupted_runs(session_maker, agent_runtime=agents, command_runtime=commands)

    await session.refresh(idle)
    assert idle.status == "running"


async def seed_workflow(session, user, workspace, *, phase_status: str) -> SpecWorkflow:
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="Add auth",
        feature_slug=f"001-{uuid4().hex[:6]}",
        description="We need login",
        branch="001-add-auth",
        spec_dir="specs/001-add-auth",
        worktree_path="worktrees/001-add-auth",
        status="running",
        current_phase="specify",
    )
    session.add(workflow)
    await session.flush()
    session.add(SpecPhase(workflow_id=workflow.id, phase="specify", status=phase_status))
    await session.flush()
    return workflow


async def phase_of(session, workflow) -> SpecPhase:
    return (
        await session.execute(select(SpecPhase).where(SpecPhase.workflow_id == workflow.id))
    ).scalar_one()
