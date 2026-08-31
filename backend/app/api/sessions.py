from typing import Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.agents import AGENT_RUN_RATE_LIMIT_DEP
from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.db.session import async_session_maker, get_db_session
from app.models import AgentRun, AgentSession, User
from app.schemas.agents import (
    AgentRunCreateRequest,
    AgentRunResponse,
    AgentSessionCreateRequest,
    AgentSessionDetailResponse,
    AgentSessionResponse,
    AgentSessionUpdateRequest,
)
from app.schemas.workspaces import DeleteResponse
from app.services.agents import (
    AgentConfigurationError,
    AgentRunAlreadyActiveError,
    AgentService,
    AgentSessionArchivedError,
    AgentSessionNotFoundError,
)
from app.services.daytona_runtime import get_daytona_runtime
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError, WorktreeService

router = APIRouter(prefix="/workspaces/{workspace_id}/agent/sessions", tags=["agent-sessions"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class AgentSessionServiceProtocol(Protocol):
    async def create_session(
        self,
        workspace_id: UUID,
        user_id: UUID,
        title: str | None = None,
    ) -> AgentSession: ...

    async def list_sessions(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> list[AgentSession]: ...

    async def get_session_detail(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
    ) -> tuple[AgentSession, list[AgentRun]]: ...

    async def update_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        title: str | None | object = ...,
        status: str | object = ...,
    ) -> AgentSession: ...

    async def delete_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
    ) -> None: ...

    async def append_turn(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        prompt: str,
        working_dir: str | None = None,
        worktree_id: UUID | None = None,
    ) -> AgentRun: ...


async def get_agent_session_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> AgentSessionServiceProtocol:
    return AgentService(
        session=session,
        session_maker=async_session_maker,
        settings=settings,
    )


AGENT_SESSION_SERVICE_DEP = Depends(get_agent_session_service)


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


def _workspace_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def _session_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


def agent_session_detail_response(
    agent_session: AgentSession, turns: list[AgentRun]
) -> AgentSessionDetailResponse:
    return AgentSessionDetailResponse(
        id=agent_session.id,
        workspace_id=agent_session.workspace_id,
        user_id=agent_session.user_id,
        title=agent_session.title,
        status=agent_session.status,
        created_at=agent_session.created_at,
        updated_at=agent_session.updated_at,
        turns=[AgentRunResponse.model_validate(turn) for turn in turns],
    )


@router.post("", response_model=AgentSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_agent_session(
    workspace_id: UUID,
    payload: AgentSessionCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: AgentSessionServiceProtocol = AGENT_SESSION_SERVICE_DEP,
) -> AgentSession:
    try:
        return await service.create_session(workspace_id, current_user.id, title=payload.title)
    except WorkspaceNotFoundError:
        raise _workspace_not_found() from None


@router.get("", response_model=list[AgentSessionResponse])
async def list_agent_sessions(
    workspace_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    # Inline Literal (not the AgentSessionStatus alias) so ruff's FastAPI B008 exemption
    # applies; behaviour is identical to the alias.
    session_status: Literal["active", "archived"] | None = Query(None, alias="status"),
    current_user: User = CURRENT_USER_DEP,
    service: AgentSessionServiceProtocol = AGENT_SESSION_SERVICE_DEP,
) -> list[AgentSession]:
    try:
        return await service.list_sessions(
            workspace_id,
            current_user.id,
            limit=limit,
            offset=offset,
            status=session_status,
        )
    except WorkspaceNotFoundError:
        raise _workspace_not_found() from None


@router.get("/{session_id}", response_model=AgentSessionDetailResponse)
async def get_agent_session(
    workspace_id: UUID,
    session_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: AgentSessionServiceProtocol = AGENT_SESSION_SERVICE_DEP,
) -> AgentSessionDetailResponse:
    try:
        agent_session, turns = await service.get_session_detail(
            workspace_id, session_id, current_user.id
        )
    except WorkspaceNotFoundError:
        raise _workspace_not_found() from None
    except AgentSessionNotFoundError:
        raise _session_not_found() from None
    return agent_session_detail_response(agent_session, turns)


@router.patch("/{session_id}", response_model=AgentSessionResponse)
async def update_agent_session(
    workspace_id: UUID,
    session_id: UUID,
    payload: AgentSessionUpdateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: AgentSessionServiceProtocol = AGENT_SESSION_SERVICE_DEP,
) -> AgentSession:
    fields = payload.model_fields_set
    try:
        return await service.update_session(
            workspace_id,
            session_id,
            current_user.id,
            title=payload.title if "title" in fields else ...,
            status=payload.status if "status" in fields else ...,
        )
    except WorkspaceNotFoundError:
        raise _workspace_not_found() from None
    except AgentSessionNotFoundError:
        raise _session_not_found() from None


@router.delete("/{session_id}", response_model=DeleteResponse)
async def delete_agent_session(
    workspace_id: UUID,
    session_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: AgentSessionServiceProtocol = AGENT_SESSION_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete_session(workspace_id, session_id, current_user.id)
    except WorkspaceNotFoundError:
        raise _workspace_not_found() from None
    except AgentSessionNotFoundError:
        raise _session_not_found() from None
    return DeleteResponse(ok=True)


@router.post(
    "/{session_id}/turns",
    response_model=AgentRunResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AGENT_RUN_RATE_LIMIT_DEP],
)
async def create_agent_session_turn(
    workspace_id: UUID,
    session_id: UUID,
    payload: AgentRunCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: AgentSessionServiceProtocol = AGENT_SESSION_SERVICE_DEP,
    worktree_service: WorktreeService = WORKTREE_SERVICE_DEP,
) -> AgentRun:
    try:
        working_dir: str | None = None
        if payload.worktree_id is not None:
            worktree = await worktree_service.get_worktree(
                workspace_id, current_user.id, payload.worktree_id
            )
            working_dir = worktree.path
        return await service.append_turn(
            workspace_id,
            session_id,
            current_user.id,
            payload.prompt,
            working_dir=working_dir,
            worktree_id=payload.worktree_id,
        )
    except WorkspaceNotFoundError:
        raise _workspace_not_found() from None
    except AgentSessionNotFoundError:
        raise _session_not_found() from None
    except WorktreeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worktree not found",
        ) from None
    except AgentSessionArchivedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is archived",
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
