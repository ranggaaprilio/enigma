from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.core.security import verify_session_token
from app.db.session import async_session_maker, get_db_session
from app.models import CommandRun, CustomCommand, User
from app.schemas.commands import (
    CommandReorderRequest,
    CommandRunCreateRequest,
    CommandRunResponse,
    CustomCommandCreateRequest,
    CustomCommandResponse,
    CustomCommandUpdateRequest,
    DetectCommandsResponse,
    DetectedCommandResponse,
    ImportCommandsRequest,
    RunPresetResponse,
)
from app.schemas.workspaces import DeleteResponse
from app.services.commands import (
    CommandConfirmationRequiredError,
    CommandRunNotFoundError,
    CommandService,
    CommandWorkspacePathError,
    CustomCommandNotFoundError,
    command_runtime,
)
from app.services.rate_limit import RedisRateLimiter
from app.services.run_detection import RunDetectionService, RunDetectionUnavailableError
from app.services.run_presets import RUN_PRESETS, UnknownPresetError, get_preset
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["commands"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


async def enforce_command_run_rate_limit(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    settings: Settings = SETTINGS_DEP,
) -> None:
    """Throttle command-run creation per (user, workspace).

    Runs are fire-and-forget background tasks against the sandbox with no concurrency cap, so
    without this a tight client loop can saturate the workspace.
    """
    limiter = RedisRateLimiter(
        prefix="command:run",
        scope="command_run",
        attempts=settings.command_run_rate_limit_attempts,
        window_seconds=settings.command_run_rate_limit_window_seconds,
    )
    await limiter.check(f"{current_user.id}:{workspace_id}")


COMMAND_RUN_RATE_LIMIT_DEP = Depends(enforce_command_run_rate_limit)


class CommandServiceProtocol(Protocol):
    async def create_command(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
        command: str,
        description: str | None,
        icon: str | None,
        hotkey: str | None,
        requires_confirmation: bool,
        role: str = "custom",
        kind: str = "task",
        cwd: str | None = None,
        port: int | None = None,
        enabled: bool = True,
        timeout_seconds: int | None = None,
    ) -> CustomCommand: ...

    async def list_commands(self, workspace_id: UUID, user_id: UUID) -> list[CustomCommand]: ...

    async def update_command(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        values: dict,
    ) -> CustomCommand: ...

    async def delete_command(self, workspace_id: UUID, command_id: UUID, user_id: UUID) -> None:
        ...

    async def reorder_commands(
        self,
        workspace_id: UUID,
        user_id: UUID,
        order: dict[UUID, int],
    ) -> list[CustomCommand]: ...

    async def import_commands(
        self,
        workspace_id: UUID,
        user_id: UUID,
        items: list[dict],
    ) -> list[CustomCommand]: ...

    async def create_run(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        confirmed: bool,
        worktree_id: UUID | None = None,
    ) -> CommandRun: ...

    async def list_runs(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
    ) -> list[CommandRun]: ...


async def get_command_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> CommandServiceProtocol:
    return CommandService(
        session=session,
        session_maker=async_session_maker,
        settings=settings,
    )


COMMAND_SERVICE_DEP = Depends(get_command_service)


def workspace_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")


def command_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")


@router.post("/commands", response_model=CustomCommandResponse, status_code=status.HTTP_201_CREATED)
async def create_custom_command(
    workspace_id: UUID,
    payload: CustomCommandCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> CustomCommand:
    try:
        return await service.create_command(
            workspace_id=workspace_id,
            user_id=current_user.id,
            name=payload.name,
            command=payload.command,
            description=payload.description,
            icon=payload.icon,
            hotkey=payload.hotkey,
            requires_confirmation=payload.requires_confirmation,
            role=payload.role,
            # Only an explicit `kind` overrides the role-based default (a `dev`-role command
            # defaults to `service`); omit it entirely so the service applies that default.
            kind=payload.kind if "kind" in payload.model_fields_set else None,
            cwd=payload.cwd,
            port=payload.port,
            enabled=payload.enabled,
            timeout_seconds=payload.timeout_seconds,
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None


@router.get("/commands", response_model=list[CustomCommandResponse])
async def list_custom_commands(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> list[CustomCommand]:
    try:
        return await service.list_commands(workspace_id, current_user.id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None


@router.post("/commands/detect", response_model=DetectCommandsResponse)
async def detect_run_commands(
    workspace_id: UUID,
    worktree_id: UUID | None = None,
    current_user: User = CURRENT_USER_DEP,
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> DetectCommandsResponse:
    """Propose registry entries from the repository's manifests.

    Read-only: nothing is persisted, and no repository code is executed. The user picks which
    candidates to keep and posts them back to ``/commands/import``.
    """
    service = RunDetectionService(session=session, settings=settings)
    try:
        candidates = await service.detect(workspace_id, current_user.id, worktree_id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorktreeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Worktree not found"
        ) from None
    except RunDetectionUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from None
    return DetectCommandsResponse(
        candidates=[DetectedCommandResponse(**vars(candidate)) for candidate in candidates]
    )


@router.post(
    "/commands/import",
    response_model=list[CustomCommandResponse],
    status_code=status.HTTP_201_CREATED,
)
async def import_run_commands(
    workspace_id: UUID,
    payload: ImportCommandsRequest,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> list[CustomCommand]:
    try:
        return await service.import_commands(
            workspace_id=workspace_id,
            user_id=current_user.id,
            items=[item.model_dump() for item in payload.items],
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None


@router.get("/commands/presets", response_model=list[RunPresetResponse])
async def list_run_presets(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
) -> list[RunPresetResponse]:
    return [RunPresetResponse(**preset.as_dict()) for preset in RUN_PRESETS]


@router.post(
    "/commands/presets/{slug}/apply",
    response_model=list[CustomCommandResponse],
    status_code=status.HTTP_201_CREATED,
)
async def apply_run_preset(
    workspace_id: UUID,
    slug: str,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> list[CustomCommand]:
    try:
        preset = get_preset(slug)
    except UnknownPresetError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Preset not found"
        ) from None
    try:
        return await service.import_commands(
            workspace_id=workspace_id,
            user_id=current_user.id,
            items=preset.as_dict()["commands"],
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None


@router.post("/commands/reorder", response_model=list[CustomCommandResponse])
async def reorder_custom_commands(
    workspace_id: UUID,
    payload: CommandReorderRequest,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> list[CustomCommand]:
    try:
        return await service.reorder_commands(
            workspace_id=workspace_id,
            user_id=current_user.id,
            order={item.id: item.sort_order for item in payload.items},
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None


@router.patch("/commands/{command_id}", response_model=CustomCommandResponse)
async def update_custom_command(
    workspace_id: UUID,
    command_id: UUID,
    payload: CustomCommandUpdateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> CustomCommand:
    try:
        return await service.update_command(
            workspace_id=workspace_id,
            command_id=command_id,
            user_id=current_user.id,
            values=payload.model_dump(exclude_unset=True),
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except CustomCommandNotFoundError:
        raise command_not_found() from None


@router.delete("/commands/{command_id}", response_model=DeleteResponse)
async def delete_custom_command(
    workspace_id: UUID,
    command_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete_command(workspace_id, command_id, current_user.id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except CustomCommandNotFoundError:
        raise command_not_found() from None
    return DeleteResponse(ok=True)


@router.post(
    "/commands/{command_id}/run",
    response_model=CommandRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def run_custom_command(
    workspace_id: UUID,
    command_id: UUID,
    payload: CommandRunCreateRequest | None = None,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
    _: None = COMMAND_RUN_RATE_LIMIT_DEP,
) -> CommandRun:
    try:
        return await service.create_run(
            workspace_id=workspace_id,
            command_id=command_id,
            user_id=current_user.id,
            confirmed=payload.confirmed if payload else False,
            worktree_id=payload.worktree_id if payload else None,
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except WorktreeNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Worktree not found"
        ) from None
    except CustomCommandNotFoundError:
        raise command_not_found() from None
    except CommandConfirmationRequiredError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Command requires confirmation",
        ) from None
    except CommandWorkspacePathError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace directory is not ready",
        ) from None


@router.get("/commands/{command_id}/runs", response_model=list[CommandRunResponse])
async def list_custom_command_runs(
    workspace_id: UUID,
    command_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    service: CommandServiceProtocol = COMMAND_SERVICE_DEP,
) -> list[CommandRun]:
    try:
        return await service.list_runs(workspace_id, command_id, current_user.id)
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except CustomCommandNotFoundError:
        raise command_not_found() from None


@router.websocket("/command-runs/{run_id}/stream")
async def stream_command_run(websocket: WebSocket, workspace_id: UUID, run_id: UUID) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        user_id = user.id

        service = CommandService(
            session=session,
            session_maker=async_session_maker,
            settings=settings,
        )
        try:
            run = await service.get_run(workspace_id, run_id, user_id)
        except (WorkspaceNotFoundError, CommandRunNotFoundError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    if run.output:
        await websocket.send_json({"type": "output", "content": run.output})
    if run.status != "running":
        await websocket.send_json(
            {"type": "status", "status": run.status, "exit_code": run.exit_code}
        )
        await websocket.close()
        return

    queue = command_runtime.subscribe(run.id)
    try:
        async with async_session_maker() as session:
            service = CommandService(
                session=session,
                session_maker=async_session_maker,
                settings=settings,
            )
            try:
                current_run = await service.get_run(workspace_id, run_id, user_id)
            except (WorkspaceNotFoundError, CommandRunNotFoundError):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            if current_run.status != "running":
                await websocket.send_json(
                    {
                        "type": "status",
                        "status": current_run.status,
                        "exit_code": current_run.exit_code,
                    }
                )
                await websocket.close()
                return
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)
            if payload.get("type") == "status":
                await websocket.close()
                return
    except WebSocketDisconnect:
        return
    finally:
        command_runtime.unsubscribe(run.id, queue)


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
