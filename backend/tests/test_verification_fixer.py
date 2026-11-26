"""Plan 2 Task 4: an agent fixes failing tests between verification iterations.

The orchestrator-level behaviour runs against a fake ``Fixer`` (deterministic, no LLM); the
``AgentFixer`` adapter is covered for its prompt and for mapping every refusal of
``AgentService.create_run`` to ``FixRefusedError``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.lifecycle import ServiceDrainingError
from app.models import AgentRun, VerificationRun
from app.services.agents import AgentConfigurationError, AgentRunAlreadyActiveError
from app.services.budgets import BudgetExceededError
from app.services.capacity import RunCapacityExceededError
from app.services.jobs import RunQueueUnavailableError
from app.services.sandbox.base import RuntimeCommandResult
from app.services.verification.fixer import (
    AgentFixer,
    FixDeferredError,
    FixRefusedError,
    build_fix_prompt,
)
from app.services.verification.orchestrator import (
    PlanCriteria,
    Step,
    VerificationOrchestrator,
)
from app.services.workspaces import WorkspaceAccessDeniedError
from tests.sandbox.fakes import FakeSandboxRuntime
from tests.test_verification_orchestrator import (
    _cleanup,
    _evidence,
    _run_status,
    _seed_with_criteria,
)


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeFixer:
    """Starts a placeholder agent run row (``verification_runs.agent_run_id`` is a real FK)
    and reports whatever status the test sets instead of the row's."""

    def __init__(self, maker=None, refuse: str | None = None, defer: bool = False) -> None:
        self.maker = maker
        self.refuse = refuse
        self.defer = defer
        self.started: list[tuple[UUID, str]] = []
        self.statuses: dict[UUID, str] = {}

    async def start(self, workspace, run, failing) -> UUID:
        if self.defer:
            raise FixDeferredError
        if self.refuse:
            raise FixRefusedError(self.refuse)
        agent_run_id = uuid4()
        async with self.maker() as session:
            session.add(
                AgentRun(
                    id=agent_run_id,
                    workspace_id=workspace.id,
                    user_id=workspace.owner_id,
                    prompt="fix",
                    status="completed",  # the fake's ``statuses`` is what the loop sees
                    provider="mock",
                    model="mock",
                )
            )
            await session.commit()
        output = failing.detail.get("output", "") if failing is not None else ""
        self.started.append((agent_run_id, output))
        self.statuses[agent_run_id] = "thinking"
        return agent_run_id

    async def status(self, agent_run_id: UUID) -> str | None:
        return self.statuses.get(agent_run_id)


def _fails_until_fixed(state: dict):
    def handler(_command: str) -> RuntimeCommandResult:
        if state["fixed"]:
            return RuntimeCommandResult(exit_code=0, stdout="1 passed", stderr="")
        return RuntimeCommandResult(exit_code=1, stdout="AssertionError: boom", stderr="")

    return handler


async def _agent_run_id(maker, run_id) -> UUID | None:
    async with maker() as session:
        return (await session.get(VerificationRun, run_id)).agent_run_id


async def test_failed_test_hands_off_to_one_agent_fix_then_retests(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 2}
    )
    state = {"fixed": False}
    fixer = FakeFixer(maker)
    orch = VerificationOrchestrator(
        PlanCriteria("ignored"),
        session_maker=maker,
        runtime=FakeSandboxRuntime(exec_handler=_fails_until_fixed(state)),
        fixer=fixer,
    )
    try:
        assert await orch.advance(run.id) is Step.CONTINUE  # test fails
        assert await _run_status(maker, run.id) == "fixing"

        assert await orch.advance(run.id) is Step.WAIT  # agent run started
        [(agent_run_id, output)] = fixer.started
        assert "AssertionError: boom" in output  # the agent got the failure output
        assert await _agent_run_id(maker, run.id) == agent_run_id

        # Replaying while the agent works neither starts a second agent run nor moves on.
        assert await orch.advance(run.id) is Step.WAIT
        assert len(fixer.started) == 1

        fixer.statuses[agent_run_id] = "completed"
        state["fixed"] = True
        final = await orch.drive(run.id)

        assert final.status == "review"
        assert final.iteration == 1
        kinds = [(ev.kind, ev.status) for ev in await _evidence(maker, run.id)]
        assert ("fix", "passed") in kinds
        assert [k for k in kinds if k[0] == "test"] == [("test", "failed"), ("test", "passed")]
        fix_ev = next(ev for ev in await _evidence(maker, run.id) if ev.kind == "fix")
        assert fix_ev.detail["agent_run_id"] == str(agent_run_id)
        assert fix_ev.detail["agent_status"] == "completed"
    finally:
        await _cleanup(maker, workspace, user_id)


