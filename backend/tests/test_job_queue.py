"""Epic 02: queue-backed run execution -- settings, the single-active-run index, enqueue path."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.models import AgentEvent, AgentRun, User, Workspace
from app.services.agents import AgentRunAlreadyActiveError, AgentRuntime, AgentService
from app.services.jobs import (
    ENQUEUE_FAILED,
    RunQueueUnavailableError,
    run_queue_unavailable_handler,
)
from app.services.runtime import InMemoryEventBus, InMemoryRunLock


def queue_settings(**overrides) -> Settings:
    overrides.setdefault("run_execution_mode", "queue")
    return Settings(
        agent_allow_mock_provider=True,
        agent_provider="mock",
        openhands_llm_base_url=None,
        openhands_llm_api_key=None,
        openhands_llm_model=None,
        openai_api_key=None,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_run_execution_mode_defaults_to_inline():
    assert Settings().run_execution_mode == "inline"


def test_run_execution_mode_rejects_unknown_values():
    with pytest.raises(ValidationError):
        Settings(run_execution_mode="celery")


def test_worker_settings_have_sane_defaults():
    settings = Settings()
    assert settings.run_queue_name == "enigma:runs"
    assert settings.worker_max_jobs == 10
    assert settings.worker_max_tries == 3
    assert settings.worker_id  # hostname by default, never empty
    # The in-handler run timeout must fire before arq's job timeout, or arq cancels the job
    # without the run row ever being told.
    assert settings.worker_job_timeout_seconds > settings.agent_run_timeout_seconds


# ---------------------------------------------------------------------------
# Single-active-run partial unique index
# ---------------------------------------------------------------------------


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


def make_user_and_workspace() -> tuple[User, Workspace]:
    now = datetime.now(UTC)
    user = User(
        id=uuid4(),
        email=f"jobs-{uuid4().hex[:8]}@example.com",
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
        sandbox_id=f"sandbox-{uuid4().hex[:8]}",
        workspace_path="workspace",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    return user, workspace


def make_run(user: User, workspace: Workspace, status: str = "thinking") -> AgentRun:
    return AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="do things",
        status=status,
        provider="mock",
        model="mock-model",
    )


async def test_index_rejects_a_second_active_run_in_one_workspace(db_session):
    session, _ = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    session.add(make_run(user, workspace, "thinking"))
    await session.flush()

    session.add(make_run(user, workspace, "executing"))
    with pytest.raises(IntegrityError, match="uq_agent_runs_one_active_per_workspace"):
        async with session.begin_nested():
            await session.flush()


async def test_index_allows_terminal_runs_alongside_an_active_one(db_session):
    session, _ = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    session.add_all(
        [
            make_run(user, workspace, "thinking"),
            make_run(user, workspace, "completed"),
            make_run(user, workspace, "error"),
            make_run(user, workspace, "stopped"),
        ]
    )
    await session.flush()


async def test_a_replica_that_passes_the_precheck_still_gets_already_active(
    db_session, monkeypatch
):
    """Two API replicas have separate in-process locks; the index is the cross-replica guard.

    Simulate replica B racing replica A by giving it its own lock and blinding the pre-insert
    SELECT, so only the database can stop the second active run.
    """
    session, session_maker = db_session
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    session.add(make_run(user, workspace, "thinking"))
    await session.commit()

    service = AgentService(
        session, session_maker, queue_settings(), run_lock=InMemoryRunLock()
    )

    async def no_active_run(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_find_active_run_id", no_active_run)

    with pytest.raises(AgentRunAlreadyActiveError):
        await service.create_run(workspace.id, user.id, "second")

    # The session survives the violation (savepoint rollback) and still sees one active run.
    active = await session.scalars(
        select(AgentRun).where(
            AgentRun.workspace_id == workspace.id,
            AgentRun.status.in_(("thinking", "executing")),
        )
    )
    assert len(active.all()) == 1


async def test_concurrent_inserts_from_two_connections_admit_exactly_one():
    """Real concurrency: two engines (two "replicas") commit against the same workspace."""
    settings = get_settings()
    engine_a = create_async_engine(settings.database_url)
    engine_b = create_async_engine(settings.database_url)
    user, workspace = make_user_and_workspace()
    try:
        async with async_sessionmaker(engine_a, expire_on_commit=False)() as setup:
            setup.add_all([user, workspace])
            await setup.commit()

        start = asyncio.Event()

        async def insert(engine) -> bool:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await start.wait()
                session.add(make_run(user, workspace, "thinking"))
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    return False
                return True

        tasks = [asyncio.create_task(insert(engine_a)), asyncio.create_task(insert(engine_b))]
        await asyncio.sleep(0)
        start.set()
        results = await asyncio.gather(*tasks)
        assert sorted(results) == [False, True]
    finally:
        async with async_sessionmaker(engine_a)() as cleanup:
            await cleanup.execute(delete(AgentRun).where(AgentRun.workspace_id == workspace.id))
            await cleanup.execute(delete(Workspace).where(Workspace.id == workspace.id))
            await cleanup.execute(delete(User).where(User.id == user.id))
            await cleanup.commit()
        await engine_a.dispose()
        await engine_b.dispose()


# ---------------------------------------------------------------------------
# Enqueue path (queue mode)
# ---------------------------------------------------------------------------


class FakeDispatcher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.agent_runs: list = []
        self.command_runs: list = []
        self.spec_finalizes: list = []

    async def enqueue_agent_run(self, run_id) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.agent_runs.append(run_id)

    async def enqueue_command_run(self, run_id) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.command_runs.append(run_id)

    async def enqueue_spec_finalize(self, phase_id, run_id) -> None:
        if self.fail:
            raise ConnectionError("redis is down")
        self.spec_finalizes.append((phase_id, run_id))


async def seed(session) -> tuple[User, Workspace]:
    user, workspace = make_user_and_workspace()
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


async def test_queue_mode_create_run_enqueues_the_run_id_and_starts_nothing_locally(
    db_session,
):
    session, session_maker = db_session
    user, workspace = await seed(session)
    dispatcher = FakeDispatcher()
    runtime = AgentRuntime(bus=InMemoryEventBus())
    service = AgentService(
        session, session_maker, queue_settings(), runtime=runtime, dispatcher=dispatcher
    )

    run = await service.create_run(workspace.id, user.id, "hello", working_dir="worktrees/x")

    assert run.status == "thinking"
    assert dispatcher.agent_runs == [run.id]
    assert runtime.registry.get_task(run.id) is None
    stored = await session.get(AgentRun, run.id)
    await session.refresh(stored)
    assert stored.working_dir == "worktrees/x"
    assert stored.enqueued_at is not None
    assert stored.claimed_at is None


async def test_inline_mode_never_touches_the_dispatcher(db_session):
    session, session_maker = db_session
    user, workspace = await seed(session)
    dispatcher = FakeDispatcher()
    runtime = AgentRuntime(bus=InMemoryEventBus())
    settings = queue_settings()
    settings.run_execution_mode = "inline"
    service = AgentService(session, session_maker, settings, runtime=runtime, dispatcher=dispatcher)

    run = await service.create_run(workspace.id, user.id, "hello")
    task = runtime.registry.get_task(run.id)
    assert task is not None
    await task

    assert dispatcher.agent_runs == []


async def test_enqueue_failure_ends_the_run_and_frees_the_workspace(db_session):
    session, session_maker = db_session
    user, workspace = await seed(session)
    bus = InMemoryEventBus()
    runtime = AgentRuntime(bus=bus)
    service = AgentService(
        session,
        session_maker,
        queue_settings(),
        runtime=runtime,
        dispatcher=FakeDispatcher(fail=True),
    )

    with pytest.raises(RunQueueUnavailableError):
        await service.create_run(workspace.id, user.id, "hello")

    runs = (
        await session.scalars(select(AgentRun).where(AgentRun.workspace_id == workspace.id))
    ).all()
    assert len(runs) == 1
    await session.refresh(runs[0])
    assert runs[0].status == "error"
    assert runs[0].error_reason == ENQUEUE_FAILED
    events = (
        await session.scalars(select(AgentEvent).where(AgentEvent.agent_run_id == runs[0].id))
    ).all()
    assert [event.event_type for event in events] == ["error"]

    # The workspace is not wedged: a retry is accepted once the queue is back.
    service._dispatcher = FakeDispatcher()
    retry = await service.create_run(workspace.id, user.id, "hello again")
    assert retry.status == "thinking"


async def test_queue_unavailable_maps_to_503_with_retry_after():
    from app.main import app

    response = await run_queue_unavailable_handler(None, RunQueueUnavailableError())
    assert response.status_code == 503
    assert response.headers["Retry-After"]
    assert app.exception_handlers[RunQueueUnavailableError] is run_queue_unavailable_handler


# ---------------------------------------------------------------------------
# Job serialization: JSON, never pickle (Redis write access must not mean code execution)
# ---------------------------------------------------------------------------


def test_jobs_round_trip_through_json():
    from arq.jobs import deserialize_job_raw, serialize_job

    from app.services.jobs import deserialize_job_payload, serialize_job_payload

    raw = serialize_job(
        "run_agent_job", ("abc",), {}, None, 123, serializer=serialize_job_payload
    )
    assert raw.startswith(b"{")
    assert deserialize_job_raw(raw, deserializer=deserialize_job_payload) == (
        "run_agent_job",
        ["abc"],
        {},
        None,
        123,
    )


def test_a_pickled_payload_is_rejected_not_executed():
    import pickle

    from app.services.jobs import deserialize_job_payload

    class Boom:
        def __reduce__(self):
            return (exec, ("raise SystemExit('pickle executed')",))

    with pytest.raises(ValueError):
        deserialize_job_payload(pickle.dumps(Boom()))


async def test_enqueue_pool_and_worker_both_use_the_json_serializer(monkeypatch):
    from app.services import jobs
    from app.worker import WorkerSettings

    captured: dict = {}

    class FakePool:
        async def enqueue_job(self, *args, **kwargs):
            return None

    async def fake_create_pool(_settings, **kwargs):
        captured.update(kwargs)
        return FakePool()

    monkeypatch.setattr(jobs, "create_pool", fake_create_pool)
    await jobs.ArqDispatcher(queue_settings()).enqueue_agent_run(uuid4())

    assert captured["job_serializer"] is jobs.serialize_job_payload
    assert captured["job_deserializer"] is jobs.deserialize_job_payload
    assert WorkerSettings.job_serializer is jobs.serialize_job_payload
    assert WorkerSettings.job_deserializer is jobs.deserialize_job_payload
