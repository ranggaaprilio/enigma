from httpx import ASGITransport, AsyncClient

from app.api import health as health_module
from app.main import app


async def test_health_returns_ok_when_dependencies_are_ready(monkeypatch):
    async def ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "check_database", ok)
    monkeypatch.setattr(health_module, "check_redis", ok)
    monkeypatch.setattr(health_module, "check_daytona", ok)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
        "daytona": "ok",
    }


async def test_health_returns_degraded_when_dependency_fails(monkeypatch):
    async def ok() -> str:
        return "ok"

    async def failing() -> str:
        raise RuntimeError("not reachable")

    monkeypatch.setattr(health_module, "check_database", ok)
    monkeypatch.setattr(health_module, "check_redis", failing)
    monkeypatch.setattr(health_module, "check_daytona", ok)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "database": "ok",
        "redis": "error",
        "daytona": "ok",
    }


async def test_ready_returns_ready_when_postgres_and_redis_are_up(monkeypatch):
    async def ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "check_database", ok)
    monkeypatch.setattr(health_module, "check_redis", ok)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "redis": "ok"}


async def test_ready_returns_503_when_a_dependency_is_down(monkeypatch):
    async def ok() -> str:
        return "ok"

    async def failing() -> str:
        raise RuntimeError("not reachable")

    monkeypatch.setattr(health_module, "check_database", failing)
    monkeypatch.setattr(health_module, "check_redis", ok)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "error", "redis": "ok"}


async def test_metrics_endpoint_exposes_prometheus_text():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    # Prometheus exposition format is text with HELP/TYPE comment lines.
    assert "# HELP" in response.text or "# TYPE" in response.text


async def test_requests_carry_a_correlation_id_header(monkeypatch):
    async def ok() -> str:
        return "ok"

    monkeypatch.setattr(health_module, "check_database", ok)
    monkeypatch.setattr(health_module, "check_redis", ok)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A generated id is echoed back...
        generated = await client.get("/ready")
        assert generated.headers.get("X-Request-ID")

        # ...and a supplied id is propagated verbatim.
        supplied = await client.get("/ready", headers={"X-Request-ID": "trace-123"})
        assert supplied.headers.get("X-Request-ID") == "trace-123"
