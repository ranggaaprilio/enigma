"""Docker provider specifics: container spec, lifecycle, exec wrapping, timeouts, streaming.

The provider-neutral contract is in ``tests/sandbox/test_conformance.py``; behaviour against
a real daemon is in ``tests/test_docker_integration.py`` (``DOCKER_INTEGRATION_TESTS=1``).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
import requests
from docker.errors import APIError

from app.services.sandbox import (
    CommandTimeoutError,
    ProvisioningError,
    RuntimeCommandResult,
    RuntimeUnavailableError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from app.services.sandbox import docker as docker_module
from app.services.sandbox.docker import assert_safe_container_spec, map_docker_error
from tests.sandbox.fakes import (
    DockerExecResult,
    FakeDockerClient,
    docker_runtime_with,
    make_workspace,
)


def docker_workspace(sandbox_id: str | None = None):
    workspace = make_workspace(sandbox_id, runtime="docker")
    workspace.workspace_path = "workspace"
    workspace.snapshot = None
    return workspace


async def until(predicate, timeout: float = 5.0) -> None:
    """Bounded wait, so a regression fails the test instead of hanging the suite."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def provisioned(client: FakeDockerClient, **settings):
    runtime = docker_runtime_with(client, **settings)
    workspace = docker_workspace()
    workspace.sandbox_id = await runtime.provision(workspace)
    return runtime, workspace


# --- provision ---------------------------------------------------------------------------


async def test_provision_runs_a_labelled_locked_down_container() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(
        client, docker_sandbox_cpus=1.5, docker_sandbox_memory_mb=2048
    )

    [spec] = client.containers.run_specs
    assert spec["name"] == f"execute-{workspace.id.hex}"
    assert spec["image"] == "enigma-sandbox:latest"
    assert spec["labels"] == {
        "enigma.managed": "1",
        "enigma.workspace-id": str(workspace.id),
        "enigma.owner-id": str(workspace.owner_id),
    }
    assert spec["network"] == "enigma-sandboxes"
    assert spec["entrypoint"] == ["sleep", "infinity"] and spec["init"] is True
    assert spec["user"] == "daytona"
    assert spec["security_opt"] == ["no-new-privileges:true"]
    assert spec["cap_drop"] == ["ALL"] and spec["pids_limit"] == 4096
    assert (spec["mem_limit"], spec["nano_cpus"]) == ("2048m", 1_500_000_000)
    for forbidden in ("volumes", "mounts", "privileged", "cap_add", "network_mode"):
        assert forbidden not in spec
    container = client.containers.get(workspace.sandbox_id)
    # Parity with Daytona's create_folder(workspace_path): relative to the sandbox home.
    assert container.exec_runs == [["mkdir", "-p", "--", "/home/daytona/workspace"]]


async def test_provision_ignores_the_user_settable_snapshot() -> None:
    """``workspace.snapshot`` is a Daytona snapshot name any owner can set; on Docker it
    must not select an arbitrary local image."""
    client = FakeDockerClient(images={"enigma-sandbox:latest", "someone-elses:image"})
    runtime = docker_runtime_with(client)
    workspace = docker_workspace()
    workspace.snapshot = "someone-elses:image"

    await runtime.provision(workspace)

    assert client.containers.run_specs[0]["image"] == "enigma-sandbox:latest"


async def test_provision_is_idempotent_by_name_and_starts_a_stopped_container() -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client)
    workspace = docker_workspace()
    existing = client.add_container(
        f"execute-{workspace.id.hex}",
        status="exited",
        labels={"enigma.managed": "1", "enigma.workspace-id": str(workspace.id)},
    )

    assert await runtime.provision(workspace) == existing.id
    assert client.containers.run_specs == []
    assert existing.status == "running"
    # Re-created on adopt, so a sandbox whose first provision died half-way is repaired.
    assert existing.exec_runs == [["mkdir", "-p", "--", "/home/daytona/workspace"]]


