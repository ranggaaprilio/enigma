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
