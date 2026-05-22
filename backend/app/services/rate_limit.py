import hashlib

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.services.redis import get_redis_client


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host


class RedisLoginRateLimiter:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _key(self, email: str, request: Request) -> str:
        fingerprint = hashlib.sha256(f"{email}|{_client_ip(request)}".encode()).hexdigest()
        return f"auth:login:{fingerprint}"

    async def check(self, email: str, request: Request) -> None:
        redis = get_redis_client()
        try:
            key = self._key(email, request)
            attempts = await redis.incr(key)
            if attempts == 1:
                await redis.expire(key, self.settings.auth_login_rate_limit_window_seconds)

            if attempts > self.settings.auth_login_rate_limit_attempts:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many login attempts. Please try again later.",
                )
        finally:
            await redis.aclose()

    async def reset(self, email: str, request: Request) -> None:
        redis = get_redis_client()
        try:
            await redis.delete(self._key(email, request))
        finally:
            await redis.aclose()
