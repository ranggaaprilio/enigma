"""Verification loop API — trigger a run and drive the human gate.

Prefix: ``/workspaces/{workspace_id}/verification``.

The trigger endpoint runs the whole ``test -> observe -> iterate -> review`` loop
synchronously in the request (inline mode; there is no queue/worker path for
verification runs yet). A run is created at ``planning`` and returned at
``review`` — or ``error`` if a non-provider exception escaped the loop (the
orchestrator guarantees the run lands in a terminal state before re-raising).
The gate endpoints finalize a ``review`` run to ``approved``/``rejected``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import User, VerificationEvidence, VerificationRun
from app.schemas.verification import (
    VerificationEvidenceResponse,
    VerificationRunCreateRequest,
    VerificationRunDetailResponse,
    VerificationRunResponse,
)
from app.services.access import WorkspaceAccessDeniedError, require_workspace_access
from app.services.verification.orchestrator import (
    PlanCriteria,
    VerificationOrchestrator,
    VerificationRunAlreadyActiveError,
    VerificationRunNotFoundError,
    VerificationRunNotInWorkspaceError,
)
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
    status_code=status.HTTP_201_CREATED,
)
async def create_verification_run(
    workspace_id: UUID,
    payload: VerificationRunCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunDetailResponse:
    await _get_workspace(session, workspace_id, current_user)

    criteria = PlanCriteria(
        test_command=payload.test_command,
        observe_port=payload.observe_port,
        cwd=payload.cwd,
        timeout=payload.timeout,
        max_iterations=payload.max_iterations,
    )
    orch = VerificationOrchestrator(criteria)
    try:
        run = await orch.create_run(workspace_id, payload.plan_ref)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create verification run",
        ) from None

    # Drive the loop inline; the orchestrator parks the run at review (or error).
    try:
        workspace, _ = await _get_workspace(session, workspace_id, current_user)
        final = await orch.run(workspace, run.id)
    except VerificationRunAlreadyActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace already has an active verification run",
        ) from None
    except (VerificationRunNotFoundError, VerificationRunNotInWorkspaceError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Verification run not found"
        ) from None

    async with orch.session_maker() as s:
        final = await s.get(VerificationRun, final.id)
        evidence = (
            await s.scalars(
                select(VerificationEvidence)
                .where(VerificationEvidence.run_id == final.id)
                .order_by(VerificationEvidence.created_at)
            )
        ).all()
    return VerificationRunDetailResponse(
        **VerificationRunResponse.model_validate(final).model_dump(),
        evidence=[VerificationEvidenceResponse.model_validate(e) for e in evidence],
    )


@router.get("/runs", response_model=list[VerificationRunResponse])
async def list_verification_runs(
    workspace_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> list[VerificationRunResponse]:
    await _get_workspace(session, workspace_id, current_user)
    orch = VerificationOrchestrator(PlanCriteria(test_command=""))
    runs = await orch.list_runs(workspace_id, limit=limit, offset=offset)
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
    await _get_workspace(session, workspace_id, user)
    orch = VerificationOrchestrator(PlanCriteria(test_command=""))
    changed = await (orch.approve(run_id) if approve else orch.reject(run_id))
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
