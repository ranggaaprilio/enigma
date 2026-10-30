"""Draining, graceful shutdown and the probes that expose them."""

import asyncio
import json
import signal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app import main as main_module
from app.api import health as health_module
from app.core.config import Settings
from app.core.lifecycle import (
    AppLifecycle,
    ServiceDrainingError,
    drain_in_flight_runs,
    install_sigterm_drain,
    lifecycle,
    service_draining_handler,
)
from app.main import app
from app.services.agents import AgentService
from app.services.commands import CommandService
from app.services.runtime import InMemoryRunRegistry


@pytest.fixture
def draining(monkeypatch):
    monkeypatch.setattr(lifecycle, "draining", True)


# --- probes ------------------------------------------------------------------------------


async def test_ready_returns_503_draining_without_probing_dependencies(monkeypatch, draining):
    async def must_not_probe() -> str:
        raise AssertionError("a draining instance must not probe its dependencies")

    monkeypatch.setattr(health_module, "check_database", must_not_probe)
    monkeypatch.setattr(health_module, "check_redis", must_not_probe)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "draining", "database": "skipped", "redis": "skipped"}


async def test_live_is_a_dependency_free_liveness_probe(monkeypatch, draining):
    async def must_not_probe() -> str:
        raise AssertionError("liveness must not probe dependencies")

    monkeypatch.setattr(health_module, "check_database", must_not_probe)
    monkeypatch.setattr(health_module, "check_redis", must_not_probe)
    monkeypatch.setattr(health_module, "check_daytona", must_not_probe)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- refusing new work -------------------------------------------------------------------


async def test_agent_create_run_refuses_while_draining(draining):
    service = AgentService(None, None, None)  # type: ignore[arg-type]
    with pytest.raises(ServiceDrainingError):
        await service.create_run(uuid4(), uuid4(), "prompt")


async def test_command_create_run_refuses_while_draining(draining):
    service = CommandService(None, None, None)  # type: ignore[arg-type]
    with pytest.raises(ServiceDrainingError):
        await service.create_run(uuid4(), uuid4(), uuid4(), confirmed=True)


async def test_draining_error_maps_to_503_with_retry_after():
    response = await service_draining_handler(None, ServiceDrainingError())  # type: ignore[arg-type]

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert json.loads(response.body) == {
        "detail": "The server is restarting. Please retry in a few seconds."
    }
    assert app.exception_handlers[ServiceDrainingError] is service_draining_handler


# --- draining in-flight runs ---------------------------------------------------------------


async def test_drain_waits_for_runs_that_finish_within_the_grace_window():
    registry = InMemoryRunRegistry()
    finished = []

    async def quick() -> None:
        await asyncio.sleep(0.01)
        finished.append(True)

    registry.register(uuid4(), asyncio.create_task(quick()))

    assert await drain_in_flight_runs(registry, grace_seconds=1) == 0
    assert finished == [True]


async def test_drain_cancels_runs_still_going_after_the_grace_window():
    registry = InMemoryRunRegistry()
    stuck = asyncio.create_task(asyncio.Event().wait())
    registry.register(uuid4(), stuck)

    assert await drain_in_flight_runs(registry, grace_seconds=0.01) == 1
    assert stuck.cancelled()


async def test_drain_with_nothing_in_flight_returns_immediately():
    assert await drain_in_flight_runs(InMemoryRunRegistry(), grace_seconds=60) == 0


class _CancellingSessionMaker:
    """A session factory whose first query is interrupted, as a shutdown cancel would."""

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        raise asyncio.CancelledError


async def test_agent_run_cancelled_while_draining_is_left_for_the_reconciliation_sweep(
    monkeypatch, draining
):
    stopped = []

    async def record_stop(self, run_id):
        stopped.append(run_id)

    monkeypatch.setattr(AgentService, "_mark_stopped", record_stop)
    service = AgentService(None, _CancellingSessionMaker(), None)  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        await service._execute_run(uuid4(), asyncio.Event())
    # Not "stopped" -- the shutdown sweep records it as interrupted_by_restart instead.
    assert stopped == []


async def test_agent_run_cancelled_outside_a_drain_is_still_marked_stopped(monkeypatch):
    stopped = []

    async def record_stop(self, run_id):
        stopped.append(run_id)

    monkeypatch.setattr(AgentService, "_mark_stopped", record_stop)
    service = AgentService(None, _CancellingSessionMaker(), None)  # type: ignore[arg-type]
    run_id = uuid4()

    await service._execute_run(run_id, asyncio.Event())
    assert stopped == [run_id]


# --- SIGTERM -------------------------------------------------------------------------------


async def test_sigterm_flips_draining_then_hands_over_to_the_server_after_the_delay():
    forwarded: list[int] = []
    handed_over = asyncio.Event()

    def server_handler(sig, frame):
        forwarded.append(sig)
        handed_over.set()

    original = signal.signal(signal.SIGTERM, server_handler)
    previous = signal.getsignal(signal.SIGTERM)
    state = AppLifecycle()
    restore = install_sigterm_drain(asyncio.get_running_loop(), 0.05, state)
    try:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        assert state.draining  # flipped inside the handler itself: /ready is 503 at once
        assert forwarded == []  # still serving so the LB can stop routing
        await asyncio.wait_for(handed_over.wait(), timeout=5)
        assert forwarded == [signal.SIGTERM]

        restore()
        assert signal.getsignal(signal.SIGTERM) is previous
    finally:
        restore()
        signal.signal(signal.SIGTERM, original)


