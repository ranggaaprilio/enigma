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

REAL_SECRET = "a-real-production-secret-value-of-sufficient-length"
# A well-formed Fernet key (32 url-safe base64 bytes) that is not the shipped default.
REAL_ENCRYPTION_KEY = "q0pn5-5JpCUzSIqhCv4UbIi4_0wqQJ3vAfUZz1i2h9E="
REAL_METRICS_TOKEN = "a-real-metrics-scrape-token"


def _clear_secret_env(monkeypatch) -> None:
    for name in (
        "AUTH_SECRET_KEY",
        "WORKSPACE_ENV_ENCRYPTION_KEY",
        "AUTH_COOKIE_SECURE",
        "METRICS_TOKEN",
    ):
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


def test_non_local_environment_rejects_unset_metrics_token(monkeypatch):
    _clear_secret_env(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        Settings(
            environment="production",
            auth_secret_key=REAL_SECRET,
            workspace_env_encryption_key=REAL_ENCRYPTION_KEY,
            _env_file=None,
        )
    assert "metrics_token" in str(exc.value)


def test_non_local_environment_with_real_secrets_forces_secure_cookies(monkeypatch):
    _clear_secret_env(monkeypatch)
    settings = Settings(
        environment="production",
        auth_secret_key=REAL_SECRET,
        workspace_env_encryption_key=REAL_ENCRYPTION_KEY,
        metrics_token=REAL_METRICS_TOKEN,
        auth_cookie_secure=False,
        _env_file=None,
    )
    assert settings.auth_cookie_secure is True


def test_malformed_encryption_key_is_rejected_even_locally(monkeypatch):
    """A bad key would otherwise only surface on the first encrypt/decrypt, long after boot."""
    _clear_secret_env(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        Settings(
            environment="local",
            workspace_env_encryption_key="not-a-fernet-key",
            _env_file=None,
        )
    assert "workspace_env_encryption_key" in str(exc.value)
    assert "Fernet" in str(exc.value)


def test_non_local_environment_rejects_short_auth_secret(monkeypatch):
    _clear_secret_env(monkeypatch)
    with pytest.raises(ValidationError) as exc:
        Settings(
            environment="production",
            auth_secret_key="too-short",
            workspace_env_encryption_key=REAL_ENCRYPTION_KEY,
            metrics_token=REAL_METRICS_TOKEN,
            _env_file=None,
        )
    assert "auth_secret_key" in str(exc.value)
    assert "32" in str(exc.value)


def test_local_environment_allows_short_auth_secret(monkeypatch):
    _clear_secret_env(monkeypatch)
    settings = Settings(environment="local", auth_secret_key="secret", _env_file=None)
    assert settings.auth_secret_key == "secret"


class FakeRedis:
    """Minimal in-memory stand-in for the async Redis client used by the rate limiter.

    ``pipeline`` mirrors redis-py's async context manager: commands are queued and applied
    in order on ``execute``, which is how the limiter keeps INCR and EXPIRE atomic.
    """

    def __init__(self, store: dict[str, int]) -> None:
        self.store = store
        self.ttls: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int, nx: bool = False) -> bool:
        if nx and key in self.ttls:
            return False
        self.ttls[key] = ttl
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)
        self.ttls.pop(key, None)

    def pipeline(self, transaction: bool = True) -> "FakePipeline":
        return FakePipeline(self)

    async def aclose(self) -> None:
        return None


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.queued: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        return None

    def incr(self, key: str) -> "FakePipeline":
        self.queued.append(("incr", (key,), {}))
        return self

    def expire(self, key: str, ttl: int, nx: bool = False) -> "FakePipeline":
        self.queued.append(("expire", (key, ttl), {"nx": nx}))
        return self

    async def execute(self) -> list:
        results = []
        for name, args, kwargs in self.queued:
            results.append(await getattr(self.redis, name)(*args, **kwargs))
        self.queued.clear()
        return results


async def test_redis_rate_limiter_allows_up_to_limit_then_429s(monkeypatch):
    store: dict[str, int] = {}
    monkeypatch.setattr(rate_limit_module, "get_redis_client", lambda: FakeRedis(store))

    limiter = RedisRateLimiter(prefix="agent:run", scope="agent_run", attempts=2, window_seconds=60)

    await limiter.check("user:workspace")  # attempt 1
    await limiter.check("user:workspace")  # attempt 2

    with pytest.raises(HTTPException) as exc:
        await limiter.check("user:workspace")  # attempt 3 exceeds the limit
    assert exc.value.status_code == 429


async def test_redis_rate_limiter_sets_the_window_ttl_once(monkeypatch):
    """The window must be stamped on the first hit and never pushed out by later ones."""
    store: dict[str, int] = {}
    redis = FakeRedis(store)
    monkeypatch.setattr(rate_limit_module, "get_redis_client", lambda: redis)

    limiter = RedisRateLimiter(
        prefix="agent:run", scope="agent_run", attempts=5, window_seconds=60
    )
    await limiter.check("user:workspace")
    key = next(iter(redis.ttls))
    redis.ttls[key] = 12  # pretend the window has been counting down

    await limiter.check("user:workspace")

    assert redis.ttls[key] == 12  # EXPIRE ... NX left the in-flight window alone


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


def test_settings_validation_errors_never_echo_secret_values(monkeypatch):
    """pydantic appends `input_value=...` to validation errors; a near-miss production key (or the
    tail of the whole settings dict, for model-level errors) would land in crash-loop logs."""
    _clear_secret_env(monkeypatch)
    with pytest.raises(ValidationError) as field_error:
        Settings(workspace_env_encryption_key="SECRETVALUE123", _env_file=None)
    assert "SECRETVALUE123" not in str(field_error.value)

    with pytest.raises(ValidationError) as model_error:
        Settings(
            environment="production",
            auth_secret_key="short-SECRET",
            workspace_env_encryption_key=REAL_ENCRYPTION_KEY,
            metrics_token="TOKEN-SECRET-VALUE",
            _env_file=None,
        )
    message = str(model_error.value)
    assert "short-SECRET" not in message
    assert REAL_ENCRYPTION_KEY[-8:] not in message
    assert "TOKEN-SECRET-VALUE" not in message
