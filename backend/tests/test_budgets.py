from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agents as agents_module
from app.api import auth as auth_module
from app.api import budgets as budgets_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import (
    AgentRun,
    AuditLog,
    Notification,
    Project,
    ProjectMember,
    Team,
    TeamMember,
    UsageBudget,
    User,
    Workspace,
)
from app.services.agents import AgentService
from app.services.budgets import (
    BudgetAccessError,
    BudgetConflictError,
    BudgetExceededError,
    BudgetService,
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


class _ImmediateTask:
    def add_done_callback(self, callback):
        return None


def _fake_create_task(coro, *args, **kwargs):
    coro.close()
    return _ImmediateTask()


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
        repo_url="https://github.com/example/budget",
        default_branch="main",
        runtime="daytona",
        workspace_path="/workspace",
        status="ready",
    )


def _run(ws: Workspace, user: User, *, cost: str, tokens: int = 100) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        workspace_id=ws.id,
        user_id=user.id,
        prompt="p",
        status="completed",
        provider="openai",
        model="gpt-4.1-mini",
        tokens_used=tokens,
        estimated_cost=Decimal(cost),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


async def _seed(session) -> SimpleNamespace:
    owner = _user("bowner")
    lead = _user("blead")
    admin = _user("badmin")
    member = _user("bmember")
    session.add_all([owner, lead, admin, member])
    await session.flush()

    project = Project(id=uuid4(), name="BP", slug=f"bp-{uuid4()}", created_by=admin.id)
    session.add(project)
    await session.flush()
    session.add_all(
        [
            ProjectMember(id=uuid4(), project_id=project.id, user_id=admin.id, role="admin"),
            ProjectMember(id=uuid4(), project_id=project.id, user_id=member.id, role="member"),
            ProjectMember(id=uuid4(), project_id=project.id, user_id=lead.id, role="member"),
        ]
    )
    team = Team(id=uuid4(), project_id=project.id, name="BT", slug=f"bt-{uuid4()}")
    session.add(team)
    await session.flush()
    session.add(TeamMember(id=uuid4(), team_id=team.id, user_id=lead.id, role="lead"))

    ws_solo = _workspace(owner, None, "wsolo")
    ws_team = _workspace(admin, team, "wteam")
    session.add_all([ws_solo, ws_team])
    await session.commit()

    return SimpleNamespace(
        owner=owner,
        lead=lead,
        admin=admin,
        member=member,
        project=project,
        team=team,
        ws_solo=ws_solo,
        ws_team=ws_team,
    )


class TestBudgetService:
    async def test_create_and_status_warning(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        session.add(_run(data.ws_solo, data.owner, cost="6.00"))
        await session.commit()

        svc = BudgetService(session)
        budget = await svc.create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=10.0,
            limit_tokens=None,
            threshold_percent=50,
            enforce=False,
        )
        cost, tokens, status = await svc.status_for(budget)
        assert cost == Decimal("6.00")
        assert status == "warning"

    async def test_workspace_budget_requires_owner(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        svc = BudgetService(session)
        with pytest.raises(BudgetAccessError):
            await svc.create(
                user_id=data.member.id,
                scope="workspace",
                scope_id=data.ws_solo.id,
                period="monthly",
                limit_cost=10.0,
                limit_tokens=None,
                threshold_percent=80,
                enforce=False,
            )

    async def test_team_budget_managed_by_lead_not_member(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        svc = BudgetService(session)
        # Team lead may create.
        await svc.create(
            user_id=data.lead.id,
            scope="team",
            scope_id=data.team.id,
            period="monthly",
            limit_cost=100.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=False,
        )
        # Plain project member may not.
        with pytest.raises(BudgetAccessError):
            await svc.create(
                user_id=data.member.id,
                scope="team",
                scope_id=data.team.id,
                period="daily",
                limit_cost=100.0,
                limit_tokens=None,
                threshold_percent=80,
                enforce=False,
            )

    async def test_project_budget_requires_admin(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        svc = BudgetService(session)
        await svc.create(
            user_id=data.admin.id,
            scope="project",
            scope_id=data.project.id,
            period="monthly",
            limit_cost=500.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=False,
        )
        with pytest.raises(BudgetAccessError):
            await svc.create(
                user_id=data.member.id,
                scope="project",
                scope_id=data.project.id,
                period="daily",
                limit_cost=500.0,
                limit_tokens=None,
                threshold_percent=80,
                enforce=False,
            )

    async def test_duplicate_budget_conflicts(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        svc = BudgetService(session)
        kwargs = dict(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=10.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=False,
        )
        await svc.create(**kwargs)
        with pytest.raises(BudgetConflictError):
            await svc.create(**kwargs)

    async def test_create_writes_audit_log(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        svc = BudgetService(session)
        await svc.create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=10.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=False,
        )
        logs = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "budget_create")
                )
            )
            .scalars()
            .all()
        )
        assert any(log.target_type == "usage_budget" for log in logs)

    async def test_update_and_delete(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        svc = BudgetService(session)
        budget = await svc.create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=10.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=False,
        )
        updated = await svc.update(
            user_id=data.owner.id,
            budget_id=budget.id,
            changes={"limit_cost": 25.0, "enforce": True},
        )
        assert updated.limit_cost == Decimal("25.0")
        assert updated.enforce is True

        await svc.delete(user_id=data.owner.id, budget_id=budget.id)
        assert await session.get(UsageBudget, budget.id) is None

    async def test_enforce_blocks_create_run(self, db_session):
        session, session_maker = db_session
        data = await _seed(session)
        session.add(_run(data.ws_solo, data.owner, cost="2.00"))
        await session.commit()
        svc = BudgetService(session)
        await svc.create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=1.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=True,
        )

        agent_service = AgentService(
            session, session_maker, Settings(agent_allow_mock_provider=True)
        )
        with pytest.raises(BudgetExceededError):
            await agent_service.create_run(data.ws_solo.id, data.owner.id, "please run")

    async def test_threshold_warning_notification(self, db_session, monkeypatch):
        session, session_maker = db_session
        data = await _seed(session)
        session.add(_run(data.ws_solo, data.owner, cost="6.00"))
        await session.commit()
        svc = BudgetService(session)
        await svc.create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=10.0,
            limit_tokens=None,
            threshold_percent=50,
            enforce=False,
        )

        monkeypatch.setattr("app.services.agents.asyncio.create_task", _fake_create_task)
        agent_service = AgentService(
            session, session_maker, Settings(agent_allow_mock_provider=True)
        )
        run = await agent_service.create_run(data.ws_solo.id, data.owner.id, "please run")
        assert run.id is not None

        warnings = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.workspace_id == data.ws_solo.id,
                        Notification.notification_type == "budget_warning",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(warnings) >= 1


