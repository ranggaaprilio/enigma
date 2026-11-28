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
            runtime="opensandbox",
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


# --- Plan 6: a real screenshot, stored in the artifact store -------------------------------

from app.models import VerificationArtifact  # noqa: E402
from app.services.artifact_store import ArtifactStoreError  # noqa: E402
from app.services.sandbox.base import RuntimeCommandResult  # noqa: E402
from app.services.verification.screenshot import PNG_SIGNATURE  # noqa: E402
from tests.sandbox.fakes import FakeFileSystem  # noqa: E402

SHOT_DIR = "/tmp/tmp.Shot01"
PNG = PNG_SIGNATURE + b"pixels"


class MemoryStore:
    def __init__(self, fail: bool = False) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail = fail

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail:
            raise ArtifactStoreError("artifact store put failed")
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def _shooting_runtime(*, produces_png: bool = True) -> FakeSandboxRuntime:
    def handler(command: str) -> RuntimeCommandResult:
        if "chromium" in command:
            size = len(PNG) if produces_png else 0
            return RuntimeCommandResult(exit_code=0, stdout=f"{SHOT_DIR}\n{size}\n", stderr="")
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")

    files = {f"{SHOT_DIR}/shot.png": PNG} if produces_png else {}
    return FakeSandboxRuntime(filesystem=FakeFileSystem(files=files), exec_handler=handler)


def _chromium_ran(runtime: FakeSandboxRuntime) -> bool:
    return any("chromium" in call["command"] for call in runtime.exec_calls)


async def _artifacts(maker, evidence_id):
    async with maker() as session:
        return (
            await session.scalars(
                select(VerificationArtifact).where(VerificationArtifact.evidence_id == evidence_id)
            )
        ).all()


async def test_observe_stores_a_real_screenshot(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime, store = _shooting_runtime(), MemoryStore()
    try:
        async with maker() as session:
            ev = await observe(
                workspace,
                run,
                5173,
                runtime=runtime,
                session=session,
                transport=httpx.MockTransport(_html_handler),
                store=store,
            )
        assert ev.status == "passed"
        assert ev.detail["screenshot_captured"] is True
        key = f"verification/{workspace.id}/{run.id}/{ev.id}.png"
        assert store.objects[key] == (PNG, "image/png")
        [artifact] = await _artifacts(maker, ev.id)
        assert (artifact.storage_key, artifact.content_type, artifact.byte_size) == (
            key,
            "image/png",
            len(PNG),
        )
    finally:
        await _cleanup(maker, workspace, user_id)


@pytest.mark.parametrize(
    ("produces_png", "store_fails", "reason"),
    [(False, False, "no screenshot"), (True, True, "could not be stored")],
)
async def test_a_failed_screenshot_never_fails_the_observation(
    maker, produces_png, store_fails, reason
) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime, store = _shooting_runtime(produces_png=produces_png), MemoryStore(fail=store_fails)
    try:
        async with maker() as session:
            ev = await observe(
                workspace,
                run,
                5173,
                runtime=runtime,
                session=session,
                transport=httpx.MockTransport(_html_handler),
                store=store,
            )
        assert ev.status == "passed"  # the snapshot still counts
        assert ev.detail["screenshot_captured"] is False
        assert reason in ev.detail["screenshot_error"]
        assert await _artifacts(maker, ev.id) == []
        assert store.objects == {}
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_without_a_store_no_screenshot_is_attempted(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = _shooting_runtime()
    try:
        async with maker() as session:
            ev = await observe(
                workspace,
                run,
                5173,
                runtime=runtime,
                session=session,
                transport=httpx.MockTransport(_html_handler),
            )
        assert ev.detail["screenshot_captured"] is False
        assert ev.detail["screenshot_error"] == "artifact storage is not configured"
        assert not _chromium_ran(runtime)
    finally:
        await _cleanup(maker, workspace, user_id)


async def test_a_failed_snapshot_takes_no_screenshot(maker) -> None:
    workspace, run, user_id = await _seed(maker)
    runtime = _shooting_runtime()
    try:
        async with maker() as session:
            ev = await observe(
                workspace,
                run,
                5173,
                runtime=runtime,
                session=session,
                transport=httpx.MockTransport(_connect_error_handler),
                store=MemoryStore(),
            )
        assert ev.status == "observation_failed"
        assert not _chromium_ran(runtime)
    finally:
        await _cleanup(maker, workspace, user_id)
