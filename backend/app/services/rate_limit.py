import hashlib

from fastapi import HTTPException, Request, status
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.lifecycle import RETRY_AFTER_SECONDS
from app.core.metrics import rate_limit_rejections_total
from app.services.redis import get_redis_client


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host


async def _register_attempt(key: str, window_seconds: int) -> tuple[int, int]:
    """Increment the fixed-window counter for ``key``, setting its expiry on the first hit.

    Returns the current attempt count within the window and the seconds left in it.
    """
    redis = get_redis_client()
    try:
        # One round trip, one transaction. Incrementing and then expiring as separate
        # commands leaves a TTL-less counter behind if the process dies (or the connection
        # drops) in between — the key then never resets and the caller is 429'd forever.
        # `EXPIRE ... NX` sets the window only when the key has no TTL yet, so a sustained
        # burst cannot keep pushing the window out either.
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            pipe.ttl(key)
            attempts, _, ttl = await pipe.execute()
        return int(attempts), max(int(ttl), 1)
    finally:
        await redis.aclose()


async def _reset_key(key: str) -> None:
    redis = get_redis_client()
    try:
        await redis.delete(key)
    finally:
        await redis.aclose()


class RedisRateLimiter:
    """Fixed-window rate limiter keyed by an arbitrary identity string.

    Generalises the login limiter's INCR/EXPIRE/429 pattern so any expensive endpoint can be
    throttled per user/workspace/etc. Raises ``429`` (with ``Retry-After`` = seconds left in
    the window) once ``attempts`` is exceeded and records a ``rate_limit_rejections_total{scope}``
    metric. It guards spend-bearing actions, so it fails closed: a Redis error is a ``503``.
    """

    def __init__(self, *, prefix: str, scope: str, attempts: int, window_seconds: int) -> None:
        self.prefix = prefix
        self.scope = scope
        self.attempts = attempts
        self.window_seconds = window_seconds

    def _key(self, identity: str) -> str:
        fingerprint = hashlib.sha256(identity.encode()).hexdigest()
        return f"{self.prefix}:{fingerprint}"

    async def check(self, identity: str) -> None:
        try:
            attempts, retry_after = await _register_attempt(
                self._key(identity), self.window_seconds
            )
        except RedisError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Rate limiting is temporarily unavailable. Please retry shortly.",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            ) from error
        if attempts > self.attempts:
            rate_limit_rejections_total.labels(self.scope).inc()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": str(retry_after)},
            )

    async def reset(self, identity: str) -> None:
        await _reset_key(self._key(identity))


class RedisLoginRateLimiter:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _key(self, email: str, request: Request) -> str:
        fingerprint = hashlib.sha256(f"{email}|{_client_ip(request)}".encode()).hexdigest()
        return f"auth:login:{fingerprint}"

    async def check(self, email: str, request: Request) -> None:
        attempts, _ = await _register_attempt(
            self._key(email, request),
            self.settings.auth_login_rate_limit_window_seconds,
        )
        if attempts > self.settings.auth_login_rate_limit_attempts:
            rate_limit_rejections_total.labels("auth_login").inc()
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts. Please try again later.",
            )

    async def reset(self, email: str, request: Request) -> None:
        await _reset_key(self._key(email, request))
