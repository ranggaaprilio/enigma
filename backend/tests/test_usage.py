from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import dashboard as dashboard_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import AgentRun, Project, ProjectMember, Team, User, Workspace
from app.services.usage import SqlAlchemyUsageService, UsageAccessError


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


def _user(label: str) -> User:
    return User(
        id=uuid4(),
        email=f"{label}-{uuid4()}@example.com",
        password_hash=hash_password("password"),
        role="developer",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _workspace(owner: User, team: Team | None, label: str) -> Workspace:
    uid = uuid4()
    return Workspace(
        id=uuid4(),
        owner_id=owner.id,
        team_id=team.id if team is not None else None,
        name=f"{label}-{uid}",
        slug=f"{label}-{uid}",
        repo_url="https://github.com/example/usage",
        default_branch="main",
        runtime="daytona",
        workspace_path="/workspace",
        status="ready",
    )


def _run(ws: Workspace, user: User, *, cost: str, tokens: int, started: datetime) -> AgentRun:
    inp = tokens // 2
    return AgentRun(
        id=uuid4(),
        workspace_id=ws.id,
        user_id=user.id,
        prompt="p",
        status="completed",
        provider="openai",
        model="gpt-4.1-mini",
        tokens_used=tokens,
        input_tokens=inp,
        output_tokens=tokens - inp,
        estimated_cost=Decimal(cost),
        started_at=started,
        completed_at=started,
    )


async def _seed(session) -> SimpleNamespace:
    now = datetime.now(UTC)
    recent = now - timedelta(days=1)
    old = now - timedelta(days=60)

    owner = _user("owner")
    member = _user("member")
    stranger = _user("stranger")
    session.add_all([owner, member, stranger])
    await session.flush()

    project = Project(id=uuid4(), name="P", slug=f"p-{uuid4()}", created_by=owner.id)
    session.add(project)
    await session.flush()
    session.add_all(
        [
            ProjectMember(
                id=uuid4(), project_id=project.id, user_id=owner.id, role="admin"
            ),
            ProjectMember(
                id=uuid4(), project_id=project.id, user_id=member.id, role="member"
            ),
        ]
    )

    team = Team(id=uuid4(), project_id=project.id, name="T", slug=f"t-{uuid4()}")
    session.add(team)
    await session.flush()

    ws = _workspace(owner, team, "w")
    session.add(ws)
    await session.flush()

    session.add_all(
        [
            _run(ws, owner, cost="1.00", tokens=100, started=recent),
            _run(ws, member, cost="2.00", tokens=200, started=recent),
            _run(ws, owner, cost="5.00", tokens=500, started=old),  # outside 30-day window
        ]
    )
    await session.commit()

    return SimpleNamespace(
        owner=owner,
        member=member,
        stranger=stranger,
        project=project,
        team=team,
        ws=ws,
        now=now,
    )


class TestUsageService:
    async def test_project_scope_totals_and_breakdown(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        res = await svc.usage_summary(
            data.owner.id,
            "project",
            data.project.id,
            data.now - timedelta(days=30),
            data.now,
        )
        assert res.totals.run_count == 2
        assert res.totals.tokens_used == 300
        assert res.totals.estimated_cost == pytest.approx(3.0)
        assert res.breakdown_dimension == "team"
        assert len(res.breakdown) == 1
        assert res.breakdown[0].id == data.team.id
        assert res.breakdown[0].estimated_cost == pytest.approx(3.0)
        # Top spenders ordered by cost desc: member (2.0) then owner (1.0).
        assert [item.estimated_cost for item in res.top_spenders] == pytest.approx([2.0, 1.0])
        assert res.top_spenders[0].id == data.member.id

    async def test_team_scope_breaks_down_by_workspace(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        res = await svc.usage_summary(
            data.owner.id, "team", data.team.id, data.now - timedelta(days=30), data.now
        )
        assert res.breakdown_dimension == "workspace"
        assert len(res.breakdown) == 1
        assert res.breakdown[0].id == data.ws.id
        assert res.breakdown[0].estimated_cost == pytest.approx(3.0)

    async def test_workspace_scope_breaks_down_by_user(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        res = await svc.usage_summary(
            data.owner.id, "workspace", data.ws.id, data.now - timedelta(days=30), data.now
        )
        assert res.breakdown_dimension == "user"
        by_id = {item.id: item.estimated_cost for item in res.breakdown}
        assert by_id[data.owner.id] == pytest.approx(1.0)
        assert by_id[data.member.id] == pytest.approx(2.0)

    async def test_time_window_includes_older_runs_when_widened(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        res = await svc.usage_summary(
            data.owner.id, "project", data.project.id, data.now - timedelta(days=90), data.now
        )
        assert res.totals.run_count == 3
        assert res.totals.estimated_cost == pytest.approx(8.0)

    async def test_no_scope_aggregates_accessible_and_breaks_down_by_project(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        res = await svc.usage_summary(
            data.owner.id, None, None, data.now - timedelta(days=90), data.now
        )
        assert res.totals.estimated_cost == pytest.approx(8.0)
        assert res.breakdown_dimension == "project"
        assert any(item.id == data.project.id for item in res.breakdown)

    async def test_stranger_denied_project_scope(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        with pytest.raises(UsageAccessError):
            await svc.usage_summary(
                data.stranger.id,
                "project",
                data.project.id,
                data.now - timedelta(days=30),
                data.now,
            )

    async def test_stranger_denied_workspace_scope(self, db_session):
        data = await _seed(db_session)
        svc = SqlAlchemyUsageService(db_session)
        with pytest.raises(UsageAccessError):
            await svc.usage_summary(
                data.stranger.id,
                "workspace",
                data.ws.id,
                data.now - timedelta(days=30),
                data.now,
            )


def _override_current_user(user):
    async def _override():
        return user

    return _override


def _override_db(session):
    async def _override():
        yield session

    return _override


def _override_usage_service(session):
    async def _override():
        return SqlAlchemyUsageService(session)

    return _override


class TestUsageEndpoint:
    async def test_usage_endpoint_returns_totals(self, db_session):
        data = await _seed(db_session)
        app.dependency_overrides[auth_module.get_current_user] = _override_current_user(data.owner)
        app.dependency_overrides[get_db_session] = _override_db(db_session)
        app.dependency_overrides[dashboard_module.get_usage_service] = _override_usage_service(
            db_session
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/dashboard/usage",
                    params={
                        "scope": "project",
                        "id": str(data.project.id),
                        "from": (data.now - timedelta(days=30)).isoformat(),
                        "to": data.now.isoformat(),
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["estimated_cost"] == pytest.approx(3.0)
        assert body["totals"]["run_count"] == 2
        assert body["breakdown_dimension"] == "team"

    async def test_usage_endpoint_denies_stranger(self, db_session):
        data = await _seed(db_session)
        app.dependency_overrides[auth_module.get_current_user] = _override_current_user(
            data.stranger
        )
        app.dependency_overrides[get_db_session] = _override_db(db_session)
        app.dependency_overrides[dashboard_module.get_usage_service] = _override_usage_service(
            db_session
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/dashboard/usage",
                    params={"scope": "project", "id": str(data.project.id)},
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403
