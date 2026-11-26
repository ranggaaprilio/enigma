# ruff: noqa: E501

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import specs as specs_module
from app.core.config import Settings, get_settings
from app.core.lifecycle import ServiceDrainingError
from app.core.security import hash_password
from app.main import app
from app.models import AgentRun, AuditLog, Notification, SpecPhase, SpecWorkflow, User, Workspace
from app.services.speckit import (
    PHASE_ORDER,
    PHASE_PLANS,
    InvalidArtifactPathError,
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


class ScriptedRuntime(FakeRuntime):
    """FakeRuntime that can also serve ``cat`` content and ``ls -1`` directory listings."""

    def __init__(self, existing=(".git", ".specify"), files=None, listings=None):
        super().__init__(existing)
        self.files = files or {}  # rel_path -> content (served by ``cat``)
        self.listings = listings or {}  # rel_dir -> [names] (served by ``ls -1``)

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.calls.append((command, cwd))
        exit_code, stdout = 0, ""
        if command.startswith("test -e "):
            target = command[len("test -e ") :].strip().strip("'\"")
            exit_code = 0 if target in self.existing else 1
        elif command.startswith("cat "):
            target = command[len("cat ") :].strip().strip("'\"")
            if target in self.files:
                stdout = self.files[target]
            else:
                exit_code = 1
        elif command.startswith("ls -1 "):
            target = command[len("ls -1 ") :].strip().strip("'\"")
            names = self.listings.get(target)
            if names is None:
                exit_code = 1
            else:
                stdout = "\n".join(names)
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
        sandbox_id="sandbox-demo",
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


# --- start workflow: constitution-first, else straight to specify -----------------------


# A filled-in constitution (no bracketed UPPER_SNAKE template placeholders remain).
RATIFIED_CONSTITUTION = """# Demo Constitution

## Core Principles
- Library-first: every feature ships as a standalone library.
- Test-driven: tests are written before implementation.

**Ratified**: 2026-01-01 | **Version**: 1.0.0
"""


def _fake_create_run():
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

    return fake_create_run


async def _noop_finalize(self, *args, **kwargs):
    return None


async def test_start_workflow_without_constitution_runs_constitution_first(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"start-{uuid4()}@example.com")
    await session.commit()

    monkeypatch.setattr("app.services.agents.AgentService.create_run", _fake_create_run())
    monkeypatch.setattr(SpecKitService, "_finalize_phase", _noop_finalize)

    # FakeRuntime's ``cat`` reports the constitution absent -> unratified -> generate it first.
    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    workflow = await service.start_workflow(workspace.id, user.id, "Add Auth", "We need login")

    assert len(workflow.phases) == len(PHASE_ORDER)
    assert workflow.current_phase == "constitution"
    constitution = next(p for p in workflow.phases if p.phase == "constitution")
    specify = next(p for p in workflow.phases if p.phase == "specify")
    assert constitution.status == "running"
    assert constitution.agent_run_id is not None
    assert specify.status == "pending"

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


async def test_start_workflow_with_ratified_constitution_runs_specify(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"rat-{uuid4()}@example.com")
    await session.commit()

    monkeypatch.setattr("app.services.agents.AgentService.create_run", _fake_create_run())
    monkeypatch.setattr(SpecKitService, "_finalize_phase", _noop_finalize)

    # A ratified constitution already exists -> skip straight to specify (prior behavior).
    runtime = ScriptedRuntime(
        files={PHASE_PLANS["constitution"].artifact: RATIFIED_CONSTITUTION}
    )
    service = SpecKitService(session, session_maker, Settings(), runtime=runtime)
    workflow = await service.start_workflow(workspace.id, user.id, "Add Auth", "We need login")

    assert workflow.current_phase == "specify"
    constitution = next(p for p in workflow.phases if p.phase == "constitution")
    specify = next(p for p in workflow.phases if p.phase == "specify")
    assert specify.status == "running"
    assert specify.agent_run_id is not None
    assert constitution.status == "pending"
    assert workflow.worktree_path == "worktrees/001-add-auth"


async def test_constitution_finalizer_auto_chains_to_specify(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"chain-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"constitution": "running"})
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="generate constitution",
        status="completed",
        provider="mock",
        model="mock-agent",
    )
    session.add(run)
    await session.flush()
    await session.commit()

    called: dict[str, str] = {}

    async def spy_run_phase(self, workspace_id, workflow_id, phase_name, user_id, feedback=None):
        # Record the chained call and flip specify to running without scheduling a background
        # finalizer task (which would otherwise poll a non-terminal run for the whole timeout).
        called["phase"] = phase_name
        specify = (
            await self.session.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow_id, SpecPhase.phase == "specify"
                )
            )
        ).scalar_one()
        specify.status = "running"
        self.session.add(specify)
        await self.session.commit()
        return specify

    monkeypatch.setattr(SpecKitService, "run_phase", spy_run_phase)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service._finalize_phase(workflow.id, "constitution", run.id, user.id)

    assert called.get("phase") == "specify"
    async with session_maker() as verify:
        constitution = (
            await verify.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "constitution"
                )
            )
        ).scalar_one()
        specify = (
            await verify.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "specify"
                )
            )
        ).scalar_one()
        assert constitution.status == "completed"
        assert specify.status == "running"