async def test_provision_refuses_to_adopt_a_container_it_did_not_create() -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client)
    workspace = docker_workspace()
    client.add_container(f"execute-{workspace.id.hex}", labels={"enigma.managed": "1"})

    with pytest.raises(ProvisioningError, match="not created by Enigma"):
        await runtime.provision(workspace)


async def test_a_failed_workspace_dir_removes_the_new_container() -> None:
    client = FakeDockerClient()
    client.fail_mkdir = True
    runtime = docker_runtime_with(client)

    with pytest.raises(ProvisioningError, match="workspace directory"):
        await runtime.provision(docker_workspace())

    assert client.containers.by_id == {}  # nothing half-made left to adopt


async def test_missing_image_tells_the_operator_how_to_build_it() -> None:
    runtime = docker_runtime_with(FakeDockerClient(images=set()))

    with pytest.raises(ProvisioningError, match="sandbox/build-local-image.sh"):
        await runtime.provision(docker_workspace())


async def test_provision_creates_the_sandbox_network_only_when_missing() -> None:
    missing = FakeDockerClient(networks=set())
    await provisioned(missing)
    assert missing.networks.created == ["enigma-sandboxes"]

    present = FakeDockerClient()
    await provisioned(present)
    assert present.networks.created == []

    # Docker's name filter matches substrings: a look-alike must not count as present.
    lookalike = FakeDockerClient(networks={"enigma-sandboxes-old"})
    await provisioned(lookalike)
    assert lookalike.networks.created == ["enigma-sandboxes"]


async def test_daemon_down_is_runtime_unavailable_not_a_crash() -> None:
    runtime = docker_runtime_with(FakeDockerClient())
    runtime._client = runtime._exec_api = None

    def refuse():
        raise requests.exceptions.ConnectionError("unix:///var/run/docker.sock refused")

    runtime._connect = refuse  # type: ignore[method-assign]

    with pytest.raises(RuntimeUnavailableError) as excinfo:
        await runtime.provision(docker_workspace())
    assert "sock" not in str(excinfo.value)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"volumes": {"/var/run/docker.sock": {"bind": "/var/run/docker.sock"}}},
        {"mounts": ["anything"]},
        {"privileged": True},
        {"cap_add": ["SYS_ADMIN"]},
        {"network_mode": "host"},
        {"pid_mode": "host"},
        {"user": "root"},
        {"user": "0:0"},
        {"user": "00"},
        {"user": " root "},
        {"user": ""},
        {"security_opt": []},
        {"security_opt": ["no-new-privileges:true", "seccomp=unconfined"]},
        {"network": "host"},
        {"network": "none"},
        {"network": "container:other"},
        {"cap_drop": []},
        {"pids_limit": None},
    ],
)
def test_unsafe_container_specs_are_refused(unsafe) -> None:
    runtime = docker_runtime_with(FakeDockerClient())
    spec = runtime._container_spec(docker_workspace(), "execute-x")
    assert_safe_container_spec(spec)  # the real spec passes

    with pytest.raises(ProvisioningError, match="unsafe"):
        assert_safe_container_spec({**spec, **unsafe})


# --- lifecycle ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", "ready"),
        ("created", "creating"),
        ("restarting", "creating"),
        ("paused", "stopped"),
        ("exited", "stopped"),
        ("dead", "error"),
        ("removing", "error"),
    ],
)
async def test_status_maps_docker_states(state: str, expected: str) -> None:
    client = FakeDockerClient()
    container = client.add_container("execute-a", status=state)
    runtime = docker_runtime_with(client)

    assert await runtime.status(docker_workspace(container.id)) == expected
    assert container.start_calls == 0  # status never starts a sandbox


async def test_removed_container_is_sandbox_not_found() -> None:
    runtime = docker_runtime_with(FakeDockerClient())

    with pytest.raises(SandboxNotFoundError):
        await runtime.status(docker_workspace("gone"))


async def test_any_operation_auto_starts_a_stopped_container() -> None:
    client = FakeDockerClient()
    container = client.add_container("execute-a", status="exited")
    runtime = docker_runtime_with(client)

    await runtime.exec(docker_workspace(container.id), "true")

    assert container.status == "running" and container.start_calls == 1


