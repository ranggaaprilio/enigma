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

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.audit_support import record_audit_safely
from app.api.auth import CURRENT_USER_DEP
from app.api.rate_limits import SANDBOX_RATE_LIMIT_DEP
from app.core.config import get_settings
from app.core.lifecycle import lifecycle
from app.db.session import get_db_session
from app.models import User, VerificationArtifact, VerificationEvidence, VerificationRun
from app.schemas.verification import (
    AgentRunVerifyRequest,
    VerificationEvidenceResponse,
    VerificationRunCreateRequest,
    VerificationRunDetailResponse,
    VerificationRunResponse,
)
from app.services.access import WorkspaceAccessDeniedError, require_workspace_access
from app.services.artifact_store import ArtifactStoreError, get_artifact_store
from app.services.audit import AuditLogService
from app.services.verification.agent_link import (
    AgentRunNotFoundError,
    AgentRunNotVerifiableError,
    NoVerificationCriteriaError,
    create_agent_run_verification,
    criteria_for_agent_run,
    verifiable_agent_run,
)
from app.services.verification.execution import (
    build_orchestrator,
    start_verification_run,
    stop_verification_run,
)
from app.services.verification.orchestrator import (
    PlanCriteria,
    VerificationOrchestrator,
    VerificationRunAlreadyActiveError,
    VerificationRunNotFoundError,
    VerificationRunNotInWorkspaceError,
)
from app.services.verification.screenshot import PNG_SIGNATURE
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


@router.post(
    "/agent-runs/{run_id}",
    response_model=VerificationRunDetailResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def verify_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    payload: AgentRunVerifyRequest,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunDetailResponse:
    """Verify a finished agent run in its own checkout (Plan 9); the fix agent, if one is
    needed, continues that run's worktree and session."""
    lifecycle.ensure_accepting_work()
    workspace, _role = await _get_workspace(session, workspace_id, current_user)
    try:
        agent_run = await verifiable_agent_run(session, workspace_id, run_id)
        criteria = await criteria_for_agent_run(
            session,
            workspace,
            agent_run,
            test_command=payload.test_command,
            observe_port=payload.observe_port,
            max_iterations=payload.max_iterations,
        )
    except AgentRunNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found"
        ) from None
    except AgentRunNotVerifiableError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from None
    except NoVerificationCriteriaError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from None

    settings = get_settings()
    try:
        run = await create_agent_run_verification(
            build_orchestrator(settings), agent_run, criteria, created_by=current_user.id
        )
    except VerificationRunAlreadyActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace already has an active verification run",
        ) from None
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
    source_agent_run_id: UUID | None = Query(None),
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> list[VerificationRunResponse]:
    await _get_workspace(session, workspace_id, current_user)
    orch = VerificationOrchestrator(PlanCriteria(test_command=""))
    runs = await orch.list_runs(
        workspace_id,
        limit=limit,
        offset=offset,
        spec_workflow_id=spec_workflow_id,
        source_agent_run_id=source_agent_run_id,
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


# A stored screenshot is a tenant-rendered image: serve it so a browser can only ever show it
# as an image, never sniff or run it, and never let a shared cache keep it.
SCREENSHOT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "Cache-Control": "private, max-age=300",
    "Content-Disposition": "inline",
}


@router.get("/runs/{run_id}/evidence/{evidence_id}/screenshot")
async def get_evidence_screenshot(
    workspace_id: UUID,
    run_id: UUID,
    evidence_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> Response:
    """The PNG captured for an observation, read back from the private artifact store."""
    await _get_workspace(session, workspace_id, current_user)
    artifact = await session.scalar(
        select(VerificationArtifact)
        .join(VerificationEvidence, VerificationEvidence.id == VerificationArtifact.evidence_id)
        .join(VerificationRun, VerificationRun.id == VerificationEvidence.run_id)
        .where(
            VerificationArtifact.evidence_id == evidence_id,
            VerificationEvidence.run_id == run_id,
            VerificationRun.workspace_id == workspace_id,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screenshot not found")
    unavailable = HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Screenshot storage is unavailable",
    )
    store = get_artifact_store(get_settings())
    if store is None:
        raise unavailable
    try:
        data = await store.get(artifact.storage_key)
    except ArtifactStoreError:
        raise unavailable from None
    if not data.startswith(PNG_SIGNATURE):  # only ever serve what was validated as PNG
        raise unavailable
    return Response(content=data, media_type="image/png", headers=SCREENSHOT_HEADERS)


@router.post(
    "/runs/{run_id}/stop",
    response_model=VerificationRunResponse,
    dependencies=[SANDBOX_RATE_LIMIT_DEP],
)
async def stop_verification(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
) -> VerificationRunResponse:
    """Stop an active run (a human cancel: ``rejected``, spec §6), with its fix agent run."""
    workspace, _role = await _get_workspace(session, workspace_id, current_user)
    orch = build_orchestrator(get_settings())
    try:
        run = await orch.get_run(workspace_id, run_id)
    except (VerificationRunNotFoundError, VerificationRunNotInWorkspaceError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Verification run not found"
        ) from None
    if run.spec_workflow_id is not None and workspace.owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can stop a Spec Kit verification",
        )
    previous = await stop_verification_run(orch, run_id)
    if previous is None:
        run = await orch.get_run(workspace_id, run_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Verification run is not active (status={run.status})",
        )
    await record_audit_safely(
        AuditLogService(session),
        user_id=current_user.id,
        workspace_id=workspace_id,
        action="verification_run_stopped",
        target_type="verification_run",
        target_id=run_id,
        metadata={"previous_status": previous},
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