async def test_a_second_sigterm_skips_the_remaining_delay():
    forwarded: list[int] = []
    original = signal.signal(signal.SIGTERM, lambda sig, frame: forwarded.append(sig))
    restore = install_sigterm_drain(asyncio.get_running_loop(), 60, AppLifecycle())
    try:
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)
        for _ in range(3):
            await asyncio.sleep(0)

        assert forwarded == [signal.SIGTERM]
    finally:
        restore()
        signal.signal(signal.SIGTERM, original)


async def test_sigterm_interposer_is_a_no_op_off_the_main_thread():
    """signal.signal raises outside the main thread (e.g. an app served by TestClient)."""
    loop = asyncio.get_running_loop()
    before = signal.getsignal(signal.SIGTERM)

    restore = await asyncio.to_thread(install_sigterm_drain, loop, 5, AppLifecycle())
    restore()

    assert signal.getsignal(signal.SIGTERM) is before


# --- lifespan wiring -----------------------------------------------------------------------


async def test_lifespan_reconciles_on_startup_and_drains_in_order_on_shutdown(monkeypatch):
    calls: list[str] = []

    owned = [uuid4()]

    async def reconcile(session_maker, run_ids=None):
        calls.append("reconcile" if run_ids is None else f"reconcile:{sorted(run_ids)}")

    class FakeRegistry:
        def active_run_ids(self):
            return owned

    async def drain(registry, grace_seconds):
        calls.append(f"drain:{grace_seconds}")
        return 0

    class FakeDaytona:
        async def close(self):
            calls.append("daytona.close")

    class FakeEngine:
        async def dispose(self):
            calls.append("engine.dispose")

    def install(loop, delay_seconds, state=None):
        calls.append(f"sigterm:{delay_seconds}")
        return lambda: calls.append("sigterm.restore")

    monkeypatch.setattr(main_module, "reconcile_interrupted_runs", reconcile)
    monkeypatch.setattr(main_module, "drain_in_flight_runs", drain)
    monkeypatch.setattr(main_module, "close_all", FakeDaytona().close)
    monkeypatch.setattr(main_module, "engine", FakeEngine())
    monkeypatch.setattr(main_module, "install_sigterm_drain", install)
    monkeypatch.setattr(main_module, "run_registry", FakeRegistry())
    monkeypatch.setattr(lifecycle, "draining", False)

    async with main_module.lifespan(app):
        calls.append("serving")
        assert not lifecycle.draining

    assert lifecycle.draining
    assert calls == [
        "reconcile",
        "sigterm:5.0",
        "serving",
        "drain:15.0",
        f"reconcile:{owned}",
        "daytona.close",
        "engine.dispose",
        "sigterm.restore",
    ]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("start_workflow", (uuid4(), uuid4(), "Feature", "desc")),
        ("run_phase", (uuid4(), uuid4(), "specify", uuid4())),
        ("request_changes", (uuid4(), uuid4(), "specify", uuid4(), "notes")),
        ("submit_analyze_answers", (uuid4(), uuid4(), uuid4(), [])),
    ],
)
async def test_spec_entry_points_refuse_before_scaffolding_while_draining(
    draining, method, args
):
    """Refused up front, not at the inner create_run, so a drain never leaves a workflow
    half-scaffolded (worktree created, phase never started)."""
    from app.services.speckit import SpecKitService

    service = SpecKitService(None, None, None, None)  # type: ignore[arg-type]
    with pytest.raises(ServiceDrainingError):
        await getattr(service, method)(*args)


async def test_lifespan_still_closes_pools_when_the_drain_fails(monkeypatch):
    closed: list[str] = []

    async def reconcile(session_maker, run_ids=None):
        return None

    async def broken_drain(registry, grace_seconds):
        raise RuntimeError("database went away")

    class FakeDaytona:
        async def close(self):
            closed.append("daytona")

    class FakeEngine:
        async def dispose(self):
            closed.append("engine")

    monkeypatch.setattr(main_module, "reconcile_interrupted_runs", reconcile)
    monkeypatch.setattr(main_module, "drain_in_flight_runs", broken_drain)
    monkeypatch.setattr(main_module, "close_all", FakeDaytona().close)
    monkeypatch.setattr(main_module, "engine", FakeEngine())
    monkeypatch.setattr(
        main_module,
        "install_sigterm_drain",
        lambda loop, delay, state=None: lambda: closed.append("sigterm"),
    )
    monkeypatch.setattr(lifecycle, "draining", False)

    async with main_module.lifespan(app):
        pass

    assert closed == ["daytona", "engine", "sigterm"]



async def test_queue_mode_api_does_not_sweep_runs_it_does_not_own(monkeypatch):
    """With runs on workers, an API restart must not mark the workers' live runs interrupted."""
    calls: list[str] = []

    async def reconcile(session_maker, run_ids=None):
        calls.append("reconcile" if run_ids is None else f"reconcile:{sorted(run_ids)}")

    class FakeRegistry:
        def active_run_ids(self):
            return []

    async def drain(registry, grace_seconds):
        return 0

    class FakeDaytona:
        async def close(self):
            return None

    class FakeEngine:
        async def dispose(self):
            return None

    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(run_execution_mode="queue"))
    monkeypatch.setattr(main_module, "reconcile_interrupted_runs", reconcile)
    monkeypatch.setattr(main_module, "drain_in_flight_runs", drain)
    monkeypatch.setattr(main_module, "close_all", FakeDaytona().close)
    monkeypatch.setattr(main_module, "engine", FakeEngine())
    monkeypatch.setattr(main_module, "install_sigterm_drain", lambda *a, **k: lambda: None)
    monkeypatch.setattr(main_module, "run_registry", FakeRegistry())
    monkeypatch.setattr(lifecycle, "draining", False)

    async with main_module.lifespan(app):
        pass

    assert "reconcile" not in calls
