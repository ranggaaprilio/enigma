import asyncio
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.metrics import agent_runs_created_total
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
from app.services.access import WorkspaceAccessDeniedError, can_drive, require_workspace_access
from app.services.agents import (
    AgentConfigurationError,
    AgentRunAlreadyActiveError,
    AgentRunNotFoundError,
    AgentRunNotInWorkspaceError,
    AgentService,
    agent_runtime,
    event_payload,
)
from app.services.budgets import BudgetExceededError
from app.services.daytona_runtime import get_daytona_runtime
from app.services.rate_limit import RedisRateLimiter
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError, WorktreeService

router = APIRouter(prefix="/workspaces/{workspace_id}/agent", tags=["agents"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


async def enforce_agent_run_rate_limit(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    settings: Settings = SETTINGS_DEP,
) -> None:
    """Throttle agent-run creation per (user, workspace). Raises 429 when the window limit
    is exceeded — protects the AI/compute path from being hammered."""
    limiter = RedisRateLimiter(
        prefix="agent:run",
        scope="agent_run",
        attempts=settings.agent_run_rate_limit_attempts,
        window_seconds=settings.agent_run_rate_limit_window_seconds,
    )
    await limiter.check(f"{current_user.id}:{workspace_id}")


AGENT_RUN_RATE_LIMIT_DEP = Depends(enforce_agent_run_rate_limit)

# Send a lightweight keepalive frame when the agent produces no events for this
# long, so idle WebSockets are not reaped by intermediaries during long pauses.
STREAM_PING_INTERVAL_SECONDS = 20.0


class AgentServiceProtocol(Protocol):
    async def create_run(
        self,
        workspace_id: UUID,
        user_id: UUID,
        prompt: str,
        working_dir: str | None = None,
        worktree_id: UUID | None = None,
    ) -> AgentRun: ...

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


async def get_worktree_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> WorktreeService:
    return WorktreeService(
        session=session,
        runtime=get_daytona_runtime(),
        settings=settings,
    )


WORKTREE_SERVICE_DEP = Depends(get_worktree_service)


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
        session_id=run.session_id,
        prompt=run.prompt,
        status=run.status,
        provider=run.provider,
        model=run.model,
        tokens_used=run.tokens_used,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        estimated_cost=float(run.estimated_cost) if run.estimated_cost is not None else None,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error_message=run.error_message,
        events=[agent_event_response(event) for event in run.events],
    )


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found")


@router.post(
    "/runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AGENT_RUN_RATE_LIMIT_DEP],
)
async def create_agent_run(
    workspace_id: UUID,
    payload: AgentRunCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: AgentServiceProtocol = AGENT_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> AgentRun:
    try:
        working_dir: str | None = None
        if payload.worktree_id is not None:
            worktree = await worktree_service.get_worktree_for_driver(
                workspace_id, current_user.id, payload.worktree_id
            )
            working_dir = worktree.path
        run = await service.create_run(
            workspace_id,
            current_user.id,
            payload.prompt,
            working_dir=working_dir,
            worktree_id=payload.worktree_id,
        )
        agent_runs_created_total.inc()
        return run
    except WorkspaceNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found",
        ) from None
    except WorkspaceAccessDeniedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to drive this workspace",
        ) from None
    except WorktreeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worktree not found",
        ) from None
    except AgentRunAlreadyActiveError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace already has an active agent run",
        ) from None
    except BudgetExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=str(error),
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
    except WorkspaceAccessDeniedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to drive this workspace",
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
            _workspace, viewer_role = await require_workspace_access(
                session, workspace_id, user.id
            )
            events = await service.list_events(workspace_id, run_id, user.id)
            run = await service.get_run(workspace_id, run_id, user.id)
        except (WorkspaceNotFoundError, AgentRunNotFoundError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    await websocket.send_json(
        {"type": "role", "content": "driver" if can_drive(viewer_role) else "observer"}
    )
    for event in events:
        await websocket.send_json(event_payload(event))

    if run.status not in ("thinking", "executing"):
        await websocket.close()
        return

    queue = agent_runtime.subscribe(run_id)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(
                    queue.get(), timeout=STREAM_PING_INTERVAL_SECONDS
                )
            except TimeoutError:
                # No agent activity in a while; send a keepalive so the socket
                # stays open through long thinking pauses. The client ignores
                # frames of type "ping".
                await websocket.send_json({"type": "ping"})
                continue
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
