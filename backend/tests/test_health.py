from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import health as health_module
from app.core.config import Settings
from app.main import app
from app.services.sandbox.availability import RuntimeAvailability


async def _ok() -> str:
    return "ok"


async def _failing() -> str:
    raise RuntimeError("not reachable")


def _providers(**statuses: str):
    async def availability(provider: str) -> RuntimeAvailability:
        status = statuses.get(provider, "unconfigured")
        return RuntimeAvailability(
            provider,
            status != "unconfigured",
            status,  # type: ignore[arg-type]
            None if status == "ok" else f"{provider} is {status}",
        )

    return availability


@pytest.fixture
def probes(monkeypatch):
    app.state.runtime_health = None  # the per-app probe cache
    monkeypatch.setattr(
        health_module,
        "get_settings",
        lambda: Settings(sandbox_runtime_default="daytona", metrics_token=None),
    )
    monkeypatch.setattr(health_module, "check_database", _ok)
    monkeypatch.setattr(health_module, "check_redis", _ok)

    async def three() -> int:
        return 3

    monkeypatch.setattr(health_module, "count_managed_containers", three)
    monkeypatch.setattr(health_module, "upkeep_last_pass_at", lambda: None)

    def set_providers(**statuses: str) -> None:
        app.state.runtime_health = None
        monkeypatch.setattr(health_module, "runtime_availability", _providers(**statuses))

    yield set_providers
    app.state.runtime_health = None


async def _health(headers: dict | None = None) -> tuple[int, dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", headers=headers)
    return response.status_code, response.json()


@pytest.mark.parametrize(
    ("daytona", "docker", "code", "overall"),
    [
        ("ok", "ok", 200, "ok"),
        ("unconfigured", "ok", 200, "ok"),  # the no-subscription self-host profile
        ("ok", "unconfigured", 200, "ok"),  # a Daytona-only install, as before the series
        ("unconfigured", "unconfigured", 503, "degraded"),  # nowhere to put a workspace
    ],
)
async def test_health_reports_each_provider_in_every_configured_combination(
    probes, daytona, docker, code, overall
):
    probes(daytona=daytona, docker=docker)

    status_code, body = await _health()

    assert status_code == code
    assert body["status"] == overall
    assert body["daytona"] == daytona  # the field existing monitors read
    assert body["sandbox_runtime_default"] == "daytona"
    assert body["runtimes"]["daytona"]["status"] == daytona
    assert body["runtimes"]["daytona"]["configured"] is (daytona != "unconfigured")
    assert body["runtimes"]["docker"]["status"] == docker
    assert body["runtimes"]["docker"]["managed_containers"] == (3 if docker == "ok" else None)
    assert body["runtimes"]["daytona"]["managed_containers"] is None


async def test_a_configured_provider_that_is_down_degrades_health(probes):
    probes(daytona="ok", docker="error")

    status_code, body = await _health()

    assert status_code == 503 and body["status"] == "degraded"
    assert body["runtimes"]["docker"] == {
        "configured": True,
        "status": "error",
        "reason": "docker is error",
        "managed_containers": None,
        "upkeep_last_pass_at": None,
    }


async def test_health_reports_when_the_docker_upkeep_last_ran(probes, monkeypatch):
    probes(docker="ok")
    ran = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(health_module, "upkeep_last_pass_at", lambda: ran)

    _, body = await _health()

    assert body["runtimes"]["docker"]["upkeep_last_pass_at"] == "2026-10-30T12:00:00Z"


async def test_health_returns_degraded_when_dependency_fails(probes, monkeypatch):
    probes(daytona="ok")
    monkeypatch.setattr(health_module, "check_redis", _failing)

    status_code, body = await _health()

    assert status_code == 503
    assert (body["status"], body["database"], body["redis"]) == ("degraded", "ok", "error")


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


async def test_operational_detail_needs_the_metrics_token_when_one_is_set(probes, monkeypatch):
    """/health is unauthenticated: the sandbox count and upkeep timing are for operators."""
    probes(docker="ok")
    ran = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(health_module, "upkeep_last_pass_at", lambda: ran)
    monkeypatch.setattr(
        health_module, "get_settings", lambda: Settings(metrics_token="scrape-secret")
    )

    _, anonymous = await _health()
    _, operator = await _health({"authorization": "Bearer scrape-secret"})

    assert anonymous["runtimes"]["docker"]["status"] == "ok"
    assert anonymous["runtimes"]["docker"]["managed_containers"] is None
    assert anonymous["runtimes"]["docker"]["upkeep_last_pass_at"] is None
    assert operator["runtimes"]["docker"]["managed_containers"] == 3
    assert operator["runtimes"]["docker"]["upkeep_last_pass_at"] == "2026-10-30T12:00:00Z"


async def test_provider_probes_are_shared_for_a_few_seconds(probes, monkeypatch):
    """An anonymous flood of /health must not become a flood of daemon and Daytona calls."""
    probes(daytona="ok", docker="ok")
    calls: list[str] = []
    inner = health_module.runtime_availability

    async def counting(provider: str):
        calls.append(provider)
        return await inner(provider)

    monkeypatch.setattr(health_module, "runtime_availability", counting)

    for _ in range(5):
        status_code, _ = await _health()
        assert status_code == 200

    assert sorted(calls) == ["daytona", "docker"]  # one probe per provider, not five


async def test_a_disabled_provider_is_not_a_fault(probes):
    probes(daytona="ok", docker="disabled")

    status_code, body = await _health()

    assert status_code == 200 and body["status"] == "ok"
    assert body["runtimes"]["docker"]["status"] == "disabled"
    assert body["runtimes"]["docker"]["managed_containers"] is None
