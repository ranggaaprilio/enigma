from typing import Any

from redis.asyncio import Redis

from app.core.config import Settings, get_settings


def redis_client_options(settings: Settings, *, subscriber: bool = False) -> dict[str, Any]:
    """Timeouts so a dead or partitioned Redis fails a call instead of hanging it.

    ``subscriber`` clients block in ``pubsub.listen()`` for as long as a channel is quiet, which
    a read timeout would turn into an error; they rely on TCP keepalive to notice a dead peer.
    """
    return {
        "socket_timeout": None if subscriber else settings.redis_socket_timeout_seconds,
        "socket_connect_timeout": settings.redis_connect_timeout_seconds,
        "socket_keepalive": True,
        "health_check_interval": settings.redis_health_check_interval_seconds,
    }


def get_redis_client(*, subscriber: bool = False) -> Redis:
    settings = get_settings()
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        **redis_client_options(settings, subscriber=subscriber),
    )
