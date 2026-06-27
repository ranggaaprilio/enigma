# ruff: noqa: E501

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import specs as specs_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.main import app
from app.models import AgentRun, AuditLog, Notification, SpecPhase, SpecWorkflow, User, Workspace
from app.services.speckit import (
    PHASE_ORDER,
    SpecGateError,
    SpecKitService,
    SpecPhaseNotReviewableError,
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


class FakeRuntime:
    """Stand-in for DaytonaRuntime. Records exec calls; never touches a sandbox.

    ``test -e <path>`` reports the path exists when it is in ``existing``; ``cat`` always
    reports the file is absent (so prompt assembly degrades gracefully); everything else
    (git worktree add, git add/commit, mkdir, specify init) succeeds.
    """

    def __init__(self, existing=(".git", ".specify")):
        self.existing = set(existing)
        self.calls: list[tuple[str, str | None]] = []

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.calls.append((command, cwd))
        exit_code, stdout = 0, ""
        if command.startswith("test -e "):
            target = command[len("test -e ") :].strip().strip("'\"")
            exit_code = 0 if target in self.existing else 1
        elif command.startswith("cat "):
            exit_code = 1
        return type("R", (), {"exit_code": exit_code, "stdout": stdout, "stderr": ""})()


async def override_get_current_user(user: User):
    async def _override() -> User:
        return user

    return _override


def speckit_override(session, session_maker, runtime):
    async def _override() -> SpecKitService:
        return SpecKitService(session, session_maker, Settings(), runtime=runtime)

    return _override


async def make_user_workspace(session, *, owner_email: str) -> tuple[User, Workspace]:
    user = User(
        id=uuid4(),
        email=owner_email,
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
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


async def make_workflow(
    session, workspace, user, *, phase_status: dict[str, str] | None = None
) -> SpecWorkflow:
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="Add auth",
        feature_slug="001-add-auth",
        description="We need login",
        branch="001-add-auth",
        spec_dir="specs/001-add-auth",
        worktree_path="worktrees/001-add-auth",
        status="running",
        current_phase="specify",
    )
    session.add(workflow)
    await session.flush()
    statuses = phase_status or {}
    for phase_name in PHASE_ORDER:
        session.add(
            SpecPhase(
                workflow_id=workflow.id,
                phase=phase_name,
                status=statuses.get(phase_name, "pending"),
            )
        )
    await session.flush()
    return workflow


# --- the hard gate: implement is blocked until plan + tasks are approved ----------------


async def test_implement_gate_blocks_until_plan_and_tasks_approved(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"gate-{uuid4()}@example.com")
    workflow = await make_workflow(
        session,
        workspace,
        user,
        phase_status={"specify": "approved", "plan": "pending", "tasks": "pending"},
    )
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    with pytest.raises(SpecGateError):
        await service.run_phase(workspace.id, workflow.id, "implement", user.id)


async def test_implement_gate_allows_when_plan_and_tasks_approved(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"gate2-{uuid4()}@example.com")
    workflow = await make_workflow(
        session,
        workspace,
        user,
        phase_status={"plan": "approved", "tasks": "approved"},
    )
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    # Prerequisites are satisfied; the gate does not raise.
    await service._assert_prerequisites(workflow.id, "implement")


async def test_implement_gate_returns_409_via_api(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"api-{uuid4()}@example.com")
    workflow = await make_workflow(
        session, workspace, user, phase_status={"plan": "pending", "tasks": "pending"}
    )
    await session.commit()

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(user)
    app.dependency_overrides[specs_module.get_speckit_service] = speckit_override(
        session, session_maker, FakeRuntime()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/workspaces/{workspace.id}/spec/workflows/{workflow.id}/phases/implement/run"
        )
    app.dependency_overrides.clear()

    assert response.status_code == 409


# --- start workflow generates spec.md and halts -----------------------------------------


async def test_start_workflow_creates_phases_and_runs_specify(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"start-{uuid4()}@example.com")
    await session.commit()

    async def fake_create_run(self, workspace_id, user_id, prompt, working_dir=None):
        run = AgentRun(
            workspace_id=workspace_id,
            user_id=user_id,
            prompt=prompt,
            status="thinking",
            provider="mock",
            model="mock-agent",
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def noop_finalize(self, *args, **kwargs):
        return None

    monkeypatch.setattr("app.services.agents.AgentService.create_run", fake_create_run)
    monkeypatch.setattr(SpecKitService, "_finalize_phase", noop_finalize)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    workflow = await service.start_workflow(workspace.id, user.id, "Add Auth", "We need login")

    assert len(workflow.phases) == len(PHASE_ORDER)
    specify = next(p for p in workflow.phases if p.phase == "specify")
    assert specify.status == "running"
    assert specify.agent_run_id is not None
    assert workflow.worktree_path == "worktrees/001-add-auth"

    audits = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "spec_workflow_start")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) >= 1


