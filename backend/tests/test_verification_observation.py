"""Observation interface: capture behavioral evidence of a running app.

Exercises ``observe`` against a fake sandbox runtime (no live sandbox) and an ``httpx``
mock transport (no live preview server), with a real Postgres for the evidence
persistence — mirroring ``test_verification_test_runner.py``.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, VerificationEvidence, VerificationRun, Workspace
from app.services.sandbox.base import SandboxRuntimeError
from app.services.verification.observation import observe
from tests.sandbox.fakes import FakeSandboxRuntime


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(maker):
    """A persisted user + workspace + verification run the adapter can attach evidence to."""
    async with maker() as session:
        user = User(
            id=uuid4(),
            email=f"{uuid4().hex}@example.com",
            password_hash="x",
            role="owner",
            is_active=True,
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name="Verify",
            slug=uuid4().hex[:12],
            default_branch="main",
            runtime="docker",
            status="ready",
            workspace_path="workspace",
            sandbox_id=uuid4().hex,
        )
        # Flush the workspace before the run: ``VerificationRun`` has no ORM relationship to
        # ``Workspace`` (only the raw FK), so SQLAlchemy cannot order the inserts without it.
        session.add(user)
        await session.flush()
        session.add(workspace)
        await session.flush()
        run = VerificationRun(workspace_id=workspace.id, plan_ref="spec:1", status="observing")
        session.add(run)
        await session.commit()
        return workspace, run, user.id


async def _cleanup(maker, workspace, user_id) -> None:
    async with maker() as session:
        row = await session.get(Workspace, workspace.id)
        user = await session.get(User, user_id)
        await session.delete(row)  # cascades to verification_runs -> verification_evidence
        await session.flush()
        await session.delete(user)
        await session.commit()


class _RaisingPreviewRuntime:
    """A minimal fake runtime whose ``preview_link`` always raises the configured error."""

    provider = "fake"

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def expose_port(self, workspace, port) -> None:
        return None

    async def preview_link(self, workspace, port):
        raise self._error


def _html_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        text="<html><head><title>Demo App</title></head><body>hello world</body></html>",
        headers={"content-type": "text/html"},
    )


def _connect_error_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


async def test_observe_records_snapshot_and_screenshot(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = FakeSandboxRuntime()
    transport = httpx.MockTransport(_html_handler)
    try:
        async with maker() as session:
            ev = await observe(
                workspace, run, 5173, runtime=runtime, session=session, transport=transport
            )

            assert ev.kind == "observation"
            assert ev.status == "passed"
            assert "snapshot" in ev.detail
            assert "screenshot" in ev.detail
            assert ev.detail["snapshot"]["status_code"] == 200
            assert ev.detail["snapshot"]["title"] == "Demo App"
            assert ev.detail["description"]
            assert ev.detail["screenshot"]
            assert ev.path == ev.detail["screenshot"]
            assert ev.run_id == run.id
            # expose_port was called best-effort before the preview link was fetched.
            assert 5173 in runtime.exposed_ports

            # The row was persisted, not just returned.
            rows = (
                await session.execute(
                    select(VerificationEvidence).where(VerificationEvidence.run_id == run.id)
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].kind == "observation"
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_observe_preview_failure_does_not_raise(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = _RaisingPreviewRuntime(SandboxRuntimeError("preview unavailable"))
    try:
        async with maker() as session:
            ev = await observe(workspace, run, 5173, runtime=runtime, session=session)

            assert ev.kind == "observation"
            assert ev.status == "observation_failed"
            assert ev.detail["error"] == "preview unavailable"
            assert ev.run_id == run.id
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_observe_fetch_failure_records_observation_failed(maker) -> None:
    """A preview link that resolves but an app that is not reachable is also not a raise."""
    workspace, run, user_id = await _seed(maker)
    runtime = FakeSandboxRuntime()
    transport = httpx.MockTransport(_connect_error_handler)
    try:
        async with maker() as session:
            ev = await observe(
                workspace, run, 5173, runtime=runtime, session=session, transport=transport
            )

            assert ev.kind == "observation"
            assert ev.status == "observation_failed"
            assert "error" in ev.detail
    finally:
        await _cleanup(maker, workspace, user_id)
