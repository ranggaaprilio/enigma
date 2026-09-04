import asyncio
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import verify_session_token
from app.db.session import async_session_maker
from app.models import User
from app.services.access import require_workspace_access
from app.services.presence import PRESENCE_CONNECTION_TTL_SECONDS, PresenceService
from app.services.redis import get_redis_client
from app.services.workspaces import WorkspaceNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/presence", tags=["presence"])

# Refresh the connection's TTL well before it expires, so a brief scheduling delay
# never lets an active viewer's presence entry lapse.
PRESENCE_HEARTBEAT_INTERVAL_SECONDS = PRESENCE_CONNECTION_TTL_SECONDS / 3
# How long to wait for the pub/sub subscription before snapshotting anyway.
PRESENCE_SUBSCRIBE_TIMEOUT_SECONDS = 5


@router.websocket("/stream")
async def stream_presence(websocket: WebSocket, workspace_id: UUID) -> None:
    settings = get_settings()
    async with async_session_maker() as session:
        user = await websocket_user(websocket, session, settings)
        if user is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        try:
            await require_workspace_access(session, workspace_id, user.id)
        except WorkspaceNotFoundError:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()

    scope = f"workspace:{workspace_id}"
    connection_id = str(uuid4())
    meta = {"name": user.name or user.email}
    redis = get_redis_client()
    presence = PresenceService(redis)

    try:
        # Subscribe *before* snapshotting, and hold delivery until the snapshot is on the
        # wire. Snapshotting first left a gap in which another viewer's join/leave was
        # published to nobody, so this client's list stayed wrong until it reconnected;
        # delivering deltas first would let a stale snapshot overwrite them.
        subscribed = asyncio.Event()
        snapshot_sent = asyncio.Event()

        async def relay() -> None:
            async for event in presence.subscribe(scope, ready=subscribed):
                await snapshot_sent.wait()
                await websocket.send_json(event)

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(PRESENCE_HEARTBEAT_INTERVAL_SECONDS)
                await presence.heartbeat(connection_id)

        relay_task = asyncio.create_task(relay())
        heartbeat_task: asyncio.Task | None = None
        joined = False
        try:
            try:
                await asyncio.wait_for(
                    subscribed.wait(), timeout=PRESENCE_SUBSCRIBE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                # Degraded but usable: the snapshot below is still correct, only the delta
                # stream may have missed the moments before it came up.
                pass

            entry = await presence.join(scope, user.id, connection_id, meta)
            joined = True
            # Publish the full viewer entry so subscribers get the same shape as a
            # snapshot row.
            await presence.publish_event(scope, {"type": "join", **entry})

            viewers = await presence.list_viewers(scope)
            await websocket.send_json({"type": "snapshot", "viewers": viewers})
            snapshot_sent.set()

            heartbeat_task = asyncio.create_task(heartbeat_loop())
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
        finally:
            relay_task.cancel()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            # Only announce a departure once the user's last connection is gone, so a
            # second tab closing does not remove a viewer who is still watching.
            if joined and await presence.leave(scope, user.id, connection_id):
                await presence.publish_event(
                    scope, {"type": "leave", "user_id": str(user.id), **meta}
                )
    finally:
        await redis.aclose()


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