async def test_phase_finalizer_is_registered_for_the_shutdown_drain(db_session, monkeypatch):
    """Finalizers live in the shared run registry so shutdown waits for (or cancels and sweeps)
    them instead of erroring a phase whose agent run finished during the grace window."""
    from app.services.runtime import run_registry

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"reg-{uuid4()}@example.com")
    await session.commit()
    release = asyncio.Event()

    async def blocking_finalize(self, *args, **kwargs):
        await release.wait()

    monkeypatch.setattr("app.services.agents.AgentService.create_run", _fake_create_run())
    monkeypatch.setattr(SpecKitService, "_finalize_phase", blocking_finalize)
    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    workflow = await service.start_workflow(workspace.id, user.id, "Add Auth", "We need login")
    constitution = next(p for p in workflow.phases if p.phase == "constitution")

    task = run_registry.get_task(constitution.id)
    assert task is not None and not task.done()
    release.set()
    await task


async def test_auto_chain_refused_by_a_drain_leaves_specify_pending(db_session, monkeypatch):
    """A deploy defers the chained specify; it is not a failure to report."""
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"drain-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"constitution": "running"})
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="generate constitution",
        status="completed",
        provider="mock",
        model="mock-agent",
    )
    session.add(run)
    await session.commit()

    async def refused_run_phase(self, *args, **kwargs):
        raise ServiceDrainingError

    monkeypatch.setattr(SpecKitService, "run_phase", refused_run_phase)
    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service._finalize_phase(workflow.id, "constitution", run.id, user.id)

    async with session_maker() as verify:
        phases = {
            p.phase: p.status
            for p in (
                await verify.execute(select(SpecPhase).where(SpecPhase.workflow_id == workflow.id))
            ).scalars()
        }
        refreshed = await verify.get(SpecWorkflow, workflow.id)
        errors = (
            await verify.execute(
                select(Notification).where(
                    Notification.user_id == user.id, Notification.notification_type == "spec_phase_error"
                )
            )
        ).scalars().all()
    assert phases["constitution"] == "completed"
    assert phases["specify"] == "pending"
    assert refreshed.status != "error"
    assert errors == []


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


async def test_run_phase_writes_untracked_feature_pointer_in_worktree(db_session, monkeypatch):
    # Spec Kit's check-prerequisites.sh fails with "Feature directory not found" unless
    # .specify/feature.json names the feature dir; it must stay out of git (info/exclude).
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"fp-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"specify": "approved"})
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

    runtime = FakeRuntime()
    service = SpecKitService(session, session_maker, Settings(), runtime=runtime)
    await service.run_phase(workspace.id, workflow.id, "plan", user.id)

    pointer_calls = [(cmd, cwd) for cmd, cwd in runtime.calls if ".specify/feature.json" in cmd]
    assert len(pointer_calls) == 1
    command, cwd = pointer_calls[0]
    assert cwd == "worktrees/001-add-auth"
    assert '{"feature_directory": "specs/001-add-auth"}' in command
    assert "info/exclude" in command


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


# --- Feature 1: plan generates the full design set --------------------------------------