def _override_current_user(user):
    async def _override():
        return user

    return _override


def _override_db(session):
    async def _override():
        yield session

    return _override


def _override_budget_service(session):
    async def _override():
        return BudgetService(session)

    return _override


def _override_agent_service(session, session_maker):
    async def _override():
        return AgentService(
            session=session,
            session_maker=session_maker,
            settings=Settings(agent_allow_mock_provider=True),
        )

    return _override


class TestBudgetEndpoints:
    async def test_create_budget_endpoint_owner(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        session.add(_run(data.ws_solo, data.owner, cost="6.00"))
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = _override_current_user(data.owner)
        app.dependency_overrides[get_db_session] = _override_db(session)
        app.dependency_overrides[budgets_module.get_budget_service] = _override_budget_service(
            session
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/budgets",
                    json={
                        "scope": "workspace",
                        "scope_id": str(data.ws_solo.id),
                        "period": "monthly",
                        "limit_cost": 10.0,
                        "threshold_percent": 50,
                        "enforce": False,
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 201
        body = response.json()
        assert body["current_cost"] == pytest.approx(6.0)
        assert body["status"] == "warning"

    async def test_create_budget_endpoint_forbidden_for_non_owner(self, db_session):
        session, _ = db_session
        data = await _seed(session)

        app.dependency_overrides[auth_module.get_current_user] = _override_current_user(data.member)
        app.dependency_overrides[get_db_session] = _override_db(session)
        app.dependency_overrides[budgets_module.get_budget_service] = _override_budget_service(
            session
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/budgets",
                    json={
                        "scope": "workspace",
                        "scope_id": str(data.ws_solo.id),
                        "limit_cost": 10.0,
                    },
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 403

    async def test_list_budgets_endpoint(self, db_session):
        session, _ = db_session
        data = await _seed(session)
        await BudgetService(session).create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=10.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=False,
        )

        app.dependency_overrides[auth_module.get_current_user] = _override_current_user(data.owner)
        app.dependency_overrides[get_db_session] = _override_db(session)
        app.dependency_overrides[budgets_module.get_budget_service] = _override_budget_service(
            session
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/budgets", params={"scope": "workspace", "id": str(data.ws_solo.id)}
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["scope"] == "workspace"

    async def test_create_run_over_enforce_budget_returns_402(self, db_session):
        session, session_maker = db_session
        data = await _seed(session)
        session.add(_run(data.ws_solo, data.owner, cost="5.00"))
        await session.commit()
        await BudgetService(session).create(
            user_id=data.owner.id,
            scope="workspace",
            scope_id=data.ws_solo.id,
            period="monthly",
            limit_cost=1.0,
            limit_tokens=None,
            threshold_percent=80,
            enforce=True,
        )

        app.dependency_overrides[auth_module.get_current_user] = _override_current_user(data.owner)
        app.dependency_overrides[get_db_session] = _override_db(session)
        app.dependency_overrides[agents_module.get_agent_service] = _override_agent_service(
            session, session_maker
        )
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/workspaces/{data.ws_solo.id}/agent/runs",
                    json={"prompt": "please run"},
                )
        finally:
            app.dependency_overrides.clear()

        assert response.status_code == 402
