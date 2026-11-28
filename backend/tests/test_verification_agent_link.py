# ruff: noqa: F811  (db_session is an imported pytest fixture)
"""Plan 9 Task 1: verify an ordinary agent run from its own context.

``POST /workspaces/{id}/verification/agent-runs/{run_id}`` links a verification run to the agent
run whose code it checks (``source_agent_run_id``), derives the test command from the workspace's
``role="test"`` command run in that agent run's checkout (its worktree, or the base clone), and
refuses what cannot be verified: an unfinished run, a fix run, or a run with no criteria.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import auth as auth_module
from app.main import app
from app.models import AgentRun, CustomCommand, VerificationRun, WorkspaceWorktree
from tests.test_verification_api import (  # noqa: F401  (db_session is a fixture)
    FakeRuntime,
    _capture_starts,
    _patch_deps,
    db_session,
    make_user_workspace,
    override_get_current_user,
)


async def _agent_run(session, workspace, user, *, status="completed", worktree=None):
    run = AgentRun(
        workspace_id=workspace.id,
        user_id=user.id,
        worktree_id=worktree.id if worktree is not None else None,
        prompt="Add a sum() helper and its test",
        status=status,
        provider="mock",
        model="mock",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def _test_command(session, workspace, *, command="npm test", cwd=None):
    session.add(
        CustomCommand(
            workspace_id=workspace.id, name="Test", command=command, role="test", cwd=cwd
        )
    )
    await session.flush()


async def _worktree(session, workspace):
    worktree = WorkspaceWorktree(
        workspace_id=workspace.id,
        branch="feature/sum",
        path="worktrees/feature-sum",
        base_ref="main",
    )
    session.add(worktree)
    await session.flush()
    return worktree


async def _setup(db_session, monkeypatch):
    session, session_maker = db_session
    user, workspace = await make_user_workspace(session)
    _patch_deps(monkeypatch, session, session_maker, FakeRuntime())
    started = _capture_starts(monkeypatch)
    app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
        user
    )
    return session, user, workspace, started


async def _verify(workspace_id, run_id, body=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/workspaces/{workspace_id}/verification/agent-runs/{run_id}", json=body or {}
        )


async def test_a_completed_run_is_verified_in_its_worktree(db_session, monkeypatch):
    session, user, workspace, started = await _setup(db_session, monkeypatch)
    await _test_command(session, workspace, cwd="web")
    worktree = await _worktree(session, workspace)
    agent_run = await _agent_run(session, workspace, user, worktree=worktree)

    resp = await _verify(workspace.id, agent_run.id)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["source_agent_run_id"] == str(agent_run.id)
    run = await session.get(VerificationRun, body["id"])
    assert run.source_agent_run_id == agent_run.id
    assert run.created_by == user.id
    assert run.plan_ref == "Add a sum() helper and its test"
    assert run.criteria["test_command"] == "npm test"
    assert run.criteria["cwd"] == "worktrees/feature-sum/web"
    assert started == [run.id]


async def test_a_run_on_the_base_checkout_is_verified_there(db_session, monkeypatch):
    session, user, workspace, _ = await _setup(db_session, monkeypatch)
    await _test_command(session, workspace)
    agent_run = await _agent_run(session, workspace, user)

    resp = await _verify(workspace.id, agent_run.id)

    assert resp.status_code == 202, resp.text
    run = await session.get(VerificationRun, resp.json()["id"])
    assert run.criteria["cwd"] == workspace.workspace_path


async def test_given_criteria_win_over_the_derived_ones(db_session, monkeypatch):
    session, user, workspace, _ = await _setup(db_session, monkeypatch)
    agent_run = await _agent_run(session, workspace, user)  # no role="test" command at all

    resp = await _verify(
        workspace.id,
        agent_run.id,
        {"test_command": "pytest -q", "observe_port": 3000, "max_iterations": 2},
    )

    assert resp.status_code == 202, resp.text
    run = await session.get(VerificationRun, resp.json()["id"])
    assert run.criteria["test_command"] == "pytest -q"
    assert run.criteria["observe_port"] == 3000
    assert run.criteria["max_iterations"] == 2
    assert run.criteria["cwd"] == workspace.workspace_path


@pytest.mark.parametrize("status", ["thinking", "executing", "error", "stopped"])
async def test_an_unfinished_run_is_refused(db_session, monkeypatch, status):
    session, user, workspace, started = await _setup(db_session, monkeypatch)
    await _test_command(session, workspace)
    # One per test: a workspace holds at most one active run (AGENTS.md gotcha 19).
    agent_run = await _agent_run(session, workspace, user, status=status)

    resp = await _verify(workspace.id, agent_run.id)

    assert resp.status_code == 409, resp.text
    assert started == []


async def test_a_fix_run_is_never_verified_on_its_own(db_session, monkeypatch):
    session, user, workspace, started = await _setup(db_session, monkeypatch)
    await _test_command(session, workspace)
    fix = await _agent_run(session, workspace, user)
    session.add(
        VerificationRun(
            workspace_id=workspace.id, plan_ref="p", status="approved", agent_run_id=fix.id
        )
    )
    await session.flush()

    resp = await _verify(workspace.id, fix.id)

    assert resp.status_code == 409
    assert "fix" in resp.json()["detail"].lower()
    assert started == []


async def test_a_finished_fix_run_is_still_recognised(db_session, monkeypatch):
    """After a fix the orchestrator clears ``agent_run_id`` (it tracks the *current* fix only);
    the fix evidence keeps the id, and that is enough to refuse it (found live, Plan 9 E2E)."""
    from app.models import VerificationEvidence

    session, user, workspace, started = await _setup(db_session, monkeypatch)
    await _test_command(session, workspace)
    fix = await _agent_run(session, workspace, user)
    finished = VerificationRun(workspace_id=workspace.id, plan_ref="p", status="approved")
    session.add(finished)
    await session.flush()
    session.add(
        VerificationEvidence(
            run_id=finished.id,
            kind="fix",
            status="passed",
            detail={"agent_run_id": str(fix.id), "agent_status": "completed"},
        )
    )
    await session.flush()

    resp = await _verify(workspace.id, fix.id)

    assert resp.status_code == 409
    assert "fix" in resp.json()["detail"].lower()
    assert started == []


async def test_a_run_without_criteria_says_how_to_add_them(db_session, monkeypatch):
    session, user, workspace, started = await _setup(db_session, monkeypatch)
    agent_run = await _agent_run(session, workspace, user)

    resp = await _verify(workspace.id, agent_run.id)

    assert resp.status_code == 422
    assert "role" in resp.json()["detail"] and "test" in resp.json()["detail"]
    assert started == []


async def test_another_workspaces_run_is_not_found(db_session, monkeypatch):
    session, user, workspace, _ = await _setup(db_session, monkeypatch)
    _, other_workspace = await make_user_workspace(session)
    foreign = await _agent_run(session, other_workspace, user)

    assert (await _verify(workspace.id, foreign.id)).status_code == 404
    assert (await _verify(workspace.id, uuid4())).status_code == 404


async def test_a_newer_verification_supersedes_the_one_in_review(db_session, monkeypatch):
    session, user, workspace, _ = await _setup(db_session, monkeypatch)
    await _test_command(session, workspace)
    agent_run = await _agent_run(session, workspace, user)
    stale = VerificationRun(
        workspace_id=workspace.id,
        plan_ref="p",
        status="review",
        source_agent_run_id=agent_run.id,
    )
    unrelated = VerificationRun(workspace_id=workspace.id, plan_ref="q", status="review")
    session.add_all([stale, unrelated])
    await session.flush()

    resp = await _verify(workspace.id, agent_run.id)

    assert resp.status_code == 202, resp.text
    await session.refresh(stale)
    await session.refresh(unrelated)
    assert stale.status == "rejected"  # its evidence is no longer the newest
    assert unrelated.status == "review"


async def test_runs_can_be_filtered_by_their_source_agent_run(db_session, monkeypatch):
    session, user, workspace, _ = await _setup(db_session, monkeypatch)
    agent_run = await _agent_run(session, workspace, user)
    linked = VerificationRun(
        workspace_id=workspace.id, plan_ref="p", status="review", source_agent_run_id=agent_run.id
    )
    other = VerificationRun(workspace_id=workspace.id, plan_ref="q", status="review")
    session.add_all([linked, other])
    await session.flush()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/workspaces/{workspace.id}/verification/runs",
            params={"source_agent_run_id": str(agent_run.id)},
        )

    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [str(linked.id)]
    assert resp.json()[0]["source_agent_run_id"] == str(agent_run.id)
    ids = (await session.scalars(select(VerificationRun.id))).all()
    assert other.id in ids