async def test_delete_removes_the_container_and_tolerates_it_being_gone() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    container = client.containers.get(workspace.sandbox_id)

    await runtime.delete(workspace)
    await runtime.delete(workspace)
    await runtime.delete(docker_workspace(None))

    assert container.removed is True


# --- exec --------------------------------------------------------------------------------


async def test_exec_wraps_the_command_in_a_killable_process_group() -> None:
    client = FakeDockerClient(exec_handler=lambda cmd: RuntimeCommandResult(0, "out\n", "err\n"))
    runtime, workspace = await provisioned(client)

    result = await runtime.exec(workspace, "make test", env={"A": "1"}, timeout=30)

    [record] = client.api.execs.values()
    shell, flag, script, pidfile, command = record["cmd"]
    assert (shell, flag, command) == ("/bin/sh", "-c", "make test")
    assert "timeout -k 5 30 /bin/sh -c" in script and pidfile.endswith(".pid")
    assert record["workdir"] == "/home/daytona/workspace"
    assert record["environment"] == ["A=1"]
    # Daytona's shape: combined output in stdout, stderr always empty.
    assert result == RuntimeCommandResult(exit_code=0, stdout="out\nerr\n", stderr="")


async def test_exec_without_timeout_disables_the_in_container_limit() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)

    await runtime.exec(workspace, "true", cwd="/abs/dir")

    [record] = client.api.execs.values()
    assert "timeout -k 5 0 " in record["cmd"][2]
    assert record["workdir"] == "/abs/dir"


async def test_exec_rejects_malformed_env_names() -> None:
    runtime, workspace = await provisioned(FakeDockerClient())

    with pytest.raises(ValueError, match="invalid environment variable name"):
        await runtime.exec(workspace, "true", env={"BAD=KEY": "x"})


async def test_gnu_timeout_exit_after_the_limit_is_a_command_timeout() -> None:
    def times_out(cmd: str) -> RuntimeCommandResult:
        time.sleep(0.6)  # past ``timeout - 0.5`` for a 1s limit, as GNU timeout would be
        return RuntimeCommandResult(124, "", "")

    runtime, workspace = await provisioned(FakeDockerClient(exec_handler=times_out))

    with pytest.raises(CommandTimeoutError):
        await runtime.exec(workspace, "sleep 99", timeout=1)


async def test_a_command_that_itself_exits_124_early_is_just_an_exit_code() -> None:
    client = FakeDockerClient(exec_handler=lambda cmd: RuntimeCommandResult(124, "", ""))
    runtime, workspace = await provisioned(client)

    result = await runtime.exec(workspace, "exit 124", timeout=60)

    assert result.exit_code == 124


async def test_host_guard_kills_the_process_group_when_the_container_timeout_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(docker_module, "KILL_GRACE_SECONDS", 0)
    monkeypatch.setattr(docker_module, "HOST_GUARD_SLACK_SECONDS", 0)
    hung = threading.Event()
    client = FakeDockerClient(exec_handler=lambda cmd: DockerExecResult(block=hung))
    runtime, workspace = await provisioned(client)

    with pytest.raises(CommandTimeoutError):
        await runtime.exec(workspace, "sleep 999", timeout=1)

    [record] = client.api.execs.values()
    assert client.killed_pidfiles == [record["cmd"][3]]


async def test_cancelling_an_exec_kills_its_process_group() -> None:
    hung = threading.Event()
    client = FakeDockerClient(exec_handler=lambda cmd: DockerExecResult(block=hung))
    runtime, workspace = await provisioned(client)

    task = asyncio.create_task(runtime.exec(workspace, "sleep 999"))
    await until(lambda: client.blocked)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(client.killed_pidfiles) == 1


# --- stream_command ----------------------------------------------------------------------


