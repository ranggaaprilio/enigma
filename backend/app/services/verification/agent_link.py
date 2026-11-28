"""Verify an ordinary agent run (Plan 9).

Spec Kit verifies its ``implement`` phase (``spec_link``); this does the same for any agent run a
user finished. The verification run names the agent run it checks (``source_agent_run_id``), its
criteria come from that run's own context -- the workspace's ``role="test"`` command run in the
run's checkout (its worktree, or the base clone) -- and its fix agent continues the run's
worktree and session (``AgentFixer``), so a fix never lands on another branch.

What cannot be verified is refused before anything is written: a run that did not complete, and
a fix run (it is verified as part of the run that started it).
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentRun, VerificationRun, Workspace
from app.services.run_paths import join_subpath, resolve_worktree
from app.services.verification.orchestrator import PlanCriteria, VerificationOrchestrator
from app.services.verification.spec_link import DEFAULT_MAX_ITERATIONS, test_command_criteria
from app.services.worktrees import WorktreeNotFoundError

# A prompt can be long; the run's plan reference only needs to say what was asked.
PLAN_REF_MAX_CHARS = 4_000
NO_CRITERIA = (
    "This workspace has no test command. Add a custom command with role 'test', "
    "or give a test command to run."
)


class AgentRunNotFoundError(Exception):
    """No such agent run in this workspace."""


class AgentRunNotVerifiableError(Exception):
    """The run cannot be verified (not completed, or itself a fix run)."""


class NoVerificationCriteriaError(Exception):
    """Neither given criteria nor a ``role="test"`` command to derive them from."""


async def verifiable_agent_run(
    session: AsyncSession, workspace_id: UUID, run_id: UUID
) -> AgentRun:
    run = await session.get(AgentRun, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise AgentRunNotFoundError
    if run.status != "completed":
        raise AgentRunNotVerifiableError("Only a completed agent run can be verified.")
    fix_of = await session.scalar(
        select(VerificationRun.id).where(VerificationRun.agent_run_id == run.id).limit(1)
    )
    if fix_of is not None:
        raise AgentRunNotVerifiableError(
            "This is a fix run of a verification; verify the run that started it."
        )
    return run


async def criteria_for_agent_run(
    session: AsyncSession,
    workspace: Workspace,
    run: AgentRun,
    *,
    test_command: str | None = None,
    observe_port: int | None = None,
    max_iterations: int | None = None,
) -> PlanCriteria:
    """The criteria to verify ``run`` with: the given ones, else derived from its context."""
    try:
        checkout, _branch, _id = await resolve_worktree(session, workspace, run.worktree_id)
    except WorktreeNotFoundError:
        raise AgentRunNotVerifiableError(
            "The worktree this run worked in no longer exists."
        ) from None
    if test_command:
        return PlanCriteria(
            test_command=test_command,
            observe_port=observe_port,
            cwd=join_subpath(checkout, workspace.app_root),
            max_iterations=max_iterations or DEFAULT_MAX_ITERATIONS,
        )
    derived = await test_command_criteria(session, workspace, checkout)
    if derived is None:
        raise NoVerificationCriteriaError(NO_CRITERIA)
    overrides = {"observe_port": observe_port, "max_iterations": max_iterations}
    return replace(derived, **{k: v for k, v in overrides.items() if v is not None})


async def create_agent_run_verification(
    orchestrator: VerificationOrchestrator,
    run: AgentRun,
    criteria: PlanCriteria,
    *,
    created_by: UUID,
) -> VerificationRun:
    """Create the linked run (parked at ``planning``; the caller starts it) and supersede the
    run's earlier verifications still in review: only the newest evidence may be approved.

    Raises ``VerificationRunAlreadyActiveError`` (nothing written) like any trigger.
    """
    verification = await orchestrator.create_run(
        run.workspace_id,
        run.prompt.strip()[:PLAN_REF_MAX_CHARS] or "Agent run",
        criteria=criteria,
        created_by=created_by,
        source_agent_run_id=run.id,
    )
    async with orchestrator.session_maker() as session:
        await session.execute(
            update(VerificationRun)
            .where(
                VerificationRun.source_agent_run_id == run.id,
                VerificationRun.status == "review",
                VerificationRun.id != verification.id,
            )
            .values(status="rejected")
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    return verification
