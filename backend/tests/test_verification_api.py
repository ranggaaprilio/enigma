from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import User, VerificationEvidence, VerificationRun, Workspace
from app.services.sandbox.base import RuntimePreviewLink


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


class FakeRuntime:
    """Stand-in for the sandbox runtime: exec always succeeds, preview is inert."""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.calls.append((command, cwd))
        return type(
            "R", (), {"exit_code": 0, "stdout": f"ok:{command}", "stderr": ""}
        )()

    async def preview_link(self, workspace, port):
        return RuntimePreviewLink(url="http://127.0.0.1:1", token="")

    async def expose_port(self, workspace, port):
        return None

    async def health(self):
        return "ok"

    @property
    def configured(self):
        return True

    async def close(self):
        return None


async def override_get_current_user(user: User):
    async def _override() -> User:
        return user

    return _override


async def make_user_workspace(session) -> tuple[User, Workspace]:
    user = User(
        id=uuid4(),
        email=f"verify-{uuid4().hex[:8]}@example.com",
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
        sandbox_id="sandbox-verify",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


def _patch_deps(monkeypatch, session, session_maker, runtime=None):
    async def override_db_session():
        yield session

    monkeypatch.setattr(app, "dependency_overrides", {})
    app.dependency_overrides[get_db_session] = override_db_session

    from app.services.verification import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "async_session_maker", session_maker)
    if runtime is not None:
        monkeypatch.setattr(orch_mod, "get_sandbox_runtime", lambda: runtime)


async def test_create_run_drives_loop_to_review(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    runtime = FakeRuntime()
    _patch_deps(monkeypatch, session, session_maker, runtime)

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/workspaces/{workspace.id}/verification/runs",
            json={
                "plan_ref": "spec:1",
                "test_command": "pytest -q",
                "observe_port": None,
            },
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "review"

    run = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == body["id"]))
    ).scalar_one()
    assert run.status == "review"

    evidence = (
        await session.execute(
            select(VerificationEvidence).where(VerificationEvidence.run_id == run.id)
        )
    ).scalars().all()
    assert any(e.kind == "test" and e.status == "passed" for e in evidence)
    assert any(cmd == "pytest -q" for cmd, _ in runtime.calls)


async def test_list_and_get_runs(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    run = VerificationRun(
        workspace_id=workspace.id, plan_ref="spec:9", status="review"
    )
    session.add(run)
    await session.flush()

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get(f"/workspaces/{workspace.id}/verification/runs")
        detail = await client.get(
            f"/workspaces/{workspace.id}/verification/runs/{run.id}"
        )
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [str(run.id)]
    assert detail.status_code == 200
    assert detail.json()["status"] == "review"
    assert detail.json()["evidence"] == []


async def test_approve_and_reject_gate(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    run = VerificationRun(
        workspace_id=workspace.id, plan_ref="spec:2", status="review"
    )
    session.add(run)
    await session.flush()

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        approved = await client.post(
            f"/workspaces/{workspace.id}/verification/runs/{run.id}/approve"
        )
        reapproved = await client.post(
            f"/workspaces/{workspace.id}/verification/runs/{run.id}/approve"
        )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert reapproved.status_code == 409  # not awaiting review anymore

    run2 = VerificationRun(
        workspace_id=workspace.id, plan_ref="spec:3", status="review"
    )
    session.add(run2)
    await session.flush()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        rejected = await client.post(
            f"/workspaces/{workspace.id}/verification/runs/{run2.id}/reject"
        )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


async def test_second_active_run_is_conflicted(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    runtime = FakeRuntime()
    _patch_deps(monkeypatch, session, session_maker, runtime)

    # An active run already occupies the workspace guard.
    active = VerificationRun(
        workspace_id=workspace.id, plan_ref="spec:4", status="testing"
    )
    session.add(active)
    await session.flush()

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/workspaces/{workspace.id}/verification/runs",
            json={"plan_ref": "spec:5", "test_command": "true"},
        )
    assert resp.status_code == 409
    assert "active" in resp.json()["detail"]


async def test_unknown_run_404(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/workspaces/{workspace.id}/verification/runs/{uuid4()}"
        )
    assert resp.status_code == 404
