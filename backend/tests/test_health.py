from httpx import ASGITransport, AsyncClient

from app.api import health as health_module
from app.main import app


async def test_health_returns_ok_when_dependencies_are_ready(monkeypatch):
    async def ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "check_database", ok)
    monkeypatch.setattr(health_module, "check_redis", ok)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_health_returns_degraded_when_dependency_fails(monkeypatch):
    async def ok() -> str:
        return "ok"

    async def failing() -> str:
        raise RuntimeError("not reachable")

    monkeypatch.setattr(health_module, "check_database", ok)
    monkeypatch.setattr(health_module, "check_redis", failing)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "ok", "redis": "error"}
