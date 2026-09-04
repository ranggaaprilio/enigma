from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import async_session_maker, get_db_session
from app.models import User
from app.schemas.specs import (
    AnalyzeAnswersRequest,
    OpenQuestion,
    OpenQuestionsResponse,
    RequestChangesRequest,
    SpecArtifactItem,
    SpecArtifactListResponse,
    SpecArtifactResponse,
    SpecPhaseName,
    SpecPhaseResponse,
    SpecWorkflowCreateRequest,
    SpecWorkflowResponse,
)
from app.services.access import WorkspaceAccessDeniedError
from app.services.agents import AgentConfigurationError, AgentRunAlreadyActiveError
from app.services.speckit import (
    InvalidArtifactPathError,
    InvalidSpecPhaseError,
    SpecGateError,
    SpecKitNotReadyError,
    SpecKitService,
    SpecPhaseNotFoundError,
    SpecPhaseNotReviewableError,
    SpecWorkflowNotFoundError,
)
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/spec", tags=["spec"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


async def get_speckit_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> SpecKitService:
    return SpecKitService(
        session=session,
        session_maker=async_session_maker,
        settings=settings,
    )


SPECKIT_SERVICE_DEP = Depends(get_speckit_service)


def _map_spec_error(error: Exception) -> HTTPException:
    if isinstance(error, WorkspaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    if isinstance(error, WorkspaceAccessDeniedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to drive this workspace",
        )
    if isinstance(error, SpecWorkflowNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Spec workflow not found"
        )
    if isinstance(error, (SpecPhaseNotFoundError, InvalidSpecPhaseError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Spec phase not found")
    if isinstance(error, InvalidArtifactPathError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid artifact path"
        )
    if isinstance(error, SpecGateError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, SpecPhaseNotReviewableError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Phase is not awaiting review",
        )
    if isinstance(error, AgentRunAlreadyActiveError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace already has an active agent run",
        )
    if isinstance(error, SpecKitNotReadyError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    if isinstance(error, AgentConfigurationError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        )
    raise error


@router.post(
    "/workflows",
    response_model=SpecWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_spec_workflow(
    workspace_id: UUID,
    payload: SpecWorkflowCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecWorkflowResponse:
    try:
        workflow = await service.start_workflow(
            workspace_id, current_user.id, payload.feature_name, payload.description
        )
    except Exception as error:  # noqa: BLE001 - re-raised by _map_spec_error
        raise _map_spec_error(error) from None
    return SpecWorkflowResponse.model_validate(workflow)


@router.get("/workflows", response_model=list[SpecWorkflowResponse])
async def list_spec_workflows(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> list[SpecWorkflowResponse]:
    try:
        workflows = await service.list_workflows(workspace_id, current_user.id)
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return [SpecWorkflowResponse.model_validate(workflow) for workflow in workflows]


@router.get("/workflows/{workflow_id}", response_model=SpecWorkflowResponse)
async def get_spec_workflow(
    workspace_id: UUID,
    workflow_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecWorkflowResponse:
    try:
        workflow = await service.get_workflow(workspace_id, workflow_id, current_user.id)
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecWorkflowResponse.model_validate(workflow)


@router.get(
    "/workflows/{workflow_id}/phases/{phase}/artifacts",
    response_model=SpecArtifactListResponse,
)
async def list_spec_artifacts(
    workspace_id: UUID,
    workflow_id: UUID,
    phase: SpecPhaseName,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecArtifactListResponse:
    try:
        items = await service.list_phase_artifacts(
            workspace_id, workflow_id, phase, current_user.id
        )
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecArtifactListResponse(
        phase=phase,
        items=[SpecArtifactItem(name=name, path=path) for name, path in items],
    )


@router.get(
    "/workflows/{workflow_id}/phases/{phase}/artifact",
    response_model=SpecArtifactResponse,
)
async def get_spec_artifact(
    workspace_id: UUID,
    workflow_id: UUID,
    phase: SpecPhaseName,
    path: str | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecArtifactResponse:
    try:
        resolved, content = await service.read_artifact(
            workspace_id, workflow_id, phase, current_user.id, rel_path=path
        )
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecArtifactResponse(phase=phase, path=resolved, content=content)


@router.get(
    "/workflows/{workflow_id}/phases/analyze/questions",
    response_model=OpenQuestionsResponse,
)
async def get_analyze_questions(
    workspace_id: UUID,
    workflow_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> OpenQuestionsResponse:
    try:
        questions = await service.get_open_questions(workspace_id, workflow_id, current_user.id)
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    items: list[OpenQuestion] = []
    for item in questions:
        try:
            items.append(OpenQuestion.model_validate(item))
        except ValueError:
            # Skip malformed entries so a bad open-questions.json degrades gracefully.
            continue
    return OpenQuestionsResponse(phase="analyze", items=items)


@router.post(
    "/workflows/{workflow_id}/phases/analyze/answers",
    response_model=SpecPhaseResponse,
)
async def submit_analyze_answers(
    workspace_id: UUID,
    workflow_id: UUID,
    payload: AnalyzeAnswersRequest,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecPhaseResponse:
    try:
        result = await service.submit_analyze_answers(
            workspace_id,
            workflow_id,
            current_user.id,
            [{"id": a.id, "answer": a.answer} for a in payload.answers],
        )
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecPhaseResponse.model_validate(result)


@router.post(
    "/workflows/{workflow_id}/phases/{phase}/run",
    response_model=SpecPhaseResponse,
)
async def run_spec_phase(
    workspace_id: UUID,
    workflow_id: UUID,
    phase: SpecPhaseName,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecPhaseResponse:
    try:
        result = await service.run_phase(workspace_id, workflow_id, phase, current_user.id)
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecPhaseResponse.model_validate(result)


@router.post(
    "/workflows/{workflow_id}/phases/{phase}/approve",
    response_model=SpecPhaseResponse,
)
async def approve_spec_phase(
    workspace_id: UUID,
    workflow_id: UUID,
    phase: SpecPhaseName,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecPhaseResponse:
    try:
        result = await service.approve_phase(workspace_id, workflow_id, phase, current_user.id)
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecPhaseResponse.model_validate(result)


@router.post(
    "/workflows/{workflow_id}/phases/{phase}/request-changes",
    response_model=SpecPhaseResponse,
)
async def request_spec_changes(
    workspace_id: UUID,
    workflow_id: UUID,
    phase: SpecPhaseName,
    payload: RequestChangesRequest,
    current_user: User = CURRENT_USER_DEP,
    service: SpecKitService = SPECKIT_SERVICE_DEP,
) -> SpecPhaseResponse:
    try:
        result = await service.request_changes(
            workspace_id, workflow_id, phase, current_user.id, payload.notes
        )
    except Exception as error:  # noqa: BLE001
        raise _map_spec_error(error) from None
    return SpecPhaseResponse.model_validate(result)
