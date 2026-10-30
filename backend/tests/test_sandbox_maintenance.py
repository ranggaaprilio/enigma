"""Docker sandbox upkeep (local-docker-runtime Epic 05): idle auto-stop, orphan cleanup."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models import User, Workspace
from app.services.sandbox_maintenance import (
    isolate_legacy_sandboxes,
    reconcile_orphans,
    stop_idle_sandboxes,
)
from tests.sandbox.fakes import FakeDockerClient, docker_runtime_with


@pytest.fixture
async def maker():
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    created: list[Workspace] = []
    maker.created = created  # type: ignore[attr-defined]
    yield maker
    async with maker() as session:
        for workspace in created:
            row = await session.get(Workspace, workspace.id)
            if row is not None:
                await session.delete(row)
                await session.flush()
            user = await session.get(User, workspace.owner_id)
            if user is not None:
                await session.delete(user)
        await session.commit()
    await engine.dispose()


async def _workspace(maker, *, runtime: str = "docker", sandbox_id: str | None = None,
                     idle_minutes: int | None = None) -> Workspace:
    async with maker() as session:
        user = User(id=uuid4(), email=f"{uuid4().hex}@example.com", password_hash="x",
                    role="owner", is_active=True)
        workspace = Workspace(id=uuid4(), owner_id=user.id, name="Upkeep", slug=uuid4().hex[:12],
                              default_branch="main", runtime=runtime, status="ready",
                              workspace_path="workspace", daytona_sandbox_id=sandbox_id)
        session.add(user)
        await session.flush()
        session.add(workspace)
        await session.flush()
        if idle_minutes is not None:
            await session.execute(
                text(
                    "INSERT INTO sandbox_activity (workspace_id, last_activity_at) "
                    "VALUES (:id, clock_timestamp() - make_interval(mins => :m))"
                ),
                {"id": workspace.id, "m": idle_minutes},
            )
        await session.commit()
    maker.created.append(workspace)
    return workspace


async def _sandboxed(maker, client, runtime, **kwargs) -> Workspace:
    workspace = await _workspace(maker, **kwargs)
    sandbox_id = await runtime.provision(workspace)
    async with maker() as session:
        row = await session.get(Workspace, workspace.id)
        row.daytona_sandbox_id = sandbox_id
        await session.commit()
    workspace.sandbox_id = sandbox_id
    return workspace


# --- idle auto-stop --------------------------------------------------------------------------


async def test_an_idle_sandbox_is_stopped_and_a_busy_one_left_running(maker) -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client, sandbox_auto_stop_minutes=30)
    idle = await _sandboxed(maker, client, runtime, idle_minutes=45)
    busy = await _sandboxed(maker, client, runtime, idle_minutes=5)

    stopped = await stop_idle_sandboxes(maker, runtime.settings, runtime)

    assert stopped == 1
    assert client.containers.get(idle.sandbox_id).status == "exited"
    assert client.containers.get(busy.sandbox_id).status == "running"
    # Kept, not removed: the next use starts it again (Daytona's auto-stop parity).
    await runtime.exec(idle, "true")
    assert client.containers.get(idle.sandbox_id).status == "running"


async def test_only_restricts_the_idle_pass_to_the_given_workspaces(maker) -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client, sandbox_auto_stop_minutes=30)
    chosen = await _sandboxed(maker, client, runtime, idle_minutes=45)
    other = await _sandboxed(maker, client, runtime, idle_minutes=45)

    assert await stop_idle_sandboxes(maker, runtime.settings, runtime, only={chosen.id}) == 1
    assert client.containers.get(chosen.sandbox_id).status == "exited"
    assert client.containers.get(other.sandbox_id).status == "running"


async def test_an_already_stopped_sandbox_is_not_stopped_again(maker) -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client, sandbox_auto_stop_minutes=30)
    idle = await _sandboxed(maker, client, runtime, idle_minutes=45)
    container = client.containers.get(idle.sandbox_id)
    container.status = "exited"

    assert await stop_idle_sandboxes(maker, runtime.settings, runtime) == 0
    assert container.stop_calls == 0


async def test_a_sandbox_running_a_long_quiet_command_is_not_stopped(maker) -> None:
    """Activity is stamped when a command starts; a 40-minute build that prints nothing
    must not have its sandbox stopped under it."""
    client = FakeDockerClient()
    runtime = docker_runtime_with(client, sandbox_auto_stop_minutes=30)
    idle = await _sandboxed(maker, client, runtime, idle_minutes=45)
    client.containers.get(idle.sandbox_id).busy = True

    assert await stop_idle_sandboxes(maker, runtime.settings, runtime) == 0
    assert client.containers.get(idle.sandbox_id).status == "running"


async def test_busy_is_not_a_licence_to_run_forever(maker) -> None:
    """The busy probe reads pidfiles in the tenant's own /tmp, so a tenant could claim
    "busy" for ever; past SANDBOX_BUSY_MAX_MINUTES of idleness it is stopped anyway."""
    client = FakeDockerClient()
    runtime = docker_runtime_with(
        client, sandbox_auto_stop_minutes=30, sandbox_busy_max_minutes=120
    )
    spared = await _sandboxed(maker, client, runtime, idle_minutes=60)
    capped = await _sandboxed(maker, client, runtime, idle_minutes=180)
    for workspace in (spared, capped):
        client.containers.get(workspace.sandbox_id).busy = True

    assert await stop_idle_sandboxes(maker, runtime.settings, runtime) == 1
    assert client.containers.get(spared.sandbox_id).status == "running"
    assert client.containers.get(capped.sandbox_id).status == "exited"


async def test_daytona_workspaces_and_a_zero_window_are_left_alone(maker) -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client, sandbox_auto_stop_minutes=30)
    await _workspace(maker, runtime="daytona", sandbox_id="daytona-1", idle_minutes=600)
    idle = await _sandboxed(maker, client, runtime, idle_minutes=600)

    disabled = docker_runtime_with(client, sandbox_auto_stop_minutes=0)
    assert await stop_idle_sandboxes(maker, disabled.settings, disabled) == 0
    assert client.containers.get(idle.sandbox_id).status == "running"


# --- orphans -----------------------------------------------------------------------------


async def test_orphans_are_removed_and_lost_containers_flagged(maker) -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client)
    kept = await _sandboxed(maker, client, runtime)
    # A sandbox whose workspace row is gone (a crash between delete steps).
    ghost = Workspace(id=uuid4(), owner_id=uuid4(), workspace_path="workspace")
    ghost_container = await runtime.provision(ghost)
    # A workspace whose container vanished out of band.
    lost = await _workspace(maker, sandbox_id="ctr-vanished")
    # Not ours: no enigma.managed label, must survive.
    decoy = client.add_container("someone-elses-postgres", labels={"app": "db"})

    report = await reconcile_orphans(maker, runtime, only={kept.id, ghost.id, lost.id})

    assert report == {"orphans_removed": 1, "sandboxes_lost": 1}
    assert ghost_container not in client.containers.by_id
    assert f"enigma-sbx-{ghost.id.hex}" not in client.networks.names
    assert client.containers.get(kept.sandbox_id).status == "running"
    assert decoy.id in client.containers.by_id
    async with maker() as session:
        assert (await session.get(Workspace, lost.id)).status == "error"
        assert (await session.get(Workspace, kept.id)).status == "ready"


# --- when the loop runs ------------------------------------------------------------------


async def test_upkeep_runs_only_on_a_docker_profile(monkeypatch) -> None:
    """The orphan sweep removes managed containers without a workspace row, so it must not
    start in a Daytona-default process (the test suite's app included)."""
    import asyncio

    from app.core.config import Settings
    from app.services import sandbox_maintenance

    started: list[object] = []

    async def fake_loop(*args) -> None:
        started.append(args)

    monkeypatch.setattr(sandbox_maintenance, "run_sandbox_maintenance", fake_loop)
    monkeypatch.setattr(sandbox_maintenance.DockerRuntime, "configured", property(lambda _: True))

    assert sandbox_maintenance.start_sandbox_maintenance(
        None, Settings(sandbox_runtime_default="daytona")  # type: ignore[arg-type]
    ) is None
    task = sandbox_maintenance.start_sandbox_maintenance(
        None, Settings(sandbox_runtime_default="docker")  # type: ignore[arg-type]
    )
    assert task is not None
    await asyncio.wait_for(task, 1)
    assert len(started) == 1

    # A mixed install (Daytona default, Docker chosen per workspace) says so explicitly.
    task = sandbox_maintenance.start_sandbox_maintenance(
        None, Settings(sandbox_runtime_default="daytona", docker_upkeep=True)  # type: ignore[arg-type]
    )
    assert task is not None
    await asyncio.wait_for(task, 1)
    assert len(started) == 2
    assert sandbox_maintenance.start_sandbox_maintenance(
        None, Settings(sandbox_runtime_default="docker", docker_upkeep=False)  # type: ignore[arg-type]
    ) is None


async def test_each_upkeep_pass_records_when_it_finished(maker, monkeypatch) -> None:
    """/health reports this (Epic 06): a stuck or crashed loop shows as a stale timestamp."""
    import asyncio

    from app.services import sandbox_maintenance

    async def nothing(*args, **kwargs):
        return {}

    for name in ("reconcile_orphans", "isolate_legacy_sandboxes", "stop_idle_sandboxes"):
        monkeypatch.setattr(sandbox_maintenance, name, nothing)
    runtime = docker_runtime_with(FakeDockerClient())
    assert runtime.last_upkeep_at is None

    task = asyncio.create_task(
        sandbox_maintenance.run_sandbox_maintenance(maker, runtime.settings, runtime)
    )
    try:
        for _ in range(100):
            if runtime.last_upkeep_at is not None:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert runtime.last_upkeep_at is not None


# --- sandboxes from before installation labels -------------------------------------------


async def test_a_legacy_sandbox_of_ours_is_moved_off_the_shared_network(maker) -> None:
    """Container labels are immutable, so a pre-Epic-05 sandbox never gets the
    installation label; it is still isolated when our database names it as a sandbox --
    and one this database does not know is left alone (it may be another stack's)."""
    client = FakeDockerClient(networks={"enigma-sandboxes"})
    runtime = docker_runtime_with(client)
    shared = client.networks.get("enigma-sandboxes")
    ours = await _workspace(maker)
    ours_ctr = client.add_container(
        f"execute-{ours.id.hex}",
        labels={"enigma.managed": "1", "enigma.workspace-id": str(ours.id)},
    )
    shared.connect(ours_ctr)
    async with maker() as session:
        row = await session.get(Workspace, ours.id)
        row.daytona_sandbox_id = ours_ctr.id
        await session.commit()
    stranger_id = uuid4()
    stranger_ctr = client.add_container(
        f"execute-{stranger_id.hex}",
        labels={"enigma.managed": "1", "enigma.workspace-id": str(stranger_id)},
    )
    shared.connect(stranger_ctr)

    assert await isolate_legacy_sandboxes(maker, runtime, only={ours.id, stranger_id}) == 1

    assert ours_ctr.networks == {f"enigma-sbx-{ours.id.hex}"}
    assert stranger_ctr.networks == {"enigma-sandboxes"}
