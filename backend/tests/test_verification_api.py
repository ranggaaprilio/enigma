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
from app.models import User, VerificationRun, Workspace
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
        sandbox_id=f"sandbox-verify-{uuid4().hex[:8]}",  # unique: tests may need two
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


def _capture_starts(monkeypatch) -> list:
    """Record what the endpoint hands to the executor instead of driving it here: the loop runs
    off-request (its own tests cover it), and this fixture's single connection cannot be shared
    with a concurrent background task."""
    from app.api import verification as api_mod

    started: list = []

    async def fake_start(run_id, settings, **_kwargs):
        started.append(run_id)

    monkeypatch.setattr(api_mod, "start_verification_run", fake_start)
    return started


async def test_create_run_returns_202_and_starts_the_loop(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    started = _capture_starts(monkeypatch)

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/workspaces/{workspace.id}/verification/runs",
            json={
                "plan_ref": "spec:1",
                "test_command": "pytest -q",
                "observe_port": 5173,
                "max_iterations": 3,
            },
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "planning"
    assert body["evidence"] == []

    run = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == body["id"]))
    ).scalar_one()
    # Everything a worker needs to resume the run is on the row.
    assert run.criteria == {
        "test_command": "pytest -q",
        "observe_port": 5173,
        "cwd": None,
        "timeout": None,
        "max_iterations": 3,
    }
    assert run.created_by == user.id
    assert [str(r) for r in started] == [body["id"]]


async def test_create_run_is_refused_while_draining(db_session, monkeypatch):
    from app.core.lifecycle import lifecycle

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    started = _capture_starts(monkeypatch)
    monkeypatch.setattr(lifecycle, "draining", True)

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/workspaces/{workspace.id}/verification/runs",
            json={"plan_ref": "spec:1", "test_command": "pytest"},
        )
    assert resp.status_code == 503
    assert started == []


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
    # Refused before anything is written: no orphan run parked at planning.
    runs = (
        await session.execute(
            select(VerificationRun).where(VerificationRun.workspace_id == workspace.id)
        )
    ).scalars().all()
    assert [r.id for r in runs] == [active.id]


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


async def test_runs_can_be_filtered_by_spec_workflow(db_session, monkeypatch):
    from app.models import SpecWorkflow

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=user.id,
        feature_name="F",
        feature_slug=f"f-{uuid4().hex[:6]}",
        description="d",
        branch="f",
        spec_dir="specs/f",
    )
    session.add(workflow)
    await session.flush()
    linked = VerificationRun(
        workspace_id=workspace.id, plan_ref="specs/f/plan.md", status="review",
        spec_workflow_id=workflow.id,
    )
    other = VerificationRun(workspace_id=workspace.id, plan_ref="other", status="review")
    session.add_all([linked, other])
    await session.flush()

    override = await override_get_current_user(user)
    app.dependency_overrides[auth_module.get_current_user] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/workspaces/{workspace.id}/verification/runs",
            params={"spec_workflow_id": str(workflow.id)},
        )
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [str(linked.id)]


async def test_a_run_of_another_workspace_cannot_be_gated(db_session, monkeypatch):
    """Security: the gate must check the run belongs to the workspace in the URL before it
    writes anything (an approval also completes a linked Spec Kit workflow)."""
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    other_user, other_workspace = await make_user_workspace(session)
    foreign = VerificationRun(
        workspace_id=other_workspace.id, plan_ref="theirs", status="review"
    )
    session.add(foreign)
    await session.flush()
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        approve = await client.post(
            f"/workspaces/{workspace.id}/verification/runs/{foreign.id}/approve"
        )
        reject = await client.post(
            f"/workspaces/{workspace.id}/verification/runs/{foreign.id}/reject"
        )

    assert approve.status_code == 404
    assert reject.status_code == 404
    await session.refresh(foreign)
    assert foreign.status == "review"  # untouched


