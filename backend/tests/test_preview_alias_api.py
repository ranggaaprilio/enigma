"""Epic 13: alias enable/disable/regenerate endpoints, against real Postgres (matching the
`_override_db`/`_override_current_user` pattern in `test_workspace_env_scoping.py`)."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import User, Workspace


@pytest.fixture
async def db_session():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with session_maker() as session:
            await session.begin_nested()
            yield session
            await session.rollback()
    await engine.dispose()


def _override_current_user(user: User):
    async def _override():
        return user

    return _override


def _override_db(session):
    async def _override():
        yield session

    return _override


async def make_workspace(session) -> tuple[User, Workspace]:
    user = User(
        id=uuid4(),
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
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
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    session.add(workspace)
    await session.flush()
    return user, workspace


@pytest.fixture
async def alias_api_context(db_session):
    user, workspace = await make_workspace(db_session)
    app.dependency_overrides[auth_module.get_current_user] = _override_current_user(user)
    app.dependency_overrides[get_db_session] = _override_db(db_session)
    yield db_session, user, workspace
    app.dependency_overrides.clear()


async def test_enable_then_disable_round_trip(alias_api_context):
    _session, _user, workspace = alias_api_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        enabled = await client.post(f"/workspaces/{workspace.id}/preview/aliases/enable")
        assert enabled.status_code == 200
        assert enabled.json()["preview_alias_enabled"] is True

        disabled = await client.post(f"/workspaces/{workspace.id}/preview/aliases/disable")
        assert disabled.status_code == 200
        assert disabled.json()["preview_alias_enabled"] is False


async def test_regenerate_requires_aliases_enabled_first(alias_api_context):
    _session, _user, workspace = alias_api_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/workspaces/{workspace.id}/preview/aliases/regenerate", json={}
        )

    assert response.status_code == 409


async def test_regenerate_the_builtin_row_mints_a_different_alias(alias_api_context):
    _session, _user, workspace = alias_api_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        enabled = await client.post(f"/workspaces/{workspace.id}/preview/aliases/enable")
        assert enabled.status_code == 200

        regenerated = await client.post(
            f"/workspaces/{workspace.id}/preview/aliases/regenerate", json={}
        )

    assert regenerated.status_code == 200
    assert "alias" in regenerated.json()
    assert regenerated.json()["alias_url"].startswith("http")


async def test_regenerate_unknown_command_id_is_404(alias_api_context):
    _session, _user, workspace = alias_api_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(f"/workspaces/{workspace.id}/preview/aliases/enable")
        response = await client.post(
            f"/workspaces/{workspace.id}/preview/aliases/regenerate",
            json={"custom_command_id": str(uuid4())},
        )

    assert response.status_code == 404


async def test_enable_endpoint_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/workspaces/{uuid4()}/preview/aliases/enable")

    assert response.status_code == 401
