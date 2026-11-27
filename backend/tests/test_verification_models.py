"""Evidence store models: verification runs and their evidence (Verifiable Agent Loop).

A ``VerificationRun`` tracks one verification loop over a workspace (identified by its
``plan_ref``), and each ``VerificationEvidence`` row is a test result, observation or
screenshot attached to that run. This is a round-trip persistence check against the real
database.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, VerificationEvidence, VerificationRun, Workspace


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _workspace(maker) -> Workspace:
    async with maker() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4().hex}@example.com",
            password_hash="x",
            role="owner",
            is_active=True,
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name="Verify",
            slug=uuid4().hex[:12],
            default_branch="main",
            runtime="docker",
            status="ready",
            workspace_path="workspace",
            sandbox_id=uuid4().hex,
        )
        session.add(user)
        await session.flush()
        session.add(workspace)
        await session.commit()
        return workspace


async def _cleanup(maker, workspace: Workspace) -> None:
    async with maker() as session:
        row = await session.get(Workspace, workspace.id)
        user = await session.get(User, workspace.owner_id)
        await session.delete(row)  # cascades to verification_runs -> verification_evidence
        await session.flush()
        await session.delete(user)
        await session.commit()


async def test_verification_run_and_evidence_round_trip(maker) -> None:
    workspace = await _workspace(maker)
    try:
        async with maker() as session:
            run = VerificationRun(workspace_id=workspace.id, plan_ref="spec:1", status="testing")
            # Append through the relationship (rather than a lazy ``run.evidence`` read after
            # flush, which async SQLAlchemy forbids) so the collection stays loaded.
            run.evidence.append(
                VerificationEvidence(kind="test", status="passed", detail={"exit_code": 0})
            )
            session.add(run)
            await session.commit()

            assert run.id is not None
            assert run.plan_ref == "spec:1"
            assert run.status == "testing"
            assert run.evidence[0].kind == "test"
            assert run.evidence[0].status == "passed"
            assert run.evidence[0].detail == {"exit_code": 0}
    finally:
        await _cleanup(maker, workspace)


async def test_verification_run_persists_loop_state(maker) -> None:
    """Plan 2 Task 1: criteria, author, iteration and the fix agent run survive a round trip,
    so a run can be resumed by another process."""
    workspace = await _workspace(maker)
    try:
        criteria = {"test_command": "pytest -q", "observe_port": 5173, "max_iterations": 3}
        async with maker() as session:
            run = VerificationRun(
                workspace_id=workspace.id,
                plan_ref="spec:1",
                status="fixing",
                criteria=criteria,
                created_by=workspace.owner_id,
                iteration=2,
            )
            run.evidence.append(
                VerificationEvidence(
                    kind="fix", status="passed", detail={"agent_status": "completed"}
                )
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        async with maker() as session:
            loaded = await session.get(VerificationRun, run_id)
            assert loaded.status == "fixing"
            assert loaded.criteria == criteria
            assert loaded.created_by == workspace.owner_id
            assert loaded.iteration == 2
            assert loaded.agent_run_id is None
    finally:
        await _cleanup(maker, workspace)


async def test_verification_run_defaults_for_loop_state(maker) -> None:
    workspace = await _workspace(maker)
    try:
        async with maker() as session:
            run = VerificationRun(workspace_id=workspace.id, plan_ref="spec:1", status="planning")
            session.add(run)
            await session.commit()
            run_id = run.id
        async with maker() as session:
            loaded = await session.get(VerificationRun, run_id)
            assert loaded.criteria == {}
            assert loaded.iteration == 0
            assert loaded.created_by is None
    finally:
        await _cleanup(maker, workspace)


async def test_verification_run_links_to_a_spec_workflow(maker) -> None:
    """Plan 3 Task 1: a run started for a Spec Kit ``implement`` phase points at its workflow,
    and the workflow carries its own verification criteria."""
    from app.models import SpecWorkflow

    workspace = await _workspace(maker)
    try:
        async with maker() as session:
            workflow = SpecWorkflow(
                workspace_id=workspace.id,
                user_id=workspace.owner_id,
                feature_name="Login",
                feature_slug=f"login-{uuid4().hex[:6]}",
                description="d",
                branch="login",
                spec_dir="specs/login",
                worktree_path="worktrees/login",
                verification={"test_command": "pytest -q", "max_iterations": 2},
            )
            session.add(workflow)
            await session.flush()
            run = VerificationRun(
                workspace_id=workspace.id,
                plan_ref="specs/login/plan.md",
                status="planning",
                spec_workflow_id=workflow.id,
            )
            session.add(run)
            await session.commit()
            run_id, workflow_id = run.id, workflow.id

        async with maker() as session:
            loaded = await session.get(VerificationRun, run_id)
            assert loaded.spec_workflow_id == workflow_id
            wf = await session.get(SpecWorkflow, workflow_id)
            assert wf.verification == {"test_command": "pytest -q", "max_iterations": 2}
    finally:
        await _cleanup(maker, workspace)


async def test_spec_workflow_verification_defaults_to_none(maker) -> None:
    from app.models import SpecWorkflow

    workspace = await _workspace(maker)
    try:
        async with maker() as session:
            workflow = SpecWorkflow(
                workspace_id=workspace.id,
                user_id=workspace.owner_id,
                feature_name="Plain",
                feature_slug=f"plain-{uuid4().hex[:6]}",
                description="d",
                branch="plain",
                spec_dir="specs/plain",
            )
            session.add(workflow)
            await session.commit()
            assert workflow.verification is None
    finally:
        await _cleanup(maker, workspace)


async def test_spec_workflow_links_to_its_pull_request(maker) -> None:
    """Plan 4 Task 1: an approved workflow points at the PR it was handed over as; deleting
    the cached PR row only unlinks it."""
    from app.models import PullRequest, SpecWorkflow

    workspace = await _workspace(maker)
    try:
        async with maker() as session:
            pr = PullRequest(
                workspace_id=workspace.id,
                number=7,
                url="https://github.com/o/r/pull/7",
                state="open",
                head="001-login",
                base="main",
                title="Login",
            )
            session.add(pr)
            await session.flush()
            workflow = SpecWorkflow(
                workspace_id=workspace.id,
                user_id=workspace.owner_id,
                feature_name="Login",
                feature_slug=f"login-{uuid4().hex[:6]}",
                description="d",
                branch="001-login",
                spec_dir="specs/001-login",
                pull_request_id=pr.id,
            )
            session.add(workflow)
            await session.commit()
            workflow_id, pr_id = workflow.id, pr.id

        async with maker() as session:
            wf = await session.get(SpecWorkflow, workflow_id)
            assert wf.pull_request_id == pr_id
            assert wf.pull_request.number == 7
            await session.delete(await session.get(PullRequest, pr_id))
            await session.commit()

        async with maker() as session:
            wf = await session.get(SpecWorkflow, workflow_id)
            assert wf.pull_request_id is None
            assert wf.pull_request is None
    finally:
        await _cleanup(maker, workspace)