async def test_only_the_owner_can_gate_a_spec_linked_run(db_session, monkeypatch):
    """Security: approving a Spec Kit-linked run completes the workflow, which approve_phase
    reserves to the workspace owner -- a team member who may drive must not bypass that."""
    from app.models import Project, SpecWorkflow, Team, TeamMember

    session, session_maker = db_session
    owner, workspace = await make_user_workspace(session)
    member, _member_ws = await make_user_workspace(session)
    project = Project(name="P", slug=f"p-{uuid4().hex[:6]}")
    session.add(project)
    await session.flush()
    team = Team(project_id=project.id, name="T", slug=f"t-{uuid4().hex[:6]}")
    session.add(team)
    await session.flush()
    session.add(TeamMember(team_id=team.id, user_id=member.id, role="member"))
    workspace.team_id = team.id
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=owner.id,
        feature_name="F",
        feature_slug=f"f-{uuid4().hex[:6]}",
        description="d",
        branch="f",
        spec_dir="specs/f",
    )
    session.add(workflow)
    await session.flush()
    linked = VerificationRun(
        workspace_id=workspace.id,
        plan_ref="specs/f/plan.md",
        status="review",
        spec_workflow_id=workflow.id,
    )
    session.add(linked)
    await session.flush()
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
        member
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        approve = await client.post(
            f"/workspaces/{workspace.id}/verification/runs/{linked.id}/approve"
        )

    assert approve.status_code == 403
    await session.refresh(linked)
    assert linked.status == "review"


# --- Plan 5: stop an active run -----------------------------------------------------------


async def _stop(user, workspace_id, run_id):
    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/workspaces/{workspace_id}/verification/runs/{run_id}/stop")


@pytest.mark.parametrize(("status", "cancelled"), [("testing", True), ("fixing", False)])
async def test_stop_ends_an_active_run_as_rejected(db_session, monkeypatch, status, cancelled):
    import asyncio

    from app.models import AuditLog
    from app.services.runtime import run_registry

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    run = VerificationRun(workspace_id=workspace.id, plan_ref="p", status=status)
    session.add(run)
    await session.flush()
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    task = asyncio.create_task(asyncio.sleep(60))  # the inline loop driving this run
    run_registry.register(run.id, task)
    try:
        response = await _stop(user, workspace.id, run.id)
        await asyncio.sleep(0)
        # A test/observation is cancelled with its task; a fix exits on its next poll (the
        # end hook stops its agent run instead of interrupting the agent's bookkeeping).
        assert task.cancelled() is cancelled
    finally:
        task.cancel()

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.workspace_id == workspace.id, AuditLog.action == "verification_run_stopped"
        )
    )
    assert audit is not None


