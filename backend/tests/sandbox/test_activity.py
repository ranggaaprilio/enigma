"""The shared idle clock (local-docker-runtime Epic 05): debounced upserts per process."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import SandboxActivity, User, Workspace
from app.services.sandbox.activity import SandboxActivityRecorder


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _workspace(maker) -> Workspace:
    async with maker() as session:
        user = User(id=uuid4(), email=f"{uuid4().hex}@example.com", password_hash="x",
                    role="owner", is_active=True)
        workspace = Workspace(id=uuid4(), owner_id=user.id, name="Idle", slug=uuid4().hex[:12],
                              default_branch="main", runtime="docker", status="ready",
                              workspace_path="workspace", daytona_sandbox_id=uuid4().hex)
        session.add(user)
        await session.flush()
        session.add(workspace)
        await session.commit()
        return workspace


async def _cleanup(maker, workspace: Workspace) -> None:
    async with maker() as session:
        row = await session.get(Workspace, workspace.id)
        user = await session.get(User, workspace.owner_id)
        await session.delete(row)  # cascades to sandbox_activity
        await session.flush()
        await session.delete(user)
        await session.commit()


async def _stamp(maker, workspace: Workspace):
    async with maker() as session:
        return await session.scalar(
            select(SandboxActivity.last_activity_at).where(
                SandboxActivity.workspace_id == workspace.id
            )
        )


async def test_touch_upserts_and_is_debounced_per_recorder(maker) -> None:
    workspace = await _workspace(maker)
    clock = [100.0]
    recorder = SandboxActivityRecorder(maker, debounce_seconds=30, clock=lambda: clock[0])
    try:
        await recorder.touch(workspace.id)
        first = await _stamp(maker, workspace)
        assert first is not None

        clock[0] += 10  # inside the window: no write
        await recorder.touch(workspace.id)
        assert await _stamp(maker, workspace) == first

        clock[0] += 30  # past it: the row moves forward (Postgres' clock, not ours)
        await recorder.touch(workspace.id)
        assert await _stamp(maker, workspace) > first
    finally:
        await _cleanup(maker, workspace)


async def test_a_failed_write_never_raises_and_opens_no_debounce_window() -> None:
    """Activity tracking must never break the exec or preview it rides on, and a write
    that failed must be retried on the next touch rather than skipped for 30 s."""
    attempts: list[int] = []

    def broken():
        attempts.append(1)
        raise OSError("database down")

    recorder = SandboxActivityRecorder(broken, debounce_seconds=30, clock=lambda: 0.0)
    workspace_id = uuid4()

    await recorder.touch(workspace_id)
    await recorder.touch(workspace_id)

    assert len(attempts) == 2
