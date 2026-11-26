"""The link between Spec Kit workflows and verification runs (Plan 3).

A workflow's ``implement`` phase is verified before the workflow can complete. This module
decides *how* (``criteria_for_workflow``): the workflow's own criteria, else the workspace's
first enabled ``test`` custom command, else not at all — so a workspace with neither keeps
today's behaviour. Commands run in the feature worktree, under the workspace's ``app_root``
and the command's own ``cwd``, exactly where the Commands panel would run them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import CustomCommand, SpecPhase, SpecWorkflow, VerificationRun, Workspace
from app.services.audit import AuditLogService
from app.services.jobs import RunDispatcher
from app.services.notifications import NotificationService
from app.services.run_paths import join_subpath
from app.services.sandbox import SandboxRuntime
from app.services.verification.execution import build_orchestrator, start_verification_run
from app.services.verification.orchestrator import (
    ACTIVE_STATUSES,
    PlanCriteria,
    VerificationOrchestrator,
)

logger = structlog.get_logger(__name__)

# Fix attempts allowed when the criteria come from a workspace ``test`` command (one fix).
DEFAULT_MAX_ITERATIONS = 2


def plan_ref_for(workflow: SpecWorkflow) -> str:
    return f"{workflow.spec_dir}/plan.md"


def _cwd(workspace: Workspace, workflow: SpecWorkflow, command_cwd: str | None) -> str:
    checkout = workflow.worktree_path or workspace.workspace_path
    return join_subpath(checkout, workspace.app_root, command_cwd)


async def criteria_for_workflow(
    session: AsyncSession, workspace: Workspace, workflow: SpecWorkflow
) -> PlanCriteria | None:
    own = workflow.verification
    if own and own.get("test_command"):
        return PlanCriteria(
            test_command=own["test_command"],
            observe_port=own.get("observe_port"),
            cwd=_cwd(workspace, workflow, None),
            max_iterations=own.get("max_iterations") or DEFAULT_MAX_ITERATIONS,
        )

    command = await session.scalar(
        select(CustomCommand)
        .where(
            CustomCommand.workspace_id == workspace.id,
            CustomCommand.role == "test",
            CustomCommand.enabled.is_(True),
        )
        .order_by(CustomCommand.sort_order, CustomCommand.created_at)
        .limit(1)
    )
    if command is None:
        return None
    return PlanCriteria(
        test_command=command.command,
        cwd=_cwd(workspace, workflow, command.cwd),
        max_iterations=DEFAULT_MAX_ITERATIONS,
    )


async def supersede_review_runs(
    session: AsyncSession,
    workflow_id: UUID,
    *,
    except_run_id: UUID | None = None,
    include_active: bool = False,
) -> None:
    """Close a workflow's linked runs still awaiting review (``-> rejected``).

    Their evidence verified code that has since changed (implement re-runs) or that a newer
    run re-verifies, so they must not stay approvable. ``include_active`` also closes runs
    still in flight -- needed when implement re-runs, or such a run would reach review later
    with evidence for the old code. Not committed: callers commit it with the change that made
    the evidence stale.
    """
    statuses = ("review", *ACTIVE_STATUSES) if include_active else ("review",)
    statement = update(VerificationRun).where(
        VerificationRun.spec_workflow_id == workflow_id,
        VerificationRun.status.in_(statuses),
    )
    if except_run_id is not None:
        statement = statement.where(VerificationRun.id != except_run_id)
    await session.execute(
        statement.values(status="rejected").execution_options(synchronize_session=False)
    )


async def start_spec_verification(
    *,
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
    workflow: SpecWorkflow,
    user_id: UUID,
    criteria: PlanCriteria,
    runtime: SandboxRuntime | None = None,
    dispatcher: RunDispatcher | None = None,
) -> VerificationRun:
    """Create a run linked to ``workflow`` and start it (inline task or queue job).

    Raises ``VerificationRunAlreadyActiveError`` (nothing written) or
    ``RunQueueUnavailableError`` (the run was ended as ``error``), like the API trigger.
    """
    orchestrator = build_orchestrator(settings, session_maker=session_maker, runtime=runtime)
    run = await orchestrator.create_run(
        workflow.workspace_id,
        plan_ref_for(workflow),
        criteria=criteria,
        created_by=user_id,
        spec_workflow_id=workflow.id,
    )
    # Only the newest evidence can be approved.
    async with session_maker() as session:
        await supersede_review_runs(session, workflow.id, except_run_id=run.id)
        await session.commit()
    await start_verification_run(
        run.id, settings, session_maker=session_maker, runtime=runtime, dispatcher=dispatcher
    )
    return run


async def approve_verification_run(
    session_maker: async_sessionmaker[AsyncSession], run_id: UUID, user_id: UUID
) -> bool:
    """Approve a run at the human gate; ``True`` iff it moved ``review -> approved``.

    For a run linked to a Spec Kit workflow whose ``implement`` phase is awaiting review, the
    same transaction approves that phase and completes the workflow, so the evidence and the
    phase can never disagree. While ``implement`` re-runs (request changes) the phase is not
    awaiting review, so approving the old evidence completes nothing.
    """
    async with session_maker() as session:
        if not await VerificationOrchestrator._transition(session, run_id, "approved", ("review",)):
            await session.commit()
            return False
        run = await session.get(VerificationRun, run_id)
        phase = None
        workflow = None
        if run.spec_workflow_id is not None:
            phase = await session.scalar(
                select(SpecPhase)
                .where(
                    SpecPhase.workflow_id == run.spec_workflow_id,
                    SpecPhase.phase == "implement",
                    SpecPhase.status == "awaiting_review",
                )
                .with_for_update()
            )
            if phase is not None:
                workflow = await session.get(SpecWorkflow, run.spec_workflow_id)
                phase.status = "approved"
                phase.reviewed_by = user_id
                phase.reviewed_at = datetime.now(UTC)
                workflow.status = "completed"
                session.add_all([phase, workflow])
        await session.commit()

        if phase is not None and workflow is not None:
            await _record_implement_approval(session, workflow, phase, run_id, user_id)
        return True


async def _record_implement_approval(
    session: AsyncSession,
    workflow: SpecWorkflow,
    phase: SpecPhase,
    run_id: UUID,
    user_id: UUID,
) -> None:
    """Audit + notify after the commit. Best effort: the approval itself already landed."""
    try:
        await AuditLogService(session).record(
            user_id=user_id,
            workspace_id=workflow.workspace_id,
            action="spec_phase_approved",
            target_type="spec_phase",
            target_id=phase.id,
            metadata={
                "phase": "implement",
                "workflow_id": str(workflow.id),
                "verification_run_id": str(run_id),
            },
        )
        await NotificationService(session).create(
            user_id=user_id,
            workspace_id=workflow.workspace_id,
            notification_type="spec_phase_approved",
            title="Feature verified and approved",
            message=f"'{workflow.feature_name}' passed review of its verification evidence.",
        )
    except Exception:
        logger.exception(
            "spec_implement_approval_record_failed", workflow_id=str(workflow.id)
        )