async def test_plan_prompt_requests_full_design_set(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"plan-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user)
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    working_dir = service._working_dir(workspace, workflow, "plan")
    prompt = await service._build_prompt(workspace, workflow, "plan", working_dir, None)

    for expected in ("research.md", "data-model.md", "contracts/", "quickstart.md"):
        assert expected in prompt
    # The single-file restriction must NOT leak into the plan phase.
    assert "Output only the artifact content to the file" not in prompt


async def test_non_plan_phase_keeps_single_file_instruction(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"spec-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user)
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    working_dir = service._working_dir(workspace, workflow, "specify")
    prompt = await service._build_prompt(workspace, workflow, "specify", working_dir, None)

    assert "Output only the artifact content to the file" in prompt
    assert "data-model.md" not in prompt


async def test_list_phase_artifacts_returns_existing_design_files(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"list-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user)
    await session.commit()

    base = workflow.spec_dir
    runtime = ScriptedRuntime(
        existing={
            f"{base}/plan.md",
            f"{base}/research.md",
            f"{base}/data-model.md",
            f"{base}/quickstart.md",
        },
        listings={f"{base}/contracts": ["auth-api.yaml"]},
    )
    service = SpecKitService(session, session_maker, Settings(), runtime=runtime)
    items = await service.list_phase_artifacts(workspace.id, workflow.id, "plan", user.id)

    names = [name for name, _ in items]
    assert names == [
        "plan.md",
        "research.md",
        "data-model.md",
        "contracts/auth-api.yaml",
        "quickstart.md",
    ]
    # Paths are spec-dir relative.
    assert (f"{base}/contracts/auth-api.yaml") in [path for _, path in items]


async def test_read_artifact_rejects_path_traversal(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"trav-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user)
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    for bad in ("../etc/passwd", "/etc/passwd", "specs/other-feature/spec.md"):
        with pytest.raises(InvalidArtifactPathError):
            await service.read_artifact(workspace.id, workflow.id, "plan", user.id, rel_path=bad)


async def test_read_artifact_reads_design_file_within_spec_dir(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"read-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user)
    await session.commit()

    target = f"{workflow.spec_dir}/research.md"
    runtime = ScriptedRuntime(files={target: "# Research\nDecision: use JWT"})
    service = SpecKitService(session, session_maker, Settings(), runtime=runtime)
    path, content = await service.read_artifact(
        workspace.id, workflow.id, "plan", user.id, rel_path=target
    )
    assert path == target
    assert "use JWT" in content


# --- Feature 2: analyze open questions + answer loop ------------------------------------


async def _seed_running_analyze(session, workspace, workflow):
    """Mark analyze as running with a completed agent run, ready to finalize."""
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=workflow.user_id,
        prompt="analyze",
        status="completed",
        provider="mock",
        model="mock-agent",
    )
    session.add(run)
    await session.flush()
    analyze = (
        await session.execute(
            select(SpecPhase).where(
                SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "analyze"
            )
        )
    ).scalar_one()
    analyze.status = "running"
    analyze.agent_run_id = run.id
    analyze.artifact_path = f"{workflow.spec_dir}/analysis.md"
    session.add(analyze)
    await session.commit()
    return run


async def test_analyze_finalizer_halts_when_open_questions_exist(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"anq-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"plan": "approved"})
    run = await _seed_running_analyze(session, workspace, workflow)

    runtime = ScriptedRuntime(
        files={
            f"{workflow.spec_dir}/open-questions.json": (
                '[{"id": "A1", "category": "Ambiguity", "severity": "HIGH", '
                '"summary": "vague perf target", "recommendation": "quantify"}]'
            )
        }
    )
    service = SpecKitService(session, session_maker, Settings(), runtime=runtime)
    await service._finalize_phase(workflow.id, "analyze", run.id, user.id)

    async with session_maker() as verify:
        refreshed = (
            await verify.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "analyze"
                )
            )
        ).scalar_one()
        assert refreshed.status == "awaiting_review"


async def test_analyze_finalizer_completes_when_no_open_questions(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"anc-{uuid4()}@example.com")
    workflow = await make_workflow(session, workspace, user, phase_status={"plan": "approved"})
    run = await _seed_running_analyze(session, workspace, workflow)

    runtime = ScriptedRuntime(files={f"{workflow.spec_dir}/open-questions.json": "[]"})
    service = SpecKitService(session, session_maker, Settings(), runtime=runtime)
    await service._finalize_phase(workflow.id, "analyze", run.id, user.id)

    async with session_maker() as verify:
        refreshed = (
            await verify.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "analyze"
                )
            )
        ).scalar_one()
        assert refreshed.status == "completed"


async def test_submit_analyze_answers_reruns_analyze(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"ans-{uuid4()}@example.com")
    workflow = await make_workflow(
        session,
        workspace,
        user,
        phase_status={"plan": "approved", "analyze": "awaiting_review"},
    )
    await session.commit()

    captured: dict[str, str] = {}

    async def fake_create_run(self, workspace_id, user_id, prompt, working_dir=None):
        captured["prompt"] = prompt
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
    phase = await service.submit_analyze_answers(
        workspace.id,
        workflow.id,
        user.id,
        [{"id": "A1", "answer": "Target p95 < 200ms"}],
    )

    assert phase.status == "running"
    assert phase.attempt == 2
    assert "Target p95 < 200ms" in captured["prompt"]

    audits = (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "spec_analyze_answered")
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) >= 1