# --- approval gate transitions -----------------------------------------------------------


async def test_approve_phase_records_reviewer_and_audit(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"appr-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"plan": "awaiting_review"})
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    phase = await service.approve_phase(workspace.id, workflow.id, "plan", user.id)

    assert phase.status == "approved"
    assert phase.reviewed_by == user.id
    assert phase.reviewed_at is not None

    audits = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "spec_phase_approved")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) >= 1


async def test_approve_non_reviewable_phase_raises(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"nr-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user)  # all pending
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    with pytest.raises(SpecPhaseNotReviewableError):
        await service.approve_phase(workspace.id, workflow.id, "specify", user.id)


async def test_request_changes_bumps_attempt_and_reruns(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"rc-{uuid4()}@example.com")
    workflow = await make_workflow(
        session,
        workspace,
        user,
        phase_status={"specify": "approved", "plan": "awaiting_review"},
    )
    await session.commit()

    async def fake_create_run(self, workspace_id, user_id, prompt, working_dir=None):
        run = AgentRun(
            workspace_id=workspace_id,
            user_id=user_id,
            prompt=prompt,
            status="thinking",
            provider="mock",
            model="mock-agent",
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def noop_finalize(self, *args, **kwargs):
        return None

    monkeypatch.setattr("app.services.agents.AgentService.create_run", fake_create_run)
    monkeypatch.setattr(SpecKitService, "_finalize_phase", noop_finalize)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    phase = await service.request_changes(
        workspace.id, workflow.id, "plan", user.id, "Please cover the logout flow"
    )

    assert phase.status == "running"
    assert phase.attempt == 2
    assert phase.review_notes == "Please cover the logout flow"

    audits = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "spec_phase_changes_requested")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) >= 1


# --- authorization -----------------------------------------------------------------------


async def test_list_workflows_non_owner_returns_404(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"own-{uuid4()}@example.com")
    other = User(
        id=uuid4(),
        email=f"other-{uuid4()}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(other)
    await session.flush()
    await session.commit()

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(other)
    app.dependency_overrides[specs_module.get_speckit_service] = speckit_override(
        session, session_maker, FakeRuntime()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace.id}/spec/workflows")
    app.dependency_overrides.clear()

    assert response.status_code == 404


# --- finalizer: a completed review phase halts at awaiting_review ------------------------


async def test_finalizer_sets_review_phase_to_awaiting_review(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"fin-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"specify": "running"})
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="generate spec",
        status="completed",
        provider="mock",
        model="mock-agent",
    )
    session.add(run)
    await session.flush()
    specify = (
        await session.execute(
            select(SpecPhase).where(
                SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "specify"
            )
        )
    ).scalar_one()
    specify.agent_run_id = run.id
    specify.artifact_path = "specs/001-add-auth/spec.md"
    session.add(specify)
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service._finalize_phase(workflow.id, "specify", run.id, user.id)

    async with session_maker() as verify:
        refreshed = (
            await verify.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "specify"
                )
            )
        ).scalar_one()
        assert refreshed.status == "awaiting_review"
        notes = (
            (
                await verify.execute(
                    select(Notification).where(
                        Notification.notification_type == "spec_phase_awaiting_review"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notes) >= 1