async def test_stream_forwards_chunks_in_order_and_reassembles_split_utf8() -> None:
    euro = "€".encode()
    client = FakeDockerClient(
        exec_handler=lambda cmd: DockerExecResult(
            exit_code=7,
            chunks=(
                (b"one\n" + euro[:2], None),
                (None, b"warn\n"),
                (euro[2:] + b" two\n", None),
            ),
        )
    )
    runtime, workspace = await provisioned(client)
    stdout: list[str] = []
    stderr: list[str] = []

    async def out(text: str) -> None:
        stdout.append(text)

    async def err(text: str) -> None:
        stderr.append(text)

    exit_code = await runtime.stream_command(
        workspace,
        "cmd-1",
        "build",
        cwd=None,
        env={"K": "v"},
        timeout=10,
        on_stdout=out,
        on_stderr=err,
    )

    assert exit_code == 7
    assert "".join(stdout) == "one\n€ two\n"
    assert stderr == ["warn\n"]


async def test_cancelled_stream_stops_delivering_and_kills_the_command() -> None:
    hung = threading.Event()
    client = FakeDockerClient(
        exec_handler=lambda cmd: DockerExecResult(chunks=((b"late\n", None),), block=hung)
    )
    runtime, workspace = await provisioned(client)
    delivered: list[str] = []

    async def sink(text: str) -> None:
        delivered.append(text)

    task = asyncio.create_task(
        runtime.stream_command(
            workspace,
            "cmd-2",
            "serve",
            cwd=None,
            env=None,
            timeout=None,
            on_stdout=sink,
            on_stderr=sink,
        )
    )
    await until(lambda: client.blocked)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The kill releases the hung command, which then emits "late\n": wait until the pump
    # has really finished, so the assertion below is not vacuous.
    [record] = client.api.execs.values()
    await until(lambda: record["finished"])
    await asyncio.sleep(0.05)

    assert len(client.killed_pidfiles) == 1
    assert delivered == []  # output that arrives after cancellation is dropped


async def test_kill_is_never_starved_by_the_commands_it_stops() -> None:
    """Every in-flight command holds an exec thread; the kill runs on the separate control
    pool, so cancelling one command works even when the exec pool is full."""
    holds = [threading.Event(), threading.Event()]
    remaining = iter(holds)
    client = FakeDockerClient(exec_handler=lambda cmd: DockerExecResult(block=next(remaining)))
    runtime, workspace = await provisioned(client, docker_max_concurrent_execs=2)

    async def sink(text: str) -> None:
        return None

    tasks = [
        asyncio.create_task(
            runtime.stream_command(
                workspace,
                f"s{i}",
                "serve",
                cwd=None,
                env=None,
                timeout=None,
                on_stdout=sink,
                on_stderr=sink,
            )
        )
        for i in range(2)
    ]
    await until(lambda: len(client.blocked) == 2)  # the exec pool is now full
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tasks[0], timeout=5)

    assert len(client.killed_pidfiles) == 1
    for hold in holds:  # which hold each command drew depends on thread scheduling
        hold.set()
    assert await asyncio.wait_for(tasks[1], timeout=5) == 0
    await runtime.close()


async def test_kill_skips_a_command_that_already_finished() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    await runtime.exec(workspace, "true")
    [(exec_id, record)] = client.api.execs.items()
    container = client.containers.get(workspace.sandbox_id)

    await runtime._kill(container, exec_id, record["cmd"][3])

    assert client.killed_pidfiles == []


# --- preview ----------------------------------------------------------------------------


async def test_preview_link_dials_the_containers_sandbox_network_address() -> None:
    """By address, not name: dev servers such as Vite 6+ refuse an unknown Host header
    (`server.allowedHosts`) but always accept an IP."""
    runtime, workspace = await provisioned(FakeDockerClient())
    asked: list[str] = []

    async def resolve(name: str) -> list[str]:
        asked.append(name)
        return ["10.89.0.7"]

    runtime._resolve_host = resolve  # type: ignore[method-assign]

    link = await runtime.preview_link(workspace, 5173)

    assert link.url == "http://10.89.0.7:5173"
    assert link.token == ""
    # Absolute name: a resolver search domain can never turn it into an outside host.
    assert asked == [f"execute-{workspace.id.hex}."]