async def test_submit_analyze_answers_rejects_when_not_awaiting_review(db_session):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"rej-{uuid4()}@example.com")
    workflow = await make_workflow(
        session, workspace, user, phase_status={"plan": "approved", "analyze": "completed"}
    )
    await session.commit()

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    with pytest.raises(SpecPhaseNotReviewableError):
        await service.submit_analyze_answers(
            workspace.id, workflow.id, user.id, [{"id": "A1", "answer": "x"}]
        )


def test_spec_budget_exceeded_maps_to_402():
    """An enforcing budget over its limit must surface as 402 from the spec routes, like the
    agent run route -- not fall through to a 500."""
    from app.services.budgets import BudgetExceededError

    error = specs_module._map_spec_error(BudgetExceededError("workspace", "monthly"))
    assert error.status_code == 402


async def test_start_workflow_stores_verification_criteria(db_session, monkeypatch):
    """Plan 3: criteria given at creation are kept on the workflow for its implement phase."""
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"vc-{uuid4()}@example.com")
    await session.commit()

    monkeypatch.setattr("app.services.agents.AgentService.create_run", _fake_create_run())
    monkeypatch.setattr(SpecKitService, "_finalize_phase", _noop_finalize)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    criteria = {"test_command": "pytest -q", "observe_port": None, "max_iterations": 2}
    workflow = await service.start_workflow(
        workspace.id, user.id, "Add Auth", "We need login", verification=criteria
    )
    assert workflow.verification == criteria



# --- Plan 3: implement is verified before the workflow completes -------------------------


async def _completed_implement(session, user, workspace, *, verification=None):
    workflow = await make_workflow(
        session,
        workspace,
        user,
        phase_status={"plan": "approved", "tasks": "approved", "implement": "running"},
    )
    workflow.verification = verification
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        prompt="implement",
        status="completed",
        provider="mock",
        model="mock-agent",
    )
    session.add(run)
    await session.commit()
    return workflow, run


def _capture_verification_starts(monkeypatch) -> list:
    from app.services.verification import spec_link

    started: list = []

    async def fake_start(run_id, settings, **kwargs):
        started.append((run_id, kwargs))

    monkeypatch.setattr(spec_link, "start_verification_run", fake_start)
    return started


async def _phase_and_workflow(session_maker, workflow_id):
    async with session_maker() as s:
        phase = (
            await s.execute(
                select(SpecPhase).where(
                    SpecPhase.workflow_id == workflow_id, SpecPhase.phase == "implement"
                )
            )
        ).scalar_one()
        return phase.status, (await s.get(SpecWorkflow, workflow_id)).status


async def test_implement_with_criteria_starts_a_linked_verification(db_session, monkeypatch):
    from app.models import VerificationRun

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"v1-{uuid4()}@example.com")
    criteria = {"test_command": "pytest -q", "observe_port": None, "max_iterations": 2}
    workflow, run = await _completed_implement(session, user, workspace, verification=criteria)
    started = _capture_verification_starts(monkeypatch)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service._finalize_phase(workflow.id, "implement", run.id, user.id)

    assert await _phase_and_workflow(session_maker, workflow.id) == (
        "awaiting_review",
        "awaiting_review",
    )
    async with session_maker() as s:
        [vrun] = (
            await s.execute(
                select(VerificationRun).where(VerificationRun.spec_workflow_id == workflow.id)
            )
        ).scalars().all()
    assert vrun.status == "planning"
    assert vrun.plan_ref == "specs/001-add-auth/plan.md"
    assert vrun.created_by == user.id
    assert vrun.criteria["test_command"] == "pytest -q"
    assert vrun.criteria["cwd"] == "worktrees/001-add-auth"
    assert [rid for rid, _ in started] == [vrun.id]


