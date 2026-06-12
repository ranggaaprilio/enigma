from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.security import verify_session_token
from app.db.session import async_session_maker, get_db_session
from app.models import AgentEvent, AgentFileChange, AgentRun, User
from app.schemas.agents import (
    AgentEventResponse,
    AgentFileChangeListResponse,
    AgentFileChangeResponse,
    AgentRunCreateRequest,
    AgentRunDetailResponse,
    AgentRunResponse,
)
from app.services.agents import (
    AgentConfigurationError,
    AgentRunAlreadyActiveError,
    AgentRunNotFoundError,
    AgentRunNotInWorkspaceError,
    AgentService,
    agent_runtime,
    event_payload,
)
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/agent", tags=["agents"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class AgentServiceProtocol(Protocol):
    async def create_run(self, workspace_id: UUID, user_id: UUID, prompt: str) -> AgentRun: ...

    async def list_runs(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AgentRun]: ...

    async def get_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun: ...

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
    ) -> list[AgentEvent]: ...

    async def stop_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun: ...

    async def list_file_changes(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
        limit: int = 50,
    ) -> tuple[list[AgentFileChange], int, bool]: ...


async def get_agent_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> AgentServiceProtocol:
    return AgentService(
        session=session,
        session_maker=async_session_maker,
        settings=settings,
    )


AGENT_SERVICE_DEP = Depends(get_agent_service)


def agent_event_response(event: AgentEvent) -> AgentEventResponse:
    return AgentEventResponse(
        id=event.id,
        agent_run_id=event.agent_run_id,
        event_type=event.event_type,
        content=event.content,
        tool_call_id=event.tool_call_id,
        metadata=event.event_metadata,
        created_at=event.created_at,
    )


def agent_run_detail_response(run: AgentRun) -> AgentRunDetailResponse:
    return AgentRunDetailResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        user_id=run.user_id,
        prompt=run.prompt,
        status=run.status,
        provider=run.provider,
        model=run.model,
        tokens_used=run.tokens_used,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error_message=run.error_message,
        events=[agent_event_response(event) for event in run.events],
    )


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")


@router.post("/runs", response_model=AgentRunResponse, status_code=status.HTTP_201_CREATED)
async def create_agent_run(
    workspace_id: UUID,
    payload: AgentRunCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: AgentServiceProtocol = AGENT_SERVICE_DEP,
) -> AgentRun:
    try:
        return await service.create_run(workspace_id, current_user.id, payload.prompt)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None
    except AgentRunAlreadyActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace already has an active agent run",
        ) from None
    except AgentConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from None


@router.get("/runs", response_model=list[AgentRunResponse])
async def list_agent_runs(
    workspace_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = CURRENT_USER_DEP,
    service: AgentServiceProtocol = AGENT_SERVICE_DEP,
) -> list[AgentRun]:
    try:
        return await service.list_runs(workspace_id, current_user.id, limit=limit, offset=offset)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None


@router.get("/runs/{run_id}", response_model=AgentRunDetailResponse)
async def get_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: AgentServiceProtocol = AGENT_SERVICE_DEP,
) -> AgentRunDetailResponse:
    try:
        run = await service.get_run(workspace_id, run_id, current_user.id)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None
    except AgentRunNotFoundError:
        raise not_found() from None
    return agent_run_detail_response(run)


@router.get("/runs/{run_id}/file-changes", response_model=AgentFileChangeListResponse)
async def list_agent_run_file_changes(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: AgentServiceProtocol = AGENT_SERVICE_DEP,
) -> AgentFileChangeListResponse:
    try:
        changes, total, capped = await service.list_file_changes(
            workspace_id, run_id, current_user.id
        )
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None
    except AgentRunNotFoundError:
        raise not_found() from None
    except AgentRunNotInWorkspaceError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent run does not belong to this workspace",
        ) from None
    return AgentFileChangeListResponse(
        items=[AgentFileChangeResponse.model_validate(c) for c in changes],
        total=total,
        capped=capped,
    )


@router.post("/runs/{run_id}/stop", response_model=AgentRunResponse)
async def stop_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: AgentServiceProtocol = AGENT_SERVICE_DEP,
) -> AgentRun:
    try:
        return await service.stop_run(workspace_id, run_id, current_user.id)
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None
    except AgentRunNotFoundError:
        raise not_found() from None


@router.websocket("/runs/{run_id}/stream")
async def stream_agent_run(websocket: WebSocket, workspace_id: UUID, run_id: UUID) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        service = AgentService(
            session=session,
            session_maker=async_session_maker,
            settings=settings,
        )
        try:
            events = await service.list_events(workspace_id, run_id, user.id)
            run = await service.get_run(workspace_id, run_id, user.id)
        except (WorkspaceNotFoundError, AgentRunNotFoundError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    for event in events:
        await websocket.send_json(event_payload(event))

    if run.status not in ("thinking", "executing"):
        await websocket.close()
        return

    queue = agent_runtime.subscribe(run_id)
    try:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
            status_value = payload.get("metadata", {}).get("status")
            if status_value in {"completed", "error", "stopped"}:
                await websocket.close()
                return
    except WebSocketDisconnect:
        return
    finally:
        agent_runtime.unsubscribe(run_id, queue)


async def websocket_user(
    websocket: WebSocket,
    session: AsyncSession,
    settings: Settings,
) -> User | None:
    token = websocket.cookies.get(settings.auth_cookie_name)
    if token is None:
        return None
    payload = verify_session_token(token, settings.auth_secret_key)
    if payload is None:
        return None
    try:
        user_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        return None
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user
