"""Daytona adapter specifics: SDK parameter mapping, state mapping, error mapping.

The provider-neutral contract is covered by ``tests/sandbox/test_conformance.py``.
"""

from uuid import uuid4

import pytest
from daytona import DaytonaTimeoutError
from daytona import PtySize as SdkPtySize

from app.core.config import Settings
from app.services.sandbox import CommandTimeoutError, PtySize, SandboxNotFoundError
from app.services.sandbox.daytona import DaytonaRuntime
from tests.sandbox.fakes import FakeDaytonaClient, FakeDaytonaSandbox, make_workspace

workspace = make_workspace


def runtime_with(client: FakeDaytonaClient, **settings) -> DaytonaRuntime:
    runtime = DaytonaRuntime(Settings(daytona_api_key="test", **settings))
    runtime._client = client  # type: ignore[assignment]
    return runtime


async def test_provision_uses_snapshot_labels_and_auto_stop() -> None:
    client = FakeDaytonaClient()
    runtime = runtime_with(
        client, daytona_default_snapshot="universal", daytona_auto_stop_minutes=30
    )
    target = workspace(None)
    target.snapshot = None

    sandbox_id = await runtime.provision(target)

    assert sandbox_id == "sandbox-1"
    assert client.created_params.snapshot == "universal"
    assert client.created_params.auto_stop_interval == 30
    assert client.created_params.auto_archive_interval == 0
    assert client.created_params.auto_delete_interval == -1
    assert client.created_params.public is False
    assert client.created_params.labels["execute-workspace-id"] == str(target.id)
    assert client.created_params.labels["execute-owner-id"] == str(target.owner_id)
    assert client.created_sandbox.fs.created_folders == [("workspace", "755")]
    # Default posture: open egress, no allow list.
    assert client.created_params.network_block_all is False
    assert client.created_params.network_allow_list is None


async def test_provision_passes_network_allow_list() -> None:
    client = FakeDaytonaClient()
    runtime = runtime_with(client, daytona_network_allow_list="10.0.0.0/16, 10.1.0.0/16")

    await runtime.provision(workspace(None))

    assert client.created_params.network_block_all is False
    assert client.created_params.network_allow_list == "10.0.0.0/16,10.1.0.0/16"


async def test_existing_stopped_sandbox_is_reused_and_started() -> None:
    target = workspace(None)
    existing = FakeDaytonaSandbox(
        "sandbox-existing", state="stopped", name=f"execute-{target.id.hex}"
    )
    runtime = runtime_with(FakeDaytonaClient([existing]))

    assert await runtime.provision(target) == "sandbox-existing"
    assert existing.started is True
    # Reused sandbox predates the policy (network_* is None) → reconcile to default.
    assert existing.network_updates == [{"network_block_all": False, "network_allow_list": None}]


async def test_reused_sandbox_skips_network_update_when_already_in_desired_state() -> None:
    target = workspace(None)
    existing = FakeDaytonaSandbox(
        "sandbox-existing",
        state="started",
        name=f"execute-{target.id.hex}",
        network_block_all=False,
        network_allow_list=None,
    )
    runtime = runtime_with(FakeDaytonaClient([existing]))

    await runtime.provision(target)

    assert existing.network_updates == []


async def test_missing_workspace_sandbox_is_not_recreated() -> None:
    runtime = runtime_with(FakeDaytonaClient())

    with pytest.raises(SandboxNotFoundError):
        await runtime.fs(workspace("missing"))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("started", "ready"),
        ("stopped", "stopped"),
        ("archived", "stopped"),
        ("starting", "creating"),
        ("creating", "creating"),
        ("restoring", "creating"),
        ("building", "creating"),
        ("destroyed", "error"),
    ],
)
async def test_status_maps_sdk_states(state: str, expected: str) -> None:
    sandbox = FakeDaytonaSandbox(state=state)
    runtime = runtime_with(FakeDaytonaClient([sandbox]))

    assert await runtime.status(workspace()) == expected
    assert sandbox.started is False  # status never starts a sandbox


async def test_stream_command_injects_env_and_publishes_both_streams() -> None:
    from app.services.sandbox import RuntimeCommandResult

    sandbox = FakeDaytonaSandbox(
        exec_handler=lambda _: RuntimeCommandResult(exit_code=0, stdout="hello", stderr="warning")
    )
    runtime = runtime_with(FakeDaytonaClient([sandbox]))
    stdout: list[str] = []
    stderr: list[str] = []

    exit_code = await runtime.stream_command(
        workspace(),
        "run-1",
        "pnpm test",
        cwd="/workspace",
        env={"TOKEN": "secret value"},
        timeout=10,
        on_stdout=lambda value: _append(stdout, value),
        on_stderr=lambda value: _append(stderr, value),
    )

    assert exit_code == 0
    assert stdout == ["hello"]
    assert stderr == ["warning"]
    assert "cd /workspace && env TOKEN='secret value' pnpm test" == (
        sandbox.process.request.command
    )
    assert sandbox.process.deleted == ["run-1"]


async def test_exec_timeout_maps_to_command_timeout_error() -> None:
    sandbox = FakeDaytonaSandbox()

    async def timing_out(command, *, cwd=None, env=None, timeout=None):
        raise DaytonaTimeoutError("slow")

    sandbox.process.exec = timing_out  # type: ignore[method-assign]
    runtime = runtime_with(FakeDaytonaClient([sandbox]))

    with pytest.raises(CommandTimeoutError):
        await runtime.exec(workspace(), "sleep 999", timeout=1)


async def test_pty_resize_converts_to_the_sdk_size_type() -> None:
    sandbox = FakeDaytonaSandbox()
    runtime = runtime_with(FakeDaytonaClient([sandbox]))

    async def on_data(data: bytes) -> None:
        return None

    handle = await runtime.create_or_connect_pty(
        workspace(), "term-1", on_data, env={"A": "1"}, cols=80, rows=24
    )
    await handle.resize(PtySize(cols=132, rows=50))

    created = sandbox.process.pty_creates[0]
    assert created["cwd"] == "/workspace"
    assert created["envs"] == {"A": "1"}
    assert isinstance(created["pty_size"], SdkPtySize)
    resized = sandbox.process.pty_sessions["term-1"].sizes[0]
    assert isinstance(resized, SdkPtySize)
    assert (resized.cols, resized.rows) == (132, 50)


async def test_preview_link_returns_standard_url_and_token() -> None:
    sandbox = FakeDaytonaSandbox()
    runtime = runtime_with(FakeDaytonaClient([sandbox]))

    preview_link = await runtime.preview_link(workspace(), 5173)

    assert preview_link.url == "https://5173-sandbox-1.proxy.daytona.example"
    assert preview_link.token == "preview-token"


async def _append(values: list[str], value: str) -> None:
    values.append(value)


def test_make_workspace_helper_gives_each_call_a_fresh_id() -> None:
    assert workspace().id != workspace().id
    assert isinstance(uuid4(), type(workspace().id))