async def test_implement_without_criteria_completes_as_before(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"v2-{uuid4()}@example.com")
    workflow, run = await _completed_implement(session, user, workspace)
    started = _capture_verification_starts(monkeypatch)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service._finalize_phase(workflow.id, "implement", run.id, user.id)

    assert await _phase_and_workflow(session_maker, workflow.id) == ("completed", "completed")
    assert started == []


async def test_a_refused_verification_still_parks_implement_for_review(db_session, monkeypatch):
    """Another verification run is active: nothing is lost -- implement waits for review and
    the human is told why, then can re-run verification."""
    from app.models import VerificationRun

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"v3-{uuid4()}@example.com")
    criteria = {"test_command": "pytest -q", "max_iterations": 1}
    workflow, run = await _completed_implement(session, user, workspace, verification=criteria)
    session.add(VerificationRun(workspace_id=workspace.id, plan_ref="other", status="testing"))
    await session.commit()
    started = _capture_verification_starts(monkeypatch)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service._finalize_phase(workflow.id, "implement", run.id, user.id)

    assert await _phase_and_workflow(session_maker, workflow.id) == (
        "awaiting_review",
        "awaiting_review",
    )
    assert started == []
    async with session_maker() as s:
        notes = (
            await s.execute(
                select(Notification).where(
                    Notification.user_id == user.id,
                    Notification.notification_type == "spec_verification_not_started",
                )
            )
        ).scalars().all()
    assert len(notes) == 1


# --- Plan 3 Task 4: one gate for the evidence and the phase ------------------------------


async def _implement_awaiting_with_run(session, user, workspace, *, run_status="review"):
    from app.models import VerificationRun

    workflow = await make_workflow(
        session,
        workspace,
        user,
        phase_status={"plan": "approved", "tasks": "approved", "implement": "awaiting_review"},
    )
    workflow.verification = {"test_command": "pytest -q", "max_iterations": 2}
    workflow.status = "awaiting_review"
    run = VerificationRun(
        workspace_id=workspace.id,
        plan_ref="specs/001-add-auth/plan.md",
        status=run_status,
        spec_workflow_id=workflow.id,
    )
    session.add(run)
    await session.commit()
    return workflow, run


async def test_approving_the_latest_linked_run_completes_the_workflow(db_session):
    from app.services.verification.spec_link import approve_verification_run

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g1-{uuid4()}@example.com")
    workflow, run = await _implement_awaiting_with_run(session, user, workspace)

    assert await approve_verification_run(session_maker, run.id, user.id) is True

    assert await _phase_and_workflow(session_maker, workflow.id) == ("approved", "completed")
    async with session_maker() as s:
        audits = (
            await s.execute(select(AuditLog).where(AuditLog.action == "spec_phase_approved"))
        ).scalars().all()
    assert any(a.target_type == "spec_phase" for a in audits)


async def test_approving_an_old_run_while_implement_reruns_changes_nothing(db_session):
    """Request changes re-runs implement: the old run's approval must not complete a workflow
    whose new code it never verified."""
    from app.services.verification.spec_link import approve_verification_run

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g2-{uuid4()}@example.com")
    workflow, old = await _implement_awaiting_with_run(session, user, workspace)
    phase = (
        await session.execute(
            select(SpecPhase).where(
                SpecPhase.workflow_id == workflow.id, SpecPhase.phase == "implement"
            )
        )
    ).scalar_one()
    phase.status = "running"
    workflow.status = "running"
    await session.commit()

    assert await approve_verification_run(session_maker, old.id, user.id) is True

    assert await _phase_and_workflow(session_maker, workflow.id) == ("running", "running")


async def test_a_new_verification_supersedes_an_old_run_awaiting_review(db_session, monkeypatch):
    from app.models import VerificationRun
    from app.services.verification.spec_link import approve_verification_run

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g2b-{uuid4()}@example.com")
    workflow, old = await _implement_awaiting_with_run(session, user, workspace)
    _capture_verification_starts(monkeypatch)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service.verify_implement(workspace.id, workflow.id, user.id)

    async with session_maker() as s:
        assert (await s.get(VerificationRun, old.id)).status == "rejected"
    assert await approve_verification_run(session_maker, old.id, user.id) is False
    assert await _phase_and_workflow(session_maker, workflow.id) == (
        "awaiting_review",
        "awaiting_review",
    )