@pytest.mark.parametrize("agent_status", ["error", "stopped"])
async def test_a_failed_agent_run_is_recorded_and_still_retested(maker, agent_status) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 2}
    )
    fixer = FakeFixer(maker)
    orch = VerificationOrchestrator(
        PlanCriteria("ignored"),
        session_maker=maker,
        runtime=FakeSandboxRuntime(exec_handler=_fails_until_fixed({"fixed": False})),
        fixer=fixer,
    )
    try:
        await orch.advance(run.id)  # test fails -> fixing
        await orch.advance(run.id)  # agent started
        [(agent_run_id, _)] = fixer.started
        fixer.statuses[agent_run_id] = agent_status

        final = await orch.drive(run.id)

        assert final.status == "review"  # budget spent: gate with the evidence
        evidence = await _evidence(maker, run.id)
        fix_ev = next(ev for ev in evidence if ev.kind == "fix")
        assert fix_ev.status == "error"
        assert fix_ev.detail["agent_status"] == agent_status
        assert len([ev for ev in evidence if ev.kind == "test"]) == 2  # re-tested anyway
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_refused_fix_goes_to_the_gate_with_the_reason(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 3}
    )
    orch = VerificationOrchestrator(
        PlanCriteria("ignored"),
        session_maker=maker,
        runtime=FakeSandboxRuntime(exec_handler=_fails_until_fixed({"fixed": False})),
        fixer=FakeFixer(maker, refuse="the budget for this workspace is exhausted"),
    )
    try:
        final = await orch.drive(run.id)

        assert final.status == "review"  # never wedged in fixing
        evidence = await _evidence(maker, run.id)
        fix_ev = next(ev for ev in evidence if ev.kind == "fix")
        assert fix_ev.status == "error"
        assert fix_ev.detail["error"] == "the budget for this workspace is exhausted"
        assert len([ev for ev in evidence if ev.kind == "test"]) == 1  # no pointless re-tests
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_draining_process_defers_the_fix_instead_of_giving_up(maker) -> None:
    """A rollout must not turn a fix attempt into a permanent refusal: the step waits and a
    live process (or the next job attempt) starts the agent run later."""
    workspace, run, user_id = await _seed_with_criteria(
        maker, "fixing", {"test_command": "pytest", "max_iterations": 2}
    )
    fixer = FakeFixer(maker, defer=True)
    orch = VerificationOrchestrator(
        PlanCriteria("ignored"),
        session_maker=maker,
        runtime=FakeSandboxRuntime(exec_handler=_fails_until_fixed({"fixed": False})),
        fixer=fixer,
    )
    try:
        assert await orch.advance(run.id) is Step.WAIT
        assert await _run_status(maker, run.id) == "fixing"
        assert await _evidence(maker, run.id) == []

        fixer.defer = False
        assert await orch.advance(run.id) is Step.WAIT  # now the agent run starts
        assert len(fixer.started) == 1
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_vanished_agent_run_does_not_wedge_the_loop(maker) -> None:
    workspace, run, user_id = await _seed_with_criteria(
        maker, "testing", {"test_command": "pytest", "max_iterations": 2}
    )
    fixer = FakeFixer(maker)
    orch = VerificationOrchestrator(
        PlanCriteria("ignored"),
        session_maker=maker,
        runtime=FakeSandboxRuntime(exec_handler=_fails_until_fixed({"fixed": False})),
        fixer=fixer,
    )
    try:
        await orch.advance(run.id)
        await orch.advance(run.id)
        fixer.statuses.clear()  # e.g. the agent run row was deleted

        final = await orch.drive(run.id)
        assert final.status == "review"
    finally:
        await _cleanup(maker, workspace, user_id)


# --- AgentFixer -------------------------------------------------------------------------


def test_fix_prompt_carries_plan_command_and_failure_output() -> None:
    prompt = build_fix_prompt(
        plan_ref="specs/001/plan.md",
        command="pytest -q",
        exit_code=1,
        output="E   AssertionError: boom",
    )
    assert "specs/001/plan.md" in prompt
    assert "pytest -q" in prompt
    assert "AssertionError: boom" in prompt
    assert "Do not delete or weaken" in prompt


@pytest.mark.parametrize(
    "error",
    [
        AgentRunAlreadyActiveError(),
        BudgetExceededError("workspace", "monthly"),
        RunCapacityExceededError("team", retry_after=30),
        AgentConfigurationError("no llm"),
        RunQueueUnavailableError(),
        WorkspaceAccessDeniedError(),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_agent_fixer_maps_refusals(error) -> None:
    await _assert_refused(error)


async def _assert_refused(error: Exception) -> None:
    class RefusingService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_run(self, *_args, **_kwargs):
            raise error

    fixer = AgentFixer(session_maker=_NullMaker(), service_factory=RefusingService)
    run = VerificationRun(
        workspace_id=uuid4(), plan_ref="p", status="fixing", created_by=uuid4(), criteria={}
    )
    failing = _Evidence({"command": "pytest", "exit_code": 1, "output": "boom"})
    with pytest.raises(FixRefusedError) as raised:
        await fixer.start(_Workspace(), run, failing)
    assert str(raised.value)  # a human-readable reason, never empty


async def test_agent_fixer_defers_while_draining() -> None:
    class DrainingService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_run(self, *_args, **_kwargs):
            raise ServiceDrainingError

    fixer = AgentFixer(session_maker=_NullMaker(), service_factory=DrainingService)
    run = VerificationRun(
        workspace_id=uuid4(), plan_ref="p", status="fixing", created_by=uuid4(), criteria={}
    )
    with pytest.raises(FixDeferredError):
        await fixer.start(_Workspace(), run, _Evidence({"command": "pytest"}))


async def test_agent_fixer_refuses_without_an_author() -> None:
    fixer = AgentFixer(session_maker=_NullMaker(), service_factory=None)
    run = VerificationRun(workspace_id=uuid4(), plan_ref="p", status="fixing", criteria={})
    with pytest.raises(FixRefusedError):
        await fixer.start(_Workspace(), run, _Evidence({"command": "pytest"}))


class _Evidence:
    def __init__(self, detail: dict) -> None:
        self.detail = detail


class _Workspace:
    id = uuid4()


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _NullMaker:
    def __call__(self):
        return _NullSession()
