"""Verification orchestrator: drive the test -> observe -> iterate -> gate loop.

Exercises ``VerificationOrchestrator`` against a fake sandbox runtime (no live sandbox), an
``httpx`` mock transport for the observation fetch (no live preview), and a real Postgres for
the run/evidence persistence — mirroring ``test_verification_observation.py``.

The loop contract: a run seeded in an active status (``planning``) is driven through
``testing`` -> ``observing`` -> ``review``, recording evidence at each step; the human gate
(``approve``/``reject``) then finalizes it from ``review`` to ``approved``/``rejected`` with a
conditional UPDATE (a lost race returns ``False``).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, VerificationEvidence, VerificationRun, Workspace
from app.services.sandbox.base import RuntimeCommandResult
from app.services.verification.orchestrator import (
    PlanCriteria,
    VerificationOrchestrator,
    VerificationRunAlreadyActiveError,
)
from tests.sandbox.fakes import FakeSandboxRuntime


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker, status: str = "planning"):
    """A persisted user + workspace + verification run the orchestrator can drive."""
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
            runtime="opensandbox",
            status="ready",
            workspace_path="workspace",
            sandbox_id=uuid4().hex,
        )
        session.add(user)
        await session.flush()
        session.add(workspace)
        await session.flush()
        run = VerificationRun(
            workspace_id=workspace.id, plan_ref="spec:1", status=status
        )
        session.add(run)
        await session.commit()
        return workspace, run, user.id


async def _cleanup(maker, workspace, user_id) -> None:
    async with maker() as session:
        row = await session.get(Workspace, workspace.id)
        user = await session.get(User, user_id)
        await session.delete(row)  # cascades to verification_runs -> verification_evidence
        await session.flush()
        await session.delete(user)
        await session.commit()


async def _evidence(maker, run_id) -> list[VerificationEvidence]:
    async with maker() as session:
        rows = (
            await session.execute(
                select(VerificationEvidence).where(VerificationEvidence.run_id == run_id)
            )
        ).scalars().all()
    return list(rows)


async def _run_status(maker, run_id) -> str | None:
    async with maker() as session:
        return await session.scalar(
            select(VerificationRun.status).where(VerificationRun.id == run_id)
        )


def _exit_handler(command: str) -> RuntimeCommandResult:
    if command == "fail":
        return RuntimeCommandResult(exit_code=3, stdout="", stderr="boom")
    return RuntimeCommandResult(exit_code=0, stdout="ok", stderr="")


def _html_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        text="<html><head><title>Demo App</title></head><body>hello world</body></html>",
        headers={"content-type": "text/html"},
    )


async def test_orchestrator_loop_and_gate(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    transport = httpx.MockTransport(_html_handler)
    criteria = PlanCriteria(test_command="pytest", observe_port=5173)
    orch = VerificationOrchestrator(
        criteria,
        session_maker=maker,
        runtime=runtime,
        observe_transport=transport,
    )
    try:
        driven = await orch.run(workspace, run.id)

        assert driven.id == run.id
        assert driven.status == "review"
        assert await _run_status(maker, run.id) == "review"

        evidence = await _evidence(maker, run.id)
        kinds = {ev.kind for ev in evidence}
        assert kinds == {"test", "observation"}
        test_ev = next(ev for ev in evidence if ev.kind == "test")
        obs_ev = next(ev for ev in evidence if ev.kind == "observation")
        assert test_ev.status == "passed"
        assert obs_ev.status == "passed"
        # The screenshot is an opaque, not-yet-materialized reference — surfaced as-is.
        assert obs_ev.path == f"verification/{run.id}/5173.png"
        assert obs_ev.detail["screenshot"] == obs_ev.path

        assert await orch.approve(run.id) is True
        assert await _run_status(maker, run.id) == "approved"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_iterates_until_test_passes(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    calls = {"n": 0}

    def flaky_handler(command: str) -> RuntimeCommandResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return RuntimeCommandResult(exit_code=1, stdout="", stderr="first attempt fails")
        return RuntimeCommandResult(exit_code=0, stdout="ok", stderr="")

    runtime = FakeSandboxRuntime(exec_handler=flaky_handler)
    criteria = PlanCriteria(test_command="pytest", max_iterations=3)
    orch = VerificationOrchestrator(criteria, session_maker=maker, runtime=runtime)
    try:
        driven = await orch.run(workspace, run.id)

        assert driven.status == "review"
        test_evidence = [
            ev for ev in await _evidence(maker, run.id) if ev.kind == "test"
        ]
        assert {ev.status for ev in test_evidence} == {"failed", "passed"}
        assert len(test_evidence) == 2
        assert calls["n"] == 2
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_approve_reject_are_conditional(maker) -> None:
    workspace, run, user_id = await _seed(maker, status="review")
    orch = VerificationOrchestrator(PlanCriteria("pytest"), session_maker=maker)
    try:
        # Reject wins the race: review -> rejected.
        assert await orch.reject(run.id) is True
        assert await _run_status(maker, run.id) == "rejected"

        # A second gate submission has lost: the run is no longer review.
        assert await orch.reject(run.id) is False
        assert await orch.approve(run.id) is False
        assert await _run_status(maker, run.id) == "rejected"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_approve_requires_review_status(maker) -> None:
    workspace, run, user_id = await _seed(maker, status="testing")
    orch = VerificationOrchestrator(PlanCriteria("pytest"), session_maker=maker)
    try:
        assert await orch.approve(run.id) is False
        assert await _run_status(maker, run.id) == "testing"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_refuses_when_another_run_active(maker) -> None:
    workspace, run, user_id = await _seed(maker, status="testing")  # the active one
    orch = VerificationOrchestrator(
        PlanCriteria("pytest"), session_maker=maker, runtime=FakeSandboxRuntime()
    )
    try:
        async with maker() as session:
            second = VerificationRun(
                workspace_id=workspace.id, plan_ref="spec:2", status="planning"
            )
            session.add(second)
            await session.commit()
            second_id = second.id

        with pytest.raises(VerificationRunAlreadyActiveError):
            await orch.run(workspace, second_id)

        assert await _run_status(maker, second_id) == "planning"  # untouched
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_run_on_terminal_run_is_a_no_op(maker) -> None:
    workspace, run, user_id = await _seed(maker, status="approved")
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    orch = VerificationOrchestrator(
        PlanCriteria("pytest"), session_maker=maker, runtime=runtime
    )
    try:
        driven = await orch.run(workspace, run.id)

        assert driven.status == "approved"
        assert await _run_status(maker, run.id) == "approved"
        assert await _evidence(maker, run.id) == []  # no loop ran, no evidence
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_does_not_redrive_active_run(maker) -> None:
    """An already-active run (``testing``) is not silently re-driven: the entry transition
    only accepts ``planning``, so ``run`` is a no-op on a run that already left planning."""
    workspace, run, user_id = await _seed(maker, status="testing")
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    orch = VerificationOrchestrator(
        PlanCriteria("pytest"), session_maker=maker, runtime=runtime
    )
    try:
        driven = await orch.run(workspace, run.id)

        assert driven.status == "testing"  # unchanged — not re-driven
        assert await _run_status(maker, run.id) == "testing"
        assert await _evidence(maker, run.id) == []  # never entered the loop
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_orchestrator_marks_run_error_on_unexpected_exception(maker) -> None:
    """A non-provider exception escaping the loop (a DB blip, a dependency bug — anything the
    test runner's ``SandboxRuntimeError``/``CommandTimeoutError`` handling does not absorb)
    must move the run to the terminal ``error`` status so the one-active guard cannot wedge
    the workspace forever."""
    workspace, run, user_id = await _seed(maker)

    def raising_handler(command: str) -> RuntimeCommandResult:
        raise RuntimeError("dependency bug")

    runtime = FakeSandboxRuntime(exec_handler=raising_handler)
    orch = VerificationOrchestrator(
        PlanCriteria("pytest"), session_maker=maker, runtime=runtime
    )
    try:
        with pytest.raises(RuntimeError, match="dependency bug"):
            await orch.run(workspace, run.id)

        assert await _run_status(maker, run.id) == "error"
        assert await _evidence(maker, run.id) == []  # crashed before any result
    finally:
        await _cleanup(maker, workspace, user_id)


# --- Plan 2 Task 3: resumable, persisted-state driven loop ---------------------------------


async def _seed_with_criteria(maker, status: str, criteria: dict):
    workspace, run, user_id = await _seed(maker, status=status)
    async with maker() as session:
        row = await session.get(VerificationRun, run.id)
        row.criteria = criteria
        await session.commit()
    return workspace, run, user_id


async def test_advance_walks_one_step_at_a_time_from_persisted_criteria(maker) -> None:
    from app.services.verification.orchestrator import Step

    # The row's criteria win over the constructor's: any process can resume from the row.
    workspace, run, user_id = await _seed_with_criteria(
        maker, "planning", {"test_command": "fail", "max_iterations": 1}
    )
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    orch = VerificationOrchestrator(PlanCriteria("ignored"), session_maker=maker, runtime=runtime)
    try:
        assert await orch.advance(run.id) is Step.CONTINUE
        assert await _run_status(maker, run.id) == "testing"
        assert await orch.advance(run.id) is Step.CONTINUE
        assert await _run_status(maker, run.id) == "observing"
        assert await orch.advance(run.id) is Step.DONE
        assert await _run_status(maker, run.id) == "review"
        # Replaying on a parked run is a no-op.
        assert await orch.advance(run.id) is Step.DONE

        [test_ev] = await _evidence(maker, run.id)
        assert test_ev.detail["command"] == "fail"
        assert test_ev.status == "failed"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_drive_resumes_a_run_left_mid_loop(maker) -> None:
    """A run another process left in ``testing`` (redelivered job) is resumed, not wedged."""
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 1}
    )
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    orch = VerificationOrchestrator(PlanCriteria("ignored"), session_maker=maker, runtime=runtime)
    try:
        final = await orch.drive(run.id)

        assert final.status == "review"
        assert [ev.status for ev in await _evidence(maker, run.id)] == ["passed"]
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_advance_retests_without_a_fixer_and_counts_iterations(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "fail", "max_iterations": 3}
    )
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    orch = VerificationOrchestrator(PlanCriteria("ignored"), session_maker=maker, runtime=runtime)
    try:
        final = await orch.drive(run.id)

        assert final.status == "review"
        assert final.iteration == 2  # two re-tests after the first attempt
        assert len(await _evidence(maker, run.id)) == 3
    finally:
        await _cleanup(maker, workspace, user_id)
