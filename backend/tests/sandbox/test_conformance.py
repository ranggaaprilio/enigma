"""One contract, every provider.

Each case yields a ready runtime and a workspace bound to it. The ``daytona`` case drives the
real ``DaytonaRuntime`` over SDK-level fakes; the ``fake`` case is the interface-level double
consumer tests inject, so it can never drift from what real providers promise. A new provider
adds one case here (Epic 02: the Docker runtime over a fake daemon).
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sandbox import (
    PtySize,
    RuntimeCommandResult,
    RuntimePreviewLink,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from app.services.sandbox.daytona import DaytonaRuntime
from tests.sandbox.fakes import (
    FakeDaytonaClient,
    FakeDaytonaSandbox,
    FakeDockerClient,
    FakeSandboxRuntime,
    docker_runtime_with,
    make_workspace,
)

STATUSES = {"ready", "stopped", "creating", "error"}


async def _missing(fs, path: str) -> bool:
    """The contract: any exception from ``get_file_info`` means "does not exist"."""
    try:
        await fs.get_file_info(path)
    except Exception:
        return True
    return False


def echo(command: str) -> RuntimeCommandResult:
    if command == "fail":
        return RuntimeCommandResult(exit_code=3, stdout="", stderr="boom")
    return RuntimeCommandResult(exit_code=0, stdout=f"ran:{command}", stderr="")


# Contract parts a provider does not implement yet: strict xfail, so the marker has to be
# removed (and the suite proves the contract) the moment the epic lands.
PENDING = {
    "docker": {
        "test_preview_link_shape": "app preview lands in local-docker-runtime Epic 04",
    },
}


@pytest.fixture(params=["daytona", "docker", "fake"])
def case(request):
    reason = PENDING.get(request.param, {}).get(request.node.originalname)
    if reason:
        request.applymarker(
            pytest.mark.xfail(strict=True, raises=SandboxRuntimeError, reason=reason)
        )
    if request.param == "docker":
        client = FakeDockerClient(exec_handler=echo)
        runtime = docker_runtime_with(client)
        container = client.add_container("execute-conformance")
        return runtime, make_workspace(container.id, runtime="docker")
    if request.param == "daytona":
        sandbox = FakeDaytonaSandbox("sandbox-1", exec_handler=echo)
        runtime = DaytonaRuntime(Settings(daytona_api_key="test"))
        runtime._client = FakeDaytonaClient([sandbox])  # type: ignore[assignment]
        return runtime, make_workspace("sandbox-1")
    return FakeSandboxRuntime(exec_handler=echo), make_workspace("sandbox-1", runtime="fake")


async def test_identity(case) -> None:
    runtime, _workspace = case
    assert runtime.provider
    assert runtime.configured is True
    assert await runtime.health() == "ok"


async def test_provision_returns_a_sandbox_id_string(case) -> None:
    runtime, _workspace = case
    sandbox_id = await runtime.provision(make_workspace(None))
    assert isinstance(sandbox_id, str) and sandbox_id


async def test_status_is_one_of_the_four_states(case) -> None:
    runtime, workspace = case
    assert await runtime.status(workspace) in STATUSES


async def test_exec_returns_a_command_result(case) -> None:
    runtime, workspace = case
    ok = await runtime.exec(workspace, "echo hi", cwd="/workspace", timeout=5)
    assert isinstance(ok, RuntimeCommandResult)
    assert (ok.exit_code, ok.stdout) == (0, "ran:echo hi")
    failed = await runtime.exec(workspace, "fail")
    assert failed.exit_code == 3


async def test_stream_command_delivers_output_and_exit_code(case) -> None:
    runtime, workspace = case
    stdout: list[str] = []
    stderr: list[str] = []

    async def out(value: str) -> None:
        stdout.append(value)

    async def err(value: str) -> None:
        stderr.append(value)

    exit_code = await runtime.stream_command(
        workspace, "run-1", "echo hi", cwd=None, env=None, timeout=5, on_stdout=out, on_stderr=err
    )
    assert exit_code == 0
    # Providers may wrap the command (cwd/env prefix); the user's command still runs last.
    assert "".join(stdout).startswith("ran:") and "".join(stdout).endswith("echo hi")
    assert stderr == []


async def test_fs_round_trip(case) -> None:
    runtime, workspace = case
    fs = await runtime.fs(workspace)
    await fs.create_folder("/workspace/dir", "755")
    await fs.upload_file(b"hello", "/workspace/dir/a.txt")

    info = await fs.get_file_info("/workspace/dir/a.txt")
    assert info.is_dir is False
    assert (await fs.get_file_info("/workspace/dir")).is_dir is True
    assert await fs.download_file("/workspace/dir/a.txt") == b"hello"
    assert [entry.name for entry in await fs.list_files("/workspace/dir")] == ["a.txt"]
    # A content search (grep), as Daytona's is — not a filename glob.
    matches = await fs.find_files("/workspace", "ell")
    assert [(match.file, match.line, match.content) for match in matches] == [
        ("/workspace/dir/a.txt", 1, "hello")
    ]
    assert await fs.find_files("/workspace", "*.txt") == []

    await fs.set_file_permissions("/workspace/dir/a.txt", mode="600")
    await fs.move_files("/workspace/dir/a.txt", "/workspace/dir/b.txt")
    assert await _missing(fs, "/workspace/dir/a.txt")
    assert await fs.download_file("/workspace/dir/b.txt") == b"hello"

    await fs.delete_file("/workspace/dir", recursive=True)
    assert await _missing(fs, "/workspace/dir")


@pytest.mark.parametrize(
    "name", ["naïve résumé ✓.txt", "with space.txt", "-leading-dash", "semi; rm -rf x", "`tick`"]
)
async def test_fs_unusual_file_names_round_trip(case, name: str) -> None:
    runtime, workspace = case
    fs = await runtime.fs(workspace)
    await fs.create_folder("/workspace/odd", "755")
    path = f"/workspace/odd/{name}"
    await fs.upload_file("ünïcode\n".encode(), path)

    assert [entry.name for entry in await fs.list_files("/workspace/odd")] == [name]
    assert (await fs.get_file_info(path)).name == name
    assert await fs.download_file(path) == "ünïcode\n".encode()
    await fs.delete_file("/workspace/odd", recursive=True)
    assert await _missing(fs, path)


async def test_workspace_without_sandbox_raises_not_found(case) -> None:
    runtime, _workspace = case
    orphan = make_workspace(None, runtime=runtime.provider)
    for call in (
        runtime.exec(orphan, "true"),
        runtime.fs(orphan),
        runtime.status(orphan),
        runtime.preview_link(orphan, 3000),
    ):
        with pytest.raises(SandboxNotFoundError) as excinfo:
            await call
        assert isinstance(excinfo.value, SandboxRuntimeError)


async def test_pty_handle_contract(case) -> None:
    runtime, workspace = case
    received: list[bytes] = []

    async def on_data(data: bytes) -> None:
        received.append(data)

    handle = await runtime.create_or_connect_pty(
        workspace, "term-1", on_data, env={"TERM": "xterm"}, cols=80, rows=24
    )
    await handle.send_input(b"ls\n")
    await handle.resize(PtySize(cols=120, rows=40))
    await handle.disconnect()

    # Reconnecting to a live session by id is how the terminal survives a page reload.
    again = await runtime.create_or_connect_pty(
        workspace, "term-1", on_data, env=None, cols=80, rows=24
    )
    assert again is not None

    await runtime.kill_pty(workspace, "term-1")
    await runtime.kill_pty(workspace, "term-1")  # idempotent: unknown session is a no-op


async def test_preview_link_shape(case) -> None:
    runtime, workspace = case
    link = await runtime.preview_link(workspace, 5173)
    assert isinstance(link, RuntimePreviewLink)
    assert link.url.startswith("http")
    assert isinstance(link.token, str)


async def test_close_is_idempotent(case) -> None:
    runtime, _workspace = case
    await runtime.close()
    await runtime.close()