async def test_approving_a_run_not_in_review_changes_nothing(db_session):
    from app.services.verification.spec_link import approve_verification_run

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g3-{uuid4()}@example.com")
    workflow, run = await _implement_awaiting_with_run(session, user, workspace, run_status="testing")

    assert await approve_verification_run(session_maker, run.id, user.id) is False
    assert await _phase_and_workflow(session_maker, workflow.id) == (
        "awaiting_review",
        "awaiting_review",
    )


async def test_implement_cannot_be_approved_around_its_evidence(db_session):
    from app.services.speckit import SpecPhaseRequiresVerificationError

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g4-{uuid4()}@example.com")
    workflow, _run = await _implement_awaiting_with_run(session, user, workspace)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    with pytest.raises(SpecPhaseRequiresVerificationError):
        await service.approve_phase(workspace.id, workflow.id, "implement", user.id)


async def test_verify_implement_starts_a_new_linked_run(db_session, monkeypatch):
    from app.models import VerificationRun

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g5-{uuid4()}@example.com")
    workflow, old = await _implement_awaiting_with_run(session, user, workspace, run_status="rejected")
    started = _capture_verification_starts(monkeypatch)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    run = await service.verify_implement(workspace.id, workflow.id, user.id)

    assert run.spec_workflow_id == workflow.id
    assert run.id != old.id
    assert [rid for rid, _ in started] == [run.id]
    async with session_maker() as s:
        linked = (
            await s.execute(
                select(VerificationRun).where(VerificationRun.spec_workflow_id == workflow.id)
            )
        ).scalars().all()
    assert len(linked) == 2


async def test_verify_implement_requires_implement_awaiting_review(db_session, monkeypatch):
    from app.services.speckit import SpecPhaseNotReviewableError

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g6-{uuid4()}@example.com")
    workflow = await make_workflow(
        session, workspace, user, phase_status={"implement": "running"}
    )
    workflow.verification = {"test_command": "pytest -q"}
    await session.commit()
    _capture_verification_starts(monkeypatch)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    with pytest.raises(SpecPhaseNotReviewableError):
        await service.verify_implement(workspace.id, workflow.id, user.id)


async def test_verify_route_starts_a_run_and_refuses_a_second_one(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g7-{uuid4()}@example.com")
    workflow, _old = await _implement_awaiting_with_run(
        session, user, workspace, run_status="rejected"
    )
    _capture_verification_starts(monkeypatch)

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(user)
    app.dependency_overrides[specs_module.get_speckit_service] = speckit_override(
        session, session_maker, FakeRuntime()
    )
    url = f"/workspaces/{workspace.id}/spec/workflows/{workflow.id}/verify"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(url)
            second = await client.post(url)  # the first run is still active (planning)
            approve = await client.post(
                f"/workspaces/{workspace.id}/spec/workflows/{workflow.id}/phases/implement/approve"
            )
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == 202, first.text
    assert first.json()["spec_workflow_id"] == str(workflow.id)
    assert first.json()["status"] == "planning"
    assert second.status_code == 409
    assert approve.status_code == 409  # only through the evidence


async def test_request_changes_on_implement_closes_its_old_evidence(db_session, monkeypatch):
    """Review HIGH: once implement re-runs, its old evidence verified code that is about to
    change -- it must not stay approvable, even if the next verification can't be created."""
    from app.models import VerificationRun
    from app.services.verification.spec_link import approve_verification_run

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session, owner_email=f"g8-{uuid4()}@example.com")
    workflow, old = await _implement_awaiting_with_run(session, user, workspace)
    monkeypatch.setattr("app.services.agents.AgentService.create_run", _fake_create_run())
    monkeypatch.setattr(SpecKitService, "_finalize_phase", _noop_finalize)

    service = SpecKitService(session, session_maker, Settings(), runtime=FakeRuntime())
    await service.request_changes(workspace.id, workflow.id, "implement", user.id, "fix it")

    async with session_maker() as s:
        assert (await s.get(VerificationRun, old.id)).status == "rejected"
    assert await approve_verification_run(session_maker, old.id, user.id) is False