async def test_stop_refuses_a_run_that_is_not_active(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    run = VerificationRun(workspace_id=workspace.id, plan_ref="p", status="review")
    session.add(run)
    await session.flush()
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    response = await _stop(user, workspace.id, run.id)

    assert response.status_code == 409
    await session.refresh(run)
    assert run.status == "review"


async def test_stop_cannot_reach_another_workspaces_run(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _other, other_workspace = await make_user_workspace(session)
    foreign = VerificationRun(workspace_id=other_workspace.id, plan_ref="p", status="testing")
    session.add(foreign)
    await session.flush()
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    response = await _stop(user, workspace.id, foreign.id)

    assert response.status_code == 404
    await session.refresh(foreign)
    assert foreign.status == "testing"


async def test_only_the_owner_can_stop_a_spec_linked_run(db_session, monkeypatch):
    from app.models import Project, SpecWorkflow, Team, TeamMember

    session, session_maker = db_session
    owner, workspace = await make_user_workspace(session)
    member, _member_ws = await make_user_workspace(session)
    project = Project(name="P", slug=f"p-{uuid4().hex[:6]}")
    session.add(project)
    await session.flush()
    team = Team(project_id=project.id, name="T", slug=f"t-{uuid4().hex[:6]}")
    session.add(team)
    await session.flush()
    session.add(TeamMember(team_id=team.id, user_id=member.id, role="member"))
    workspace.team_id = team.id
    workflow = SpecWorkflow(
        workspace_id=workspace.id,
        user_id=owner.id,
        feature_name="F",
        feature_slug=f"f-{uuid4().hex[:6]}",
        description="d",
        branch="f",
        spec_dir="specs/f",
    )
    session.add(workflow)
    await session.flush()
    linked = VerificationRun(
        workspace_id=workspace.id,
        plan_ref="specs/f/plan.md",
        status="testing",
        spec_workflow_id=workflow.id,
    )
    session.add(linked)
    await session.flush()
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())

    response = await _stop(member, workspace.id, linked.id)

    assert response.status_code == 403
    await session.refresh(linked)
    assert linked.status == "testing"


# --- Plan 6: serve an evidence screenshot ---------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pixels"


class _MemoryStore:
    def __init__(self, objects=None, fail=False):
        self.objects = dict(objects or {})
        self.fail = fail

    async def get(self, key):
        from app.services.artifact_store import ArtifactStoreError

        if self.fail:
            raise ArtifactStoreError("artifact store get failed")
        return self.objects[key]


async def _seed_screenshot(session, workspace):
    from app.models import VerificationArtifact, VerificationEvidence

    run = VerificationRun(workspace_id=workspace.id, plan_ref="p", status="review")
    session.add(run)
    await session.flush()
    evidence = VerificationEvidence(run_id=run.id, kind="observation", status="passed", detail={})
    session.add(evidence)
    await session.flush()
    key = f"verification/{workspace.id}/{run.id}/{evidence.id}.png"
    session.add(
        VerificationArtifact(
            evidence_id=evidence.id, storage_key=key, content_type="image/png", byte_size=14
        )
    )
    await session.flush()
    return run, evidence, key


async def _get_shot(user, workspace_id, run_id, evidence_id):
    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(user)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(
            f"/workspaces/{workspace_id}/verification/runs/{run_id}/evidence/{evidence_id}/screenshot"
        )


async def test_screenshot_is_served_as_a_locked_down_png(db_session, monkeypatch):
    from app.api import verification as api_mod

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    run, evidence, key = await _seed_screenshot(session, workspace)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    monkeypatch.setattr(api_mod, "get_artifact_store", lambda _s: _MemoryStore({key: PNG_BYTES}))

    response = await _get_shot(user, workspace.id, run.id, evidence.id)

    assert response.status_code == 200, response.text
    assert response.content == PNG_BYTES
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["cache-control"] == "private, max-age=300"


async def test_screenshot_lookup_is_scoped_to_the_workspace_and_run(db_session, monkeypatch):
    from uuid import uuid4 as new_id

    from app.api import verification as api_mod

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    other_user, other_workspace = await make_user_workspace(session)
    run, evidence, key = await _seed_screenshot(session, workspace)
    other_run, other_evidence, other_key = await _seed_screenshot(session, other_workspace)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    store = _MemoryStore({key: PNG_BYTES, other_key: PNG_BYTES})
    monkeypatch.setattr(api_mod, "get_artifact_store", lambda _s: store)

    # Another workspace's evidence through my workspace, my evidence under another run, and
    # evidence without an artifact: all simply not found.
    assert (await _get_shot(user, workspace.id, other_run.id, other_evidence.id)).status_code == 404
    assert (await _get_shot(user, workspace.id, new_id(), evidence.id)).status_code == 404
    assert (await _get_shot(user, workspace.id, run.id, new_id())).status_code == 404
    # And no access to the other workspace at all.
    assert (
        await _get_shot(user, other_workspace.id, other_run.id, other_evidence.id)
    ).status_code in (403, 404)


@pytest.mark.parametrize("store", [None, _MemoryStore(fail=True)])
async def test_screenshot_is_503_when_the_store_is_unavailable(db_session, monkeypatch, store):
    from app.api import verification as api_mod

    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    run, evidence, _key = await _seed_screenshot(session, workspace)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    monkeypatch.setattr(api_mod, "get_artifact_store", lambda _s: store)

    response = await _get_shot(user, workspace.id, run.id, evidence.id)

    assert response.status_code == 503
