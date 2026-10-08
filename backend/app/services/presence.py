import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

PRESENCE_CONNECTION_TTL_SECONDS = 30


class PresenceService:
    """Tracks who is currently viewing a scope (e.g. a workspace), backed by Redis.

    The viewers hash is keyed by *connection* id, not user id: one user may have several
    tabs open, and each gets its own entry. That keeps a second tab from overwriting the
    first, and closing one tab from evicting a user who is still connected elsewhere.
    Every connection also writes a short-lived ``presence:conn:<id>`` key that its
    WebSocket refreshes; ``list_viewers`` treats a missing key as a dead connection and
    reaps it, so a crashed replica cannot leave ghost viewers behind forever.
    """

    def __init__(self, redis: Redis, *, subscriber: Redis | None = None) -> None:
        self.redis = redis
        # ``listen()`` blocks while a channel is quiet, so it runs on a client without a read
        # timeout; every command stays on ``redis`` and fails fast if Redis is gone.
        self.subscriber = subscriber or redis

    def _viewers_key(self, scope: str) -> str:
        return f"presence:{scope}:viewers"

    def _channel(self, scope: str) -> str:
        return f"presence:{scope}:events"

    def _conn_key(self, connection_id: str) -> str:
        return f"presence:conn:{connection_id}"

    async def join(self, scope: str, user_id: UUID, connection_id: str, meta: dict) -> dict:
        """Register a connection as viewing ``scope`` and return its viewer entry."""
        entry = {
            "user_id": str(user_id),
            "connection_id": connection_id,
            "joined_at": datetime.now(UTC).isoformat(),
            **meta,
        }
        # Liveness key first: `list_viewers` reaps any hash entry without one, so writing
        # the hash first opens a window in which a concurrent listing sees this connection
        # as dead and deletes it — the viewer then never appears at all.
        await self.redis.setex(self._conn_key(connection_id), PRESENCE_CONNECTION_TTL_SECONDS, "1")
        await self.redis.hset(self._viewers_key(scope), connection_id, json.dumps(entry))
        return entry

    async def leave(self, scope: str, user_id: UUID, connection_id: str) -> bool:
        """Drop a connection. Returns True when the user has no other live connection."""
        await self.redis.hdel(self._viewers_key(scope), connection_id)
        await self.redis.delete(self._conn_key(connection_id))
        remaining = await self.list_viewers(scope)
        return not any(viewer.get("user_id") == str(user_id) for viewer in remaining)

    async def heartbeat(self, connection_id: str) -> None:
        # SETEX, not EXPIRE: `EXPIRE` is a no-op on a missing key, so a connection whose
        # liveness key had already lapsed (a slow tick, a Redis restart) could never come
        # back — it stayed reapable forever while its WebSocket was still open.
        await self.redis.setex(self._conn_key(connection_id), PRESENCE_CONNECTION_TTL_SECONDS, "1")

    async def list_viewers(self, scope: str) -> list[dict]:
        raw = await self.redis.hgetall(self._viewers_key(scope))
        viewers: list[dict] = []
        stale: list[str] = []
        for connection_id, payload in raw.items():
            try:
                entry = json.loads(payload)
            except (TypeError, ValueError):
                stale.append(connection_id)
                continue
            if not await self.redis.exists(self._conn_key(connection_id)):
                # The owning WebSocket stopped heartbeating (crash, hard kill) — reap it.
                stale.append(connection_id)
                continue
            entry.setdefault("connection_id", connection_id)
            viewers.append(entry)
        if stale:
            await self.redis.hdel(self._viewers_key(scope), *stale)
        return viewers

    async def publish_event(self, scope: str, event: dict) -> None:
        await self.redis.publish(self._channel(scope), json.dumps(event))

    async def subscribe(
        self, scope: str, *, ready: asyncio.Event | None = None
    ) -> AsyncIterator[dict]:
        """Yield events published to ``scope``.

        ``ready`` is set once the channel subscription is established, so a caller can take
        a consistent snapshot *after* it is guaranteed to receive deltas — otherwise every
        join/leave between the snapshot and the subscription is lost. Messages that arrive
        while the caller is still assembling its snapshot are buffered by the connection and
        delivered in order once iteration begins.
        """
        pubsub = self.subscriber.pubsub()
        await pubsub.subscribe(self._channel(scope))
        if ready is not None:
            ready.set()
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                yield json.loads(message["data"])
        finally:
            await pubsub.unsubscribe(self._channel(scope))
            await pubsub.aclose()
