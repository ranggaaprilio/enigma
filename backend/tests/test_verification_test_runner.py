"""Test runner adapter: execute a test command and record a ``VerificationEvidence``.

Exercises ``run_tests`` against a fake sandbox runtime (no live sandbox) and a real Postgres
for the evidence persistence, mirroring the round-trip test in ``test_verification_models.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, VerificationEvidence, VerificationRun, Workspace
from app.services.sandbox.base import (
    CommandTimeoutError,
    RuntimeCommandResult,
    SandboxRuntimeError,
)
from app.services.verification.test_runner import run_tests
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
        run = VerificationRun(workspace_id=workspace.id, plan_ref="spec:1", status="testing")
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


def _exit_handler(command: str) -> RuntimeCommandResult:
    if command == "fail":
        return RuntimeCommandResult(exit_code=3, stdout="", stderr="boom")
    return RuntimeCommandResult(exit_code=0, stdout="ok", stderr="")


class _RaisingRuntime:
    """A minimal fake runtime whose ``exec`` always raises the configured error."""

    provider = "fake"

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        raise self._error


async def test_run_tests_maps_exit_codes(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = FakeSandboxRuntime(exec_handler=_exit_handler)
    try:
        async with maker() as session:
            passed = await run_tests(workspace, run, "echo hi", runtime=runtime, session=session)
            failed = await run_tests(workspace, run, "fail", runtime=runtime, session=session)

            assert passed.kind == "test"
            assert passed.status == "passed"
            assert passed.detail == {"exit_code": 0, "command": "echo hi"}
            assert passed.run_id == run.id

            assert failed.kind == "test"
            assert failed.status == "failed"
            assert failed.detail == {"exit_code": 3, "command": "fail"}
            assert failed.run_id == run.id

            # Both rows were persisted, not just returned.
            rows = (
                await session.execute(
                    select(VerificationEvidence).where(VerificationEvidence.run_id == run.id)
                )
            ).scalars().all()
        assert {ev.status for ev in rows} == {"passed", "failed"}
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_run_tests_timeout_maps_to_error(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = _RaisingRuntime(CommandTimeoutError("timed out"))
    try:
        async with maker() as session:
            ev = await run_tests(workspace, run, "sleep 100", timeout=1, runtime=runtime,
                                 session=session)

            assert ev.kind == "test"
            assert ev.status == "error"
            assert ev.detail == {"command": "sleep 100", "timeout": 1, "error": "timed out"}
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_run_tests_sandbox_error_maps_to_error(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = _RaisingRuntime(SandboxRuntimeError("sandbox vanished"))
    try:
        async with maker() as session:
            ev = await run_tests(workspace, run, "pytest", runtime=runtime, session=session)

            assert ev.kind == "test"
            assert ev.status == "error"
            assert ev.detail == {"command": "pytest", "error": "sandbox vanished"}
    finally:
        await _cleanup(maker, workspace, user_id)
