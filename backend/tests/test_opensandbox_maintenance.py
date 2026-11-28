"""Plan 7a Task 5: OpenSandbox upkeep -- scoped orphan sweep, idle pause, resume on use."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.installation import INSTALLATION_LABEL
from app.services.sandbox.opensandbox import WORKSPACE_LABEL
from app.services.sandbox_maintenance import (
    pause_idle_opensandboxes,
    reconcile_opensandbox_orphans,
)
from tests.sandbox.fakes import opensandbox_runtime_with
from tests.upkeep_fixtures import _workspace, maker  # noqa: F401 (fixture)


def _ours(sandbox_id: str, workspace_id: str, installation: str = "test-installation"):
    return SimpleNamespace(
        id=sandbox_id,
        metadata={INSTALLATION_LABEL: installation, WORKSPACE_LABEL: workspace_id},
    )


def _ws(sandbox_id: str = "sandbox-1") -> Workspace:
    return Workspace(
        id=uuid4(), sandbox_id=sandbox_id, workspace_path="workspace", runtime="opensandbox"
    )


# --- the runtime ------------------------------------------------------------------------


async def test_only_this_installations_sandboxes_are_ever_listed() -> None:
    runtime = opensandbox_runtime_with()
    manager = runtime._fake_manager
    ids = [str(uuid4()) for _ in range(3)]
    manager.sandboxes = [_ours(f"s{i}", w) for i, w in enumerate(ids)] + [
        _ours("theirs", str(uuid4()), installation="another-installation")
    ]

    found = await runtime.managed_sandboxes()

    assert found == [(f"s{i}", w) for i, w in enumerate(ids)]  # every page, ours only
    assert manager.filters and all(
        (f.metadata or {}).get(INSTALLATION_LABEL) == "test-installation" for f in manager.filters
    )


async def test_pause_if_running_pauses_only_a_running_sandbox() -> None:
    runtime = opensandbox_runtime_with()
    manager = runtime._fake_manager
    workspace = _ws()

    assert await runtime.pause_if_running(workspace) is True
    assert await runtime.pause_if_running(workspace) is False
    assert manager.paused == ["sandbox-1"]


async def test_start_resumes_a_paused_sandbox() -> None:
    runtime = opensandbox_runtime_with(state="Paused")

    await runtime.start(_ws())

    assert runtime._fake_manager.resumed == ["sandbox-1"]


async def test_using_a_paused_sandbox_wakes_it_once() -> None:
    runtime = opensandbox_runtime_with(state="Paused")
    workspace = _ws()

    await runtime.exec(workspace, "true")
    await runtime.exec(workspace, "true")

    assert runtime._fake_manager.resumed == ["sandbox-1"]


async def test_execs_and_terminals_count_as_activity() -> None:
    runtime = opensandbox_runtime_with()
    workspace = _ws()

    async def on_data(_: bytes) -> None:
        return None

    await runtime.exec(workspace, "true")
    await runtime.create_or_connect_pty(workspace, "t1", on_data, env=None, cols=80, rows=24)

    assert runtime.activity.touched.count(workspace.id) >= 2


async def test_a_long_command_keeps_its_sandbox_busy(monkeypatch) -> None:
    from app.services.sandbox import opensandbox as module

    monkeypatch.setattr(module, "ACTIVITY_HEARTBEAT_SECONDS", 0.01)
    runtime = opensandbox_runtime_with()
    workspace = _ws()

    async with runtime._busy(workspace):
        await asyncio.sleep(0.06)

    assert runtime.activity.touched.count(workspace.id) >= 3


# --- the upkeep passes (database) ------------------------------------------------------------


class UpkeepRuntime:
    """What the passes need from the runtime; records what they did."""

    def __init__(self, managed: list[tuple[str, str]], running: set[str] | None = None) -> None:
        self.managed = managed
        self.running = running or set()
        self.removed: list[str] = []
        self.paused: list[UUID] = []

    async def managed_sandboxes(self) -> list[tuple[str, str]]:
        return self.managed

    async def remove_sandbox(self, sandbox_id: str) -> None:
        self.removed.append(sandbox_id)

    async def pause_if_running(self, workspace: Workspace) -> bool:
        self.paused.append(workspace.id)
        return True


async def test_orphans_of_this_installation_are_removed(maker) -> None:  # noqa: F811
    kept = await _workspace(maker, runtime="opensandbox", sandbox_id="sbx-kept")
    gone = uuid4()
    runtime = UpkeepRuntime(
        [("sbx-kept", str(kept.id)), ("sbx-orphan", str(gone)), ("sbx-unlabelled", "")]
    )

    removed = await reconcile_opensandbox_orphans(maker, runtime, only={kept.id, gone})

    assert removed == 1
    assert runtime.removed == ["sbx-orphan"]  # never a kept one, never a guess


async def test_idle_opensandboxes_are_paused_and_busy_ones_left(maker) -> None:  # noqa: F811
    idle = await _workspace(maker, runtime="opensandbox", sandbox_id="sbx-idle", idle_minutes=120)
    busy = await _workspace(maker, runtime="opensandbox", sandbox_id="sbx-busy", idle_minutes=1)
    other = await _workspace(maker, runtime="daytona", sandbox_id="dtn", idle_minutes=120)
    runtime = UpkeepRuntime([])
    settings = Settings(sandbox_auto_stop_minutes=30)

    paused = await pause_idle_opensandboxes(
        maker, settings, runtime, only={idle.id, busy.id, other.id}
    )

    assert paused == 1
    assert runtime.paused == [idle.id]


async def test_opensandbox_upkeep_runs_only_where_it_is_wanted(monkeypatch) -> None:
    from app.services import sandbox_maintenance
    from app.services.sandbox.opensandbox import OpenSandboxRuntime

    started: list[object] = []

    async def fake_loop(*args) -> None:
        started.append(args)

    monkeypatch.setattr(sandbox_maintenance, "run_opensandbox_maintenance", fake_loop)
    monkeypatch.setattr(OpenSandboxRuntime, "configured", property(lambda _: True))
    start = sandbox_maintenance.start_opensandbox_maintenance

    assert start(None, Settings(sandbox_runtime_default="daytona")) is None  # type: ignore[arg-type]
    task = start(None, Settings(sandbox_runtime_default="opensandbox"))  # type: ignore[arg-type]
    assert task is not None
    await asyncio.wait_for(task, 1)
    task = start(None, Settings(sandbox_runtime_default="daytona", opensandbox_upkeep=True))  # type: ignore[arg-type]
    assert task is not None
    await asyncio.wait_for(task, 1)
    off = Settings(sandbox_runtime_default="opensandbox", opensandbox_upkeep=False)
    assert start(None, off) is None  # type: ignore[arg-type]
    assert len(started) == 2
