"""Epic 09: a run is traceable by one request id across API, queue, worker and stream."""

from __future__ import annotations

import pytest

from app.core.middleware import sanitize_request_id


@pytest.mark.parametrize("raw", ["abc", "req-1.2_3", "a" * 64, "  trimmed  "])
def test_sanitize_request_id_keeps_well_formed_ids(raw: str) -> None:
    assert sanitize_request_id(raw) == raw.strip()


@pytest.mark.parametrize("raw", [None, "", "   ", "a" * 65, "has space", "new\nline", "é", "a;b"])
def test_sanitize_request_id_replaces_bad_ids(raw: str | None) -> None:
    generated = sanitize_request_id(raw)
    assert generated != raw
    assert len(generated) == 32
    assert sanitize_request_id(generated) == generated


# ---------------------------------------------------------------------------
# Request id → log context → run row → frames
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
from contextlib import suppress  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import structlog  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from prometheus_client import REGISTRY  # noqa: E402

from app.core.middleware import current_request_id, websocket_context  # noqa: E402
from app.models import AgentEvent, AgentRun, CommandRun  # noqa: E402
from app.services.agents import AgentRuntime, AgentService, event_payload  # noqa: E402
from app.services.commands import command_status_frame  # noqa: E402
from app.services.runtime import InMemoryEventBus  # noqa: E402
from app.worker import run_agent_job  # noqa: E402
from tests.test_job_queue import FakeDispatcher, db_session, queue_settings, seed  # noqa: E402
from tests.test_worker_jobs import make_ctx, seed_run  # noqa: E402

__all__ = ["db_session"]


def _ws_open(kind: str) -> float:
    return REGISTRY.get_sample_value("ws_connections", {"kind": kind}) or 0.0


def test_current_request_id_reads_the_bound_context() -> None:
    structlog.contextvars.clear_contextvars()
    assert current_request_id() is None
    structlog.contextvars.bind_contextvars(request_id="req-1")
    try:
        assert current_request_id() == "req-1"
    finally:
        structlog.contextvars.clear_contextvars()


def test_frames_carry_the_request_id_only_when_the_run_has_one() -> None:
    event = AgentEvent(
        id=uuid4(),
        agent_run_id=uuid4(),
        event_type="status",
        content="completed",
        event_metadata={"status": "completed"},
        created_at=datetime.now(UTC),
    )
    assert "request_id" not in event_payload(event)
    assert "request_id" not in event_payload(event, None)
    assert event_payload(event, "req-2")["request_id"] == "req-2"

    plain = CommandRun(status="completed", exit_code=0)
    assert "request_id" not in command_status_frame(plain)
    tagged = CommandRun(status="completed", exit_code=0, request_id="req-3")
    assert command_status_frame(tagged) == {
        "type": "status",
        "status": "completed",
        "exit_code": 0,
        "request_id": "req-3",
    }


def _ws_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/echo")
    @websocket_context("echo")
    async def echo(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"request_id": current_request_id(), "open": _ws_open("echo")})
        await websocket.close()

    @app.websocket("/boom")
    @websocket_context("boom")
    async def boom(websocket: WebSocket) -> None:
        await websocket.accept()
        raise RuntimeError("handler failed")

    return app


def test_websocket_context_binds_the_handshake_request_id_and_counts_the_connection() -> None:
    client = TestClient(_ws_app())
    before = _ws_open("echo")

    with client.websocket_connect("/echo", headers={"X-Request-ID": "ws-req-1"}) as socket:
        frame = socket.receive_json()
    assert frame == {"request_id": "ws-req-1", "open": before + 1}
    assert _ws_open("echo") == before

    with client.websocket_connect("/echo", headers={"X-Request-ID": "bad id!"}) as socket:
        frame = socket.receive_json()
    assert frame["request_id"] != "bad id!"
    assert len(frame["request_id"]) == 32


def test_websocket_context_releases_the_gauge_when_the_handler_raises() -> None:
    client = TestClient(_ws_app(), raise_server_exceptions=False)
    before = _ws_open("boom")
    with suppress(Exception), client.websocket_connect("/boom") as socket:
        socket.receive_json()
    assert _ws_open("boom") == before


async def test_create_run_stamps_the_bound_request_id(db_session):
    session, session_maker = db_session
    user, workspace = await seed(session)
    service = AgentService(
        session,
        session_maker,
        queue_settings(),
        runtime=AgentRuntime(bus=InMemoryEventBus()),
        dispatcher=FakeDispatcher(),
    )
    structlog.contextvars.bind_contextvars(request_id="req-create")
    try:
        run = await service.create_run(workspace.id, user.id, "hello")
    finally:
        structlog.contextvars.clear_contextvars()

    stored = await session.get(AgentRun, run.id)
    await session.refresh(stored)
    assert stored.request_id == "req-create"


async def test_worker_frames_carry_the_run_request_id(db_session):
    """API → row → worker → stream: every live frame of the run carries the creating id."""
    session, session_maker = db_session
    run = await seed_run(session, request_id="req-e2e")
    ctx = make_ctx(session_maker)
    queue = ctx["agent_runtime"].subscribe(run.id)

    assert await run_agent_job(ctx, str(run.id)) == "completed"

    frames = []
    while True:
        try:
            frames.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    assert frames, "the run published no frames"
    assert {frame.get("request_id") for frame in frames} == {"req-e2e"}
    # The job's own task context was bound; the test's context never was.
    assert current_request_id() is None
