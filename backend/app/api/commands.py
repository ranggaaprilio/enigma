import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    CustomCommandInvalidError,
    CustomCommandNotFoundError,
    command_runtime,
    command_status_frame,
)
from app.services.rate_limit import RedisRateLimiter
from app.services.run_detection import RunDetectionService, RunDetectionUnavailableError
from app.services.run_presets import RUN_PRESETS, UnknownPresetError, get_preset
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["commands"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)
# Without bus traffic for this long, a command stream re-reads the run from Postgres.
COMMAND_STREAM_IDLE_SECONDS = 5.0


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
        health_path: str | None = None,
        is_entry_point: bool = False,
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
            health_path=payload.health_path,
            is_entry_point=payload.is_entry_point,
        )
    except WorkspaceNotFoundError:
        raise workspace_not_found() from None
    except CustomCommandInvalidError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.message) from None


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
    except CustomCommandInvalidError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error.message) from None


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
    try:
        if run.status != "running":
            if run.output:
                await websocket.send_json({"type": "output", "content": run.output})
            await websocket.send_json(command_status_frame(run))
        else:
            await tail_command_run(
                websocket.send_json,
                command_runtime,
                async_session_maker,
                settings,
                workspace_id,
                run_id,
                user_id,
            )
    except WebSocketDisconnect:
        return
    except (WorkspaceNotFoundError, CommandRunNotFoundError):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.close()


class CommandStreamRuntime(Protocol):
    def subscribe(self, run_id: UUID) -> Any: ...

    def unsubscribe(self, run_id: UUID, queue: Any) -> None: ...

    async def wait_ready(self, run_id: UUID) -> bool: ...


async def tail_command_run(
    send: Callable[[dict], Awaitable[None]],
    runtime: CommandStreamRuntime,
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    workspace_id: UUID,
    run_id: UUID,
    user_id: UUID,
    *,
    idle_seconds: float = COMMAND_STREAM_IDLE_SECONDS,
) -> None:
    """Stream a command run's output with no gaps or duplicates, ending with its status frame.

    Postgres is authoritative; the bus only says "there is more". The subscription goes live
    *before* the first read, so output written in between is not missed. Live frames carry their
    ``offset`` into ``run.output``: overlap with what was already sent is trimmed, and a frame
    starting past it (an earlier frame dropped by the at-most-once bus) triggers a re-read. An idle
    timeout re-reads too, so a lost status frame still ends the stream.
    """
    queue = runtime.subscribe(run_id)
    sent = 0

    async def catch_up() -> CommandRun:
        nonlocal sent
        async with session_maker() as session:
            service = CommandService(
                session=session, session_maker=session_maker, settings=settings
            )
            run = await service.get_run(workspace_id, run_id, user_id)
        output = run.output or ""
        if len(output) > sent:
            await send({"type": "output", "content": output[sent:]})
            sent = len(output)
        return run

    try:
        await runtime.wait_ready(run_id)
        run = await catch_up()
        while run.status == "running":
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=idle_seconds)
            except TimeoutError:
                run = await catch_up()
                continue
            offset, content = payload.get("offset"), payload.get("content")
            if payload.get("type") != "output" or not isinstance(content, str):
                # A status frame: the run's row is final, so read the rest from it.
                run = await catch_up()
                continue
            if not isinstance(offset, int) or offset > sent:
                run = await catch_up()
                continue
            unsent = content[sent - offset :]
            if unsent:
                await send({"type": "output", "content": unsent})
                sent += len(unsent)
        await send(command_status_frame(run))
    finally:
        runtime.unsubscribe(run_id, queue)


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
