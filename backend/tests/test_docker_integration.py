"""Docker provider against a real daemon (Docker or Podman) and the built sandbox image.

Run with a daemon up and ``./sandbox/build-local-image.sh`` done:

    DOCKER_INTEGRATION_TESTS=1 uv run pytest tests/test_docker_integration.py

Covers the local-docker-runtime Epic 02 acceptance criteria that need a real container:
lifecycle, exec/stream parity, timeout and cancellation kill, auto-start, a command run
through ``CommandService`` and an agent ``run_command`` on a docker workspace, and no
leaked containers.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.models import User, Workspace
from app.services.agent_tools import WorkspaceTools
from app.services.commands import CommandRuntime, CommandService
from app.services.sandbox import CommandTimeoutError
from app.services.sandbox.docker import MANAGED_LABEL, WORKSPACE_LABEL, DockerRuntime
from tests.test_commands import real_db_session  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.getenv("DOCKER_INTEGRATION_TESTS") != "1",
    reason="Set DOCKER_INTEGRATION_TESTS=1 to run against a Docker/Podman daemon",
)

PS_SLEEPERS = "ps -eo args | grep -E '^sleep 3[0-9]{2}$' || echo none"


def _workspace(owner_id=None) -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=owner_id or uuid4(),
        name="Docker integration",
        slug=f"docker-it-{uuid4().hex[:8]}",
        repo_url=None,
        default_branch="main",
        runtime="docker",
        workspace_path="workspace",
        status="creating",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
async def runtime():
    runtime = DockerRuntime(Settings(docker_sandbox_memory_mb=1024, docker_sandbox_cpus=1))
    if await runtime.health() != "ok":
        pytest.fail("Docker daemon is not reachable")
    yield runtime
    await runtime.close()


@pytest.fixture
async def sandbox(runtime):
    workspace = _workspace()
    workspace.sandbox_id = await runtime.provision(workspace)
    yield workspace
    await runtime.delete(workspace)
    client, _ = await runtime._clients()
    leaked = await asyncio.to_thread(
        client.containers.list,
        all=True,
        filters={"label": [f"{MANAGED_LABEL}=1", f"{WORKSPACE_LABEL}={workspace.id}"]},
    )
    assert leaked == []


async def test_lifecycle_exec_stream_and_auto_start(runtime, sandbox) -> None:
    assert await runtime.provision(sandbox) == sandbox.sandbox_id  # idempotent by name
    assert await runtime.status(sandbox) == "ready"

    result = await runtime.exec(sandbox, "echo hello; echo oops >&2; pwd; exit 4")
    assert result.exit_code == 4
    # Combined like Daytona's: everything in stdout, stderr empty. stdout and stderr are
    # separate pipes, so their relative interleaving is not guaranteed; each stays ordered.
    assert result.stderr == ""
    assert sorted(result.stdout.splitlines()) == ["/home/daytona/workspace", "hello", "oops"]
    assert result.stdout.index("hello") < result.stdout.index("/home/daytona/workspace")

    tools = await runtime.exec(
        sandbox, "for b in mise code-server specify tmux git ssh; do command -v $b; done"
    )
    assert tools.exit_code == 0, tools.stdout

    out: list[str] = []
    err: list[str] = []

    async def on_out(text: str) -> None:
        out.append(text)

    async def on_err(text: str) -> None:
        err.append(text)

    code = await runtime.stream_command(
        sandbox,
        "it-stream",
        "for i in 1 2 3; do echo line$i; sleep 0.2; done; "
        "printf '\\342\\202'; sleep 0.2; printf '\\254\\n'; echo bad >&2; exit 2",
        cwd=None,
        env={"GREETING": "hi there"},
        timeout=30,
        on_stdout=on_out,
        on_stderr=on_err,
    )
    assert code == 2
    assert "".join(out) == "line1\nline2\nline3\n€\n"
    assert "".join(err) == "bad\n"

    client, _ = await runtime._clients()
    container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)
    await asyncio.to_thread(container.stop, timeout=1)
    assert await runtime.status(sandbox) == "stopped"
    assert (await runtime.exec(sandbox, "echo back")).stdout == "back\n"  # auto-started
    assert await runtime.status(sandbox) == "ready"


async def test_timeout_and_cancellation_leave_no_process_behind(runtime, sandbox) -> None:
    with pytest.raises(CommandTimeoutError):
        await runtime.exec(sandbox, "sleep 301 & sleep 302", timeout=2)
    assert (await runtime.exec(sandbox, PS_SLEEPERS)).stdout == "none\n"

    async def sink(text: str) -> None:
        return None

    with pytest.raises(CommandTimeoutError):
        await runtime.stream_command(
            sandbox,
            "it-timeout",
            "echo start; sleep 303",
            cwd=None,
            env=None,
            timeout=2,
            on_stdout=sink,
            on_stderr=sink,
        )
    assert (await runtime.exec(sandbox, PS_SLEEPERS)).stdout == "none\n"

    task = asyncio.create_task(runtime.exec(sandbox, "sleep 304 & sleep 305"))
    await asyncio.sleep(1.5)
    loop = asyncio.get_running_loop()
    cancelled_at = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The killer exits as soon as the group is gone instead of always waiting the grace.
    assert loop.time() - cancelled_at < 3
    assert (await runtime.exec(sandbox, PS_SLEEPERS)).stdout == "none\n"


async def test_sandbox_floor_is_enforced_on_the_real_container(runtime, sandbox) -> None:
    whoami = await runtime.exec(
        sandbox, "id -un; sudo -n true 2>/dev/null && echo ROOT || echo no-root"
    )
    assert whoami.stdout == "daytona\nno-root\n"

    client, _ = await runtime._clients()
    container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)
    host_config = container.attrs["HostConfig"]
    assert container.attrs["Mounts"] == []
    assert not host_config.get("Privileged")
    assert host_config.get("PidsLimit") == 4096
    effective = await runtime.exec(sandbox, "grep -E '^Cap(Eff|Bnd):' /proc/self/status")
    assert effective.stdout.split() == ["CapEff:", "0" * 16, "CapBnd:", "0" * 16]
    assert list(container.attrs["NetworkSettings"]["Networks"]) == ["enigma-sandboxes"]


async def test_agent_run_command_executes_in_the_container(runtime, sandbox) -> None:
    tools = WorkspaceTools(
        sandbox, runtime=runtime, command_timeout_seconds=30, max_command_output_chars=20_000
    )

    result = await tools.run_command("echo hello")

    assert (result.exit_code, result.stdout) == (0, "hello\n")


async def test_command_run_streams_and_completes_on_docker(runtime, real_db_session) -> None:  # noqa: F811
    session, session_maker = real_db_session
    user = User(
        id=uuid4(),
        email=f"docker-it-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = _workspace(user.id)
    workspace.sandbox_id = await runtime.provision(workspace)
    workspace.status = "ready"
    session.add(user)
    session.add(workspace)
    await session.flush()
    try:
        service = CommandService(
            session=session,
            session_maker=session_maker,
            settings=get_settings(),
            runtime=CommandRuntime(),
            sandbox_runtime=runtime,
        )
        command = await service.create_command(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Multi-line",
            command='echo "one"; echo "two" >&2; echo "$ENIGMA_WORKSPACE_ID"; exit 0',
            description=None,
            icon=None,
            hotkey=None,
            requires_confirmation=False,
        )
        run = await service.create_run(workspace.id, command.id, user.id, confirmed=True)
        task = service.runtime.get_task(run.id)
        assert task is not None
        await task

        await session.refresh(run)
        assert run.status == "completed" and run.exit_code == 0
        assert "one\n" in run.output and "two\n" in run.output
        assert str(workspace.id) in run.output
    finally:
        await runtime.delete(workspace)
