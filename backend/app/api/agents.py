import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.metrics import agent_runs_created_total
from app.core.middleware import websocket_context
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
    ACTIVE_RUN_STATUSES,
    AgentConfigurationError,
    AgentRunAlreadyActiveError,
    AgentRunNotFoundError,
    AgentRunNotInWorkspaceError,
    AgentService,
    agent_runtime,
    event_payload,
)
from app.services.budgets import BudgetExceededError
from app.services.rate_limit import RedisRateLimiter
from app.services.sandbox import get_sandbox_runtime
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
        runtime=get_sandbox_runtime(),
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
        error_reason=run.error_reason,
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
@websocket_context("agent")
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
            # Status before events (as in ``catch_up``): a run that reads as ended has every
            # event committed, so closing right after this snapshot cannot drop its tail.
            run = await service.get_run(workspace_id, run_id, user.id)
            events = await service.list_events(workspace_id, run_id, user.id)
        except (WorkspaceNotFoundError, AgentRunNotFoundError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    await websocket.send_json(
        {"type": "role", "content": "driver" if can_drive(viewer_role) else "observer"}
    )
    sent_ids: set[str] = set()
    # Newest ``created_at`` sent so far: idle-tick catch-ups read only from here on.
    high_water: datetime | None = None

    async def send_event(payload: dict) -> None:
        nonlocal high_water
        await websocket.send_json(payload)
        sent_ids.add(payload["id"])
        with suppress(KeyError, TypeError, ValueError):
            created_at = datetime.fromisoformat(payload["created_at"])
            if high_water is None or created_at > high_water:
                high_water = created_at

    for event in events:
        await send_event(event_payload(event, run.request_id))

    if run.status not in ("thinking", "executing"):
        await websocket.close()
        return

    queue = agent_runtime.subscribe(run_id)

    async def close_if_ended(*, final: bool = False) -> bool:
        # With runs on workers, events persisted between the history read above and the
        # subscription going live (or lost by the at-most-once bus) would never arrive here.
        # ``final`` (a terminal frame arrived live) re-reads everything before closing.
        missed, ended = await catch_up(
            async_session_maker,
            settings,
            workspace_id,
            run_id,
            user.id,
            sent_ids,
            since=None if final else high_water,
        )
        for payload in missed:
            await send_event(payload)
        if ended or final:
            await websocket.close()
        return ended or final

    try:
        await agent_runtime.wait_ready(run_id)
        if await close_if_ended():
            return
        while True:
            try:
                payload = await asyncio.wait_for(
                    queue.get(), timeout=STREAM_PING_INTERVAL_SECONDS
                )
            except TimeoutError:
                if await close_if_ended():
                    return
                # No agent activity in a while; send a keepalive so the socket
                # stays open through long thinking pauses. The client ignores
                # frames of type "ping".
                await websocket.send_json({"type": "ping"})
                continue
            payload_id = payload.get("id")
            if not isinstance(payload_id, str) or payload_id in sent_ids:
                continue
            await send_event(payload)
            status_value = payload.get("metadata", {}).get("status")
            if status_value in {"completed", "error", "stopped"}:
                await close_if_ended(final=True)
                return
    except WebSocketDisconnect:
        return
    except (WorkspaceNotFoundError, AgentRunNotFoundError):
        # Access revoked or run deleted mid-stream: the re-read in ``catch_up`` re-checks both.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
    finally:
        agent_runtime.unsubscribe(run_id, queue)


async def catch_up(
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    workspace_id: UUID,
    run_id: UUID,
    user_id: UUID,
    sent_ids: set[str],
    *,
    since: datetime | None = None,
) -> tuple[list[dict], bool]:
    """The persisted events this socket has not sent yet, and whether the run has ended.

    ``since`` limits the read to the socket's high-water mark, except once the run has ended:
    then everything is re-read, because a commit from another process (a stop accepted on
    another replica) can land late with an older ``created_at``.
    """
    async with session_maker() as session:
        service = AgentService(session=session, session_maker=session_maker, settings=settings)
        # Status before events: once it reads as ended, every event of the run is committed.
        run_row = (
            await session.execute(
                select(AgentRun.status, AgentRun.request_id).where(
                    AgentRun.id == run_id, AgentRun.workspace_id == workspace_id
                )
            )
        ).first()
        run_status, request_id = (run_row.status, run_row.request_id) if run_row else (None, None)
        ended = run_status not in ACTIVE_RUN_STATUSES
        events = await service.list_events(
            workspace_id, run_id, user_id, since=None if ended else since
        )
    missed = [
        event_payload(event, request_id) for event in events if str(event.id) not in sent_ids
    ]
    return missed, ended


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
