"""Verification loop API — trigger a run and drive the human gate.

Prefix: ``/workspaces/{workspace_id}/verification``.

The trigger endpoint persists the run at ``planning`` (with its criteria and author) and
returns 202; the ``test -> (fix -> test)* -> observe -> review`` loop then runs off-request
(``services/verification/execution.py``: an in-process task inline, a worker job in queue
mode). Clients poll ``GET /runs/{id}``. The gate endpoints finalize a ``review`` run to
``approved``/``rejected``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import get_settings
from app.core.lifecycle import lifecycle
from app.db.session import get_db_session
from app.models import User, VerificationEvidence, VerificationRun
from app.schemas.verification import (
    VerificationEvidenceResponse,
    VerificationRunCreateRequest,
    VerificationRunDetailResponse,
    VerificationRunResponse,
)
from app.services.access import WorkspaceAccessDeniedError, require_workspace_access
from app.services.verification.execution import build_orchestrator, start_verification_run
from app.services.verification.orchestrator import (
    PlanCriteria,
    VerificationOrchestrator,
    VerificationRunAlreadyActiveError,
    VerificationRunNotFoundError,
    VerificationRunNotInWorkspaceError,
)
from app.services.verification.spec_link import approve_verification_run as approve_run
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/verification", tags=["verification"])

DB_SESSION_DEP = Depends(get_db_session)


async def _get_workspace(session: AsyncSession, workspace_id: UUID, user: User):
    try:
        return await require_workspace_access(session, workspace_id, user.id, require_drive=True)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        ) from None
    except WorkspaceAccessDeniedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to drive this workspace",
        ) from None


def _run_response(run: VerificationRun) -> VerificationRunResponse:
    return VerificationRunResponse.model_validate(run)


@router.post(
    "/runs",
    response_model=VerificationRunDetailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_verification_run(
    workspace_id: UUID,
    payload: VerificationRunCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunDetailResponse:
    lifecycle.ensure_accepting_work()
    await _get_workspace(session, workspace_id, current_user)

    criteria = PlanCriteria(
        test_command=payload.test_command,
        observe_port=payload.observe_port,
        cwd=payload.cwd,
        timeout=payload.timeout,
        max_iterations=payload.max_iterations,
    )
    settings = get_settings()
    orch = build_orchestrator(settings)
    try:
        run = await orch.create_run(
            workspace_id, payload.plan_ref, criteria=criteria, created_by=current_user.id
        )
    except VerificationRunAlreadyActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace already has an active verification run",
        ) from None

    # Off-request from here: an in-process task (inline) or a worker job (queue). A queue
    # failure ends the run and surfaces as 503 via the global RunQueueUnavailableError handler.
    await start_verification_run(run.id, settings)
    return VerificationRunDetailResponse(
        **VerificationRunResponse.model_validate(run).model_dump(), evidence=[]
    )


@router.get("/runs", response_model=list[VerificationRunResponse])
async def list_verification_runs(
    workspace_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    spec_workflow_id: UUID | None = Query(None),
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> list[VerificationRunResponse]:
    await _get_workspace(session, workspace_id, current_user)
    orch = VerificationOrchestrator(PlanCriteria(test_command=""))
    runs = await orch.list_runs(
        workspace_id, limit=limit, offset=offset, spec_workflow_id=spec_workflow_id
    )
    return [_run_response(r) for r in runs]


@router.get("/runs/{run_id}", response_model=VerificationRunDetailResponse)
async def get_verification_run(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunDetailResponse:
    await _get_workspace(session, workspace_id, current_user)
    orch = VerificationOrchestrator(PlanCriteria(test_command=""))
    try:
        run = await orch.get_run(workspace_id, run_id)
    except VerificationRunNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Verification run not found"
        ) from None
    except VerificationRunNotInWorkspaceError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verification run does not belong to this workspace",
        ) from None

    async with orch.session_maker() as s:
        evidence = (
            await s.scalars(
                select(VerificationEvidence)
                .where(VerificationEvidence.run_id == run.id)
                .order_by(VerificationEvidence.created_at)
            )
        ).all()
    return VerificationRunDetailResponse(
        **VerificationRunResponse.model_validate(run).model_dump(),
        evidence=[VerificationEvidenceResponse.model_validate(e) for e in evidence],
    )


async def _finalize_gate(
    workspace_id: UUID, run_id: UUID, user: User, session: AsyncSession, approve: bool
) -> VerificationRunResponse:
    workspace, _role = await _get_workspace(session, workspace_id, user)
    orch = VerificationOrchestrator(PlanCriteria(test_command=""))
    # Scope first: nothing is written for a run outside the workspace in the URL.
    try:
        run = await orch.get_run(workspace_id, run_id)
    except (VerificationRunNotFoundError, VerificationRunNotInWorkspaceError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Verification run not found"
        ) from None
    # A Spec Kit-linked run gates the workflow, which only its owner may approve or reject
    # (the same rule as approve_phase).
    if run.spec_workflow_id is not None and workspace.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can review a Spec Kit verification",
        )
    # Approval also completes a linked Spec Kit workflow's implement phase (same transaction).
    changed = await (
        approve_run(orch.session_maker, run_id, user.id)
        if approve
        else orch.reject(run_id)
    )
    if not changed:
        # Either the run does not exist, or it is no longer in review.
        try:
            run = await orch.get_run(workspace_id, run_id)
        except (VerificationRunNotFoundError, VerificationRunNotInWorkspaceError):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Verification run not found"
            ) from None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Verification run is not awaiting review (status={run.status})",
        )
    return _run_response(await orch.get_run(workspace_id, run_id))


@router.post("/runs/{run_id}/approve", response_model=VerificationRunResponse)
async def approve_verification_run(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunResponse:
    return await _finalize_gate(workspace_id, run_id, current_user, session, approve=True)


@router.post("/runs/{run_id}/reject", response_model=VerificationRunResponse)
async def reject_verification_run(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunResponse:
    return await _finalize_gate(workspace_id, run_id, current_user, session, approve=False)