async def test_preview_link_needs_no_daemon_at_all() -> None:
    """The internet-facing preview proxy holds no daemon socket."""
    runtime = docker_runtime_with(FakeDockerClient())
    runtime._clients = None  # type: ignore[assignment]  # any daemon call would now crash
    workspace = docker_workspace("container-id")

    async def resolve(name: str) -> list[str]:
        return ["172.18.0.3"]

    runtime._resolve_host = resolve  # type: ignore[method-assign]

    link = await runtime.preview_link(workspace, 3000)

    assert link.url == "http://172.18.0.3:3000"


async def test_an_unresolvable_sandbox_is_unavailable_so_the_proxy_shows_waking() -> None:
    runtime = docker_runtime_with(FakeDockerClient())

    async def resolve(name: str) -> list[str]:
        raise OSError("Name or service not known")

    runtime._resolve_host = resolve  # type: ignore[method-assign]

    with pytest.raises(RuntimeUnavailableError):
        await runtime.preview_link(docker_workspace("container-id"), 3000)


@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.169.254", "0.0.0.0"])
async def test_preview_link_refuses_an_address_off_a_private_network(address) -> None:
    """Security: the proxy must never be steered at a public host, its own loopback or the
    cloud metadata endpoint, whatever the resolver answers."""
    runtime = docker_runtime_with(FakeDockerClient())

    async def resolve(name: str) -> list[str]:
        return [address]

    runtime._resolve_host = resolve  # type: ignore[method-assign]

    with pytest.raises(RuntimeUnavailableError):
        await runtime.preview_link(docker_workspace("container-id"), 3000)


async def test_preview_link_without_a_sandbox_is_not_found() -> None:
    runtime = docker_runtime_with(FakeDockerClient())
    with pytest.raises(SandboxNotFoundError):
        await runtime.preview_link(docker_workspace(), 3000)


async def test_expose_port_runs_the_relay_script_once_with_a_short_timeout() -> None:
    """The relay only admits the preview proxy, named by DOCKER_PREVIEW_PROXY_HOST."""
    from app.services.sandbox.port_relay import port_relay_command

    client = FakeDockerClient()
    runtime, workspace = await provisioned(client, docker_preview_proxy_host="proxy-a")

    await runtime.expose_port(workspace, 5173)

    [record] = client.api.execs.values()
    assert record["cmd"][4] == port_relay_command(5173, "proxy-a")
    assert "timeout -k 5 10 " in record["cmd"][2]


async def test_expose_port_failure_is_logged_not_raised() -> None:
    """A relay that cannot start must not fail the preview session around it."""
    client = FakeDockerClient(exec_handler=lambda cmd: RuntimeCommandResult(1, "boom", ""))
    runtime, workspace = await provisioned(client)

    await runtime.expose_port(workspace, 5173)


# --- errors ------------------------------------------------------------------------------


def test_error_mapping_never_leaks_the_daemon_url() -> None:
    api_error = APIError(
        "500 Server Error for url: http+docker://localhost/v1.41/containers/create",
        explanation="conflict: name already in use",
    )
    mapped = map_docker_error(api_error)
    assert isinstance(mapped, SandboxRuntimeError)
    assert "http+docker" not in str(mapped) and "conflict" in str(mapped)

    down = map_docker_error(requests.exceptions.ConnectionError("unix:///var/run/docker.sock"))
    assert isinstance(down, RuntimeUnavailableError) and "sock" not in str(down)

    slow = map_docker_error(
        requests.exceptions.ReadTimeout(
            "HTTPConnectionPool(host='10.0.0.5', port=2375): Read timed out."
        )
    )
    assert isinstance(slow, RuntimeUnavailableError) and "10.0.0.5" not in str(slow)

    unknown = map_docker_error(OSError("/var/run/docker.sock: broken pipe"))
    assert isinstance(unknown, SandboxRuntimeError) and "sock" not in str(unknown)


async def test_close_releases_both_clients() -> None:
    client = FakeDockerClient()
    runtime = docker_runtime_with(client)

    await runtime.close()
    await runtime.close()

    assert client.closed and client.api.closed
