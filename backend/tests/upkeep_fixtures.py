"""Database fixtures for the sandbox upkeep tests: a session maker that cleans up the workspaces
(and their owners) a test creates, and a helper that creates one, optionally idle."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, Workspace


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    created: list[Workspace] = []
    maker.created = created  # type: ignore[attr-defined]
    yield maker
    async with maker() as session:
        for workspace in created:
            row = await session.get(Workspace, workspace.id)
            if row is not None:
                await session.delete(row)
                await session.flush()
            user = await session.get(User, workspace.owner_id)
            if user is not None:
                await session.delete(user)
        await session.commit()
    await engine.dispose()


async def _workspace(maker, *, runtime: str = "opensandbox", sandbox_id: str | None = None,
                     idle_minutes: int | None = None) -> Workspace:
    async with maker() as session:
        user = User(id=uuid4(), email=f"{uuid4().hex}@example.com", password_hash="x",
                    role="owner", is_active=True)
        workspace = Workspace(id=uuid4(), owner_id=user.id, name="Upkeep", slug=uuid4().hex[:12],
                              default_branch="main", runtime=runtime, status="ready",
                              workspace_path="workspace", sandbox_id=sandbox_id)
        session.add(user)
        await session.flush()
        session.add(workspace)
        await session.flush()
        if idle_minutes is not None:
            await session.execute(
                text(
                    "INSERT INTO sandbox_activity (workspace_id, last_activity_at) "
                    "VALUES (:id, clock_timestamp() - make_interval(mins => :m))"
                ),
                {"id": workspace.id, "m": idle_minutes},
            )
        await session.commit()
    maker.created.append(workspace)
    return workspace
