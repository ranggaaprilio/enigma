"""Epic 13: service_diagnostics recorder (queue + throttle + batch upsert + resolve +
retention) and reader, against real Postgres."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import ServiceDiagnostic, User, Workspace, WorkspaceService
from app.services.service_diagnostics import (
    DiagnosticEvent,
    ServiceDiagnosticsRecorder,
    ServiceDiagnosticsService,
)


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


async def _make_service(session) -> tuple[Workspace, WorkspaceService]:
    user = User(
        id=uuid4(),
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id=f"sandbox-{uuid4().hex[:8]}",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service_row = WorkspaceService(
        id=uuid4(),
        workspace_id=workspace.id,
        custom_command_id=None,
        status="running",
        desired=True,
    )
    session.add(user)
    session.add(workspace)
    session.add(service_row)
    await session.flush()
    return workspace, service_row


def _recorder(session_maker) -> ServiceDiagnosticsRecorder:
    return ServiceDiagnosticsRecorder(get_settings(), session_factory=session_maker)


async def test_record_and_flush_creates_one_row(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
            request_method="GET",
            required_header="Access-Control-Allow-Origin",
            status_code=200,
        )
    )
    await recorder.flush()

    result = await session.execute(
        select(ServiceDiagnostic).where(ServiceDiagnostic.workspace_id == workspace.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].occurrences == 1
    assert rows[0].resolved_at is None


async def test_repeated_events_within_one_batch_collapse_to_one_upsert_with_summed_occurrences(
    db_session,
):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)

    for _ in range(5):
        # Bypass the in-process throttle for this test by clearing it each time, so all 5
        # reach the queue -- the collapsing-in-one-batch behavior is what's under test here,
        # not the throttle (covered separately below).
        recorder._last_enqueued.clear()  # noqa: SLF001
        recorder.record(
            DiagnosticEvent(
                workspace_id=workspace.id,
                service_id=service_row.id,
                kind="cors_missing_allow_origin",
                origin="https://web.example.com",
            )
        )
    await recorder.flush()

    result = await session.execute(
        select(ServiceDiagnostic).where(ServiceDiagnostic.workspace_id == workspace.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].occurrences == 5


async def test_throttle_drops_repeat_events_within_the_window(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)
    assert recorder.settings.service_diagnostic_throttle_seconds > 0

    for _ in range(50):
        recorder.record(
            DiagnosticEvent(
                workspace_id=workspace.id,
                service_id=service_row.id,
                kind="cors_missing_allow_origin",
                origin="https://web.example.com",
            )
        )
    # Only the first of the 50 should have reached the queue.
    assert recorder._queue.qsize() == 1  # noqa: SLF001


async def test_a_second_upsert_after_a_resolve_un_resolves_the_row(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
        )
    )
    await recorder.flush()

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
            resolve=True,
        )
    )
    await recorder.flush()

    result = await session.execute(
        select(ServiceDiagnostic).where(ServiceDiagnostic.workspace_id == workspace.id)
    )
    row = result.scalars().one()
    assert row.resolved_at is not None

    # Bypass the throttle deliberately: this call represents a genuinely new failure some time
    # later, not a burst duplicate.
    recorder._last_enqueued.clear()  # noqa: SLF001
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
        )
    )
    await recorder.flush()

    await session.refresh(row)
    assert row.resolved_at is None
    assert row.occurrences == 2


async def test_different_origins_and_kinds_produce_separate_rows(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://a.example.com",
        )
    )
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://b.example.com",
        )
    )
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_preflight_rejected",
            origin="https://a.example.com",
        )
    )
    await recorder.flush()

    result = await session.execute(
        select(ServiceDiagnostic).where(ServiceDiagnostic.workspace_id == workspace.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 3


async def test_request_path_and_method_are_last_write_wins(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
            request_path="/first",
        )
    )
    await recorder.flush()

    recorder._last_enqueued.clear()  # noqa: SLF001
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
            request_path="/second",
        )
    )
    await recorder.flush()

    result = await session.execute(
        select(ServiceDiagnostic).where(ServiceDiagnostic.workspace_id == workspace.id)
    )
    row = result.scalars().one()
    assert row.request_path == "/second"
    assert row.occurrences == 2


async def test_retention_deletes_rows_past_the_cutoff(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    settings = get_settings()
    recorder = _recorder(session_maker)

    stale_cutoff = datetime.now(UTC) - timedelta(
        days=settings.service_diagnostic_retention_days + 1
    )
    old_row = ServiceDiagnostic(
        id=uuid4(),
        workspace_id=workspace.id,
        service_id=service_row.id,
        kind="cors_missing_allow_origin",
        origin="https://old.example.com",
        occurrences=1,
        first_seen_at=stale_cutoff,
        last_seen_at=stale_cutoff,
    )
    session.add(old_row)
    await session.flush()

    # Any event for this workspace triggers the retention sweep for it.
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://new.example.com",
        )
    )
    await recorder.flush()

    result = await session.execute(
        select(ServiceDiagnostic).where(ServiceDiagnostic.workspace_id == workspace.id)
    )
    rows = result.scalars().all()
    origins = {row.origin for row in rows}
    assert "https://old.example.com" not in origins
    assert "https://new.example.com" in origins


async def test_queue_full_drops_the_event_without_raising(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    from app.core.config import Settings

    settings = Settings(service_diagnostic_queue_maxsize=1)
    recorder = ServiceDiagnosticsRecorder(settings, session_factory=session_maker)

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://a.example.com",
        )
    )
    recorder._last_enqueued.clear()  # noqa: SLF001
    # This must not raise even though the queue (maxsize=1) is already full.
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://b.example.com",
        )
    )
    assert recorder._queue.qsize() == 1  # noqa: SLF001


async def test_service_diagnostics_service_only_returns_unresolved_rows(db_session):
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)

    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://open.example.com",
        )
    )
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://resolved.example.com",
        )
    )
    await recorder.flush()
    recorder._last_enqueued.clear()  # noqa: SLF001
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://resolved.example.com",
            resolve=True,
        )
    )
    await recorder.flush()

    reader = ServiceDiagnosticsService(session)
    unresolved = await reader.for_workspace(workspace.id)
    origins = {row.origin for row in unresolved}
    assert origins == {"https://open.example.com"}


async def test_prune_throttle_forgets_keys_whose_window_has_passed(db_session, monkeypatch):
    """Epic 07: the per-key throttle must not grow without bound on a long-lived proxy replica."""
    _session, session_maker = db_session
    recorder = _recorder(session_maker)
    window = recorder.settings.service_diagnostic_throttle_seconds
    clock = [1000.0]
    monkeypatch.setattr("app.services.service_diagnostics.time.monotonic", lambda: clock[0])

    for index in range(5):
        recorder.record(
            DiagnosticEvent(
                workspace_id=uuid4(),
                service_id=uuid4(),
                kind="cors_missing_allow_origin",
                origin=f"https://{index}.example.com",
            )
        )
    clock[0] += window / 2
    fresh_key = DiagnosticEvent(
        workspace_id=uuid4(),
        service_id=uuid4(),
        kind="cors_missing_allow_origin",
        origin="https://fresh.example.com",
    )
    recorder.record(fresh_key)
    clock[0] += window / 2 + 1

    recorder.prune_throttle()

    assert set(recorder._last_enqueued) == {fresh_key.key}  # noqa: SLF001


async def test_stop_flushes_events_still_queued(db_session):
    """Epic 07: a draining proxy replica applies what it already accepted before it exits."""
    session, session_maker = db_session
    workspace, service_row = await _make_service(session)
    recorder = _recorder(session_maker)
    await recorder.start()
    recorder.record(
        DiagnosticEvent(
            workspace_id=workspace.id,
            service_id=service_row.id,
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
        )
    )

    await recorder.stop()

    rows = (
        await session.execute(
            select(ServiceDiagnostic).where(ServiceDiagnostic.service_id == service_row.id)
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_stop_swallows_a_flush_failure(db_session):
    _session, _session_maker = db_session

    class BrokenFactory:
        def __call__(self):
            raise OSError("database is gone")

    recorder = ServiceDiagnosticsRecorder(get_settings(), session_factory=BrokenFactory())
    await recorder.start()
    recorder.record(
        DiagnosticEvent(
            workspace_id=uuid4(),
            service_id=uuid4(),
            kind="cors_missing_allow_origin",
            origin="https://web.example.com",
        )
    )

    await recorder.stop()  # must not raise: diagnostics are best-effort


async def test_stop_survives_a_consumer_that_already_died(db_session):
    _session, session_maker = db_session
    recorder = _recorder(session_maker)

    async def crashed() -> None:
        raise OSError("database went away")

    recorder._task = asyncio.create_task(crashed())  # noqa: SLF001
    await asyncio.sleep(0)

    await recorder.stop()  # must not raise into the proxy's lifespan
