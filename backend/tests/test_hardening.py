import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import (
    _DEFAULT_AUTH_SECRET_KEY,
    _DEFAULT_ENV_ENCRYPTION_KEY,
    Settings,
)
from app.main import app
from app.services import rate_limit as rate_limit_module
from app.services.rate_limit import RedisRateLimiter

REAL_SECRET = "a-real-production-secret-value"
REAL_ENCRYPTION_KEY = "another-real-encryption-key-value"


def _clear_secret_env(monkeypatch) -> None:
    for name in ("AUTH_SECRET_KEY", "WORKSPACE_ENV_ENCRYPTION_KEY", "AUTH_COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)


def test_local_environment_allows_default_secrets(monkeypatch):
    _clear_secret_env(monkeypatch)
    settings = Settings(environment="local", _env_file=None)
    assert settings.auth_secret_key == _DEFAULT_AUTH_SECRET_KEY
    assert settings.workspace_env_encryption_key == _DEFAULT_ENV_ENCRYPTION_KEY


def test_non_local_environment_rejects_default_auth_secret(monkeypatch):
    _clear_secret_env(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        Settings(
            environment="production",
            workspace_env_encryption_key=REAL_ENCRYPTION_KEY,
            _env_file=None,
        )
    assert "auth_secret_key" in str(exc.value)


def test_non_local_environment_rejects_default_encryption_key(monkeypatch):
    _clear_secret_env(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        Settings(environment="production", auth_secret_key=REAL_SECRET, _env_file=None)
    assert "workspace_env_encryption_key" in str(exc.value)


def test_non_local_environment_with_real_secrets_forces_secure_cookies(monkeypatch):
    _clear_secret_env(monkeypatch)
    settings = Settings(
        environment="production",
        auth_secret_key=REAL_SECRET,
        workspace_env_encryption_key=REAL_ENCRYPTION_KEY,
        auth_cookie_secure=False,
        _env_file=None,
    )
    assert settings.auth_cookie_secure is True


class FakeRedis:
    """Minimal in-memory stand-in for the async Redis client used by the rate limiter."""

    def __init__(self, store: dict[str, int]) -> None:
        self.store = store

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None:
        return None


async def test_redis_rate_limiter_allows_up_to_limit_then_429s(monkeypatch):
    store: dict[str, int] = {}
    monkeypatch.setattr(rate_limit_module, "get_redis_client", lambda: FakeRedis(store))

    limiter = RedisRateLimiter(prefix="agent:run", scope="agent_run", attempts=2, window_seconds=60)

    await limiter.check("user:workspace")  # attempt 1
    await limiter.check("user:workspace")  # attempt 2

    with pytest.raises(HTTPException) as exc:
        await limiter.check("user:workspace")  # attempt 3 exceeds the limit
    assert exc.value.status_code == 429


async def test_redis_rate_limiter_keys_are_isolated_per_identity(monkeypatch):
    store: dict[str, int] = {}
    monkeypatch.setattr(rate_limit_module, "get_redis_client", lambda: FakeRedis(store))

    limiter = RedisRateLimiter(prefix="agent:run", scope="agent_run", attempts=1, window_seconds=60)

    await limiter.check("user-a:ws")  # fine
    await limiter.check("user-b:ws")  # different identity, also fine

    with pytest.raises(HTTPException):
        await limiter.check("user-a:ws")  # user-a's second call is rejected


async def test_unhandled_exception_still_carries_request_id():
    """An exception unhandled by a route must still produce a response with X-Request-ID.

    Without app.main's generic Exception handler, this would propagate past
    RequestContextMiddleware and reach the client with no correlation id at all.
    """

    async def _boom():
        raise RuntimeError("boom")

    app.add_api_route("/__test_boom", _boom, methods=["GET"])
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/__test_boom")
        assert response.status_code == 500
        assert response.headers.get("X-Request-ID")
    finally:
        app.router.routes = [
            route for route in app.router.routes if getattr(route, "path", None) != "/__test_boom"
        ]
