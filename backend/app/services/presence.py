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

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

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
        await self.redis.hset(self._viewers_key(scope), connection_id, json.dumps(entry))
        await self.redis.setex(
            self._conn_key(connection_id), PRESENCE_CONNECTION_TTL_SECONDS, "1"
        )
        return entry

    async def leave(self, scope: str, user_id: UUID, connection_id: str) -> bool:
        """Drop a connection. Returns True when the user has no other live connection."""
        await self.redis.hdel(self._viewers_key(scope), connection_id)
        await self.redis.delete(self._conn_key(connection_id))
        remaining = await self.list_viewers(scope)
        return not any(viewer.get("user_id") == str(user_id) for viewer in remaining)

    async def heartbeat(self, connection_id: str) -> None:
        await self.redis.expire(self._conn_key(connection_id), PRESENCE_CONNECTION_TTL_SECONDS)

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

    async def subscribe(self, scope: str) -> AsyncIterator[dict]:
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(self._channel(scope))
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                yield json.loads(message["data"])
        finally:
            await pubsub.unsubscribe(self._channel(scope))
            await pubsub.aclose()
