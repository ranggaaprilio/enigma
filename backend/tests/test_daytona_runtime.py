from types import SimpleNamespace
from uuid import uuid4

import pytest
from daytona import DaytonaNotFoundError

from app.core.config import Settings
from app.models import Workspace
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaSandboxNotFoundError,
)


class FakeFileSystem:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    async def create_folder(self, path: str, mode: str) -> None:
        self.created.append((path, mode))


class FakeProcess:
    def __init__(self) -> None:
        self.request = None
        self.deleted: list[str] = []

    async def create_session(self, session_id: str) -> None:
        return None

    async def execute_session_command(self, session_id: str, request):
        self.request = request
        return SimpleNamespace(cmd_id="cmd-1", exit_code=None)

    async def get_session_command_logs_async(
        self,
        session_id: str,
        command_id: str,
        on_stdout,
        on_stderr,
    ) -> None:
        await on_stdout("hello")
        await on_stderr("warning")

    async def get_session_command(self, session_id: str, command_id: str):
        return SimpleNamespace(exit_code=0)

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


class FakeSandbox:
    def __init__(
        self,
        sandbox_id: str = "sandbox-1",
        state: str = "started",
        name: str = "sandbox",
        network_block_all: bool | None = None,
        network_allow_list: str | None = None,
    ) -> None:
        self.id = sandbox_id
        self.name = name
        self.state = state
        self.fs = FakeFileSystem()
        self.process = FakeProcess()
        self.started = False
        self.network_block_all = network_block_all
        self.network_allow_list = network_allow_list
        self.network_updates: list[dict] = []

    async def refresh_data(self) -> None:
        return None

    async def start(self, timeout: int) -> None:
        self.state = "started"
        self.started = True

    async def update_network_settings(
        self,
        *,
        network_block_all: bool | None = None,
        network_allow_list: str | None = None,
    ) -> None:
        self.network_updates.append(
            {"network_block_all": network_block_all, "network_allow_list": network_allow_list}
        )
        self.network_block_all = network_block_all
        self.network_allow_list = network_allow_list

    async def create_signed_preview_url(self, port: int, expires_in_seconds: int):
        return SimpleNamespace(url=f"https://{port}-{self.id}.example/?token=signed")

    async def get_preview_link(self, port: int):
        return SimpleNamespace(
            url=f"https://{port}-{self.id}.proxy.daytona.example",
            token="preview-token",
        )


class FakeClient:
    def __init__(self, existing: list[FakeSandbox] | None = None) -> None:
        self.existing = existing or []
        self.created_params = None
        self.created_sandbox = FakeSandbox()
        self.by_id = {sandbox.id: sandbox for sandbox in self.existing}

    async def list(self, query):
        for sandbox in self.existing:
            yield sandbox

    async def create(self, params, timeout: int):
        self.created_params = params
        self.by_id[self.created_sandbox.id] = self.created_sandbox
        return self.created_sandbox

    async def get(self, sandbox_id: str):
        if sandbox_id not in self.by_id:
            raise DaytonaNotFoundError("missing")
        return self.by_id[sandbox_id]


def workspace(sandbox_id: str | None = "sandbox-1") -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id=sandbox_id,
        workspace_path="/workspace",
        status="ready",
    )


async def test_provision_uses_snapshot_labels_and_auto_stop() -> None:
    runtime = DaytonaRuntime(
        Settings(
            daytona_api_key="test",
            daytona_default_snapshot="universal",
            daytona_auto_stop_minutes=30,
        )
    )
    client = FakeClient()
    runtime._client = client  # type: ignore[assignment]
    workspace_id = uuid4()
    owner_id = uuid4()

    sandbox = await runtime.provision(workspace_id, owner_id, None)

    assert sandbox.id == "sandbox-1"
    assert client.created_params.snapshot == "universal"
    assert client.created_params.auto_stop_interval == 30
    assert client.created_params.auto_archive_interval == 0
    assert client.created_params.auto_delete_interval == -1
    assert client.created_params.public is False
    assert client.created_params.labels["execute-workspace-id"] == str(workspace_id)
    assert sandbox.fs.created == [("workspace", "755")]
    # Default posture: open egress, no allow list.
    assert client.created_params.network_block_all is False
    assert client.created_params.network_allow_list is None


async def test_provision_passes_network_allow_list() -> None:
    runtime = DaytonaRuntime(
        Settings(
            daytona_api_key="test",
            daytona_network_allow_list="10.0.0.0/16, 10.1.0.0/16",
        )
    )
    client = FakeClient()
    runtime._client = client  # type: ignore[assignment]

    await runtime.provision(uuid4(), uuid4(), None)

    assert client.created_params.network_block_all is False
    assert client.created_params.network_allow_list == "10.0.0.0/16,10.1.0.0/16"


async def test_existing_stopped_sandbox_is_reused_and_started() -> None:
    workspace_id = uuid4()
    existing = FakeSandbox(
        "sandbox-existing",
        state="stopped",
        name=f"execute-{workspace_id.hex}",
    )
    runtime = DaytonaRuntime(Settings(daytona_api_key="test"))
    runtime._client = FakeClient([existing])  # type: ignore[assignment]

    result = await runtime.provision(workspace_id, uuid4(), "snapshot")

    assert result is existing
    assert existing.started is True
    # Reused sandbox predates the policy (network_* is None) → reconcile to default.
    assert existing.network_updates == [
        {"network_block_all": False, "network_allow_list": None}
    ]


async def test_reused_sandbox_skips_network_update_when_already_in_desired_state() -> None:
    workspace_id = uuid4()
    existing = FakeSandbox(
        "sandbox-existing",
        state="started",
        name=f"execute-{workspace_id.hex}",
        network_block_all=False,
        network_allow_list=None,
    )
    runtime = DaytonaRuntime(Settings(daytona_api_key="test"))
    runtime._client = FakeClient([existing])  # type: ignore[assignment]

    await runtime.provision(workspace_id, uuid4(), "snapshot")

    assert existing.network_updates == []


async def test_missing_workspace_sandbox_is_not_recreated() -> None:
    runtime = DaytonaRuntime(Settings(daytona_api_key="test"))
    runtime._client = FakeClient()  # type: ignore[assignment]

    with pytest.raises(DaytonaSandboxNotFoundError):
        await runtime.get_sandbox(workspace("missing"))


async def test_stream_command_injects_env_and_publishes_both_streams() -> None:
    sandbox = FakeSandbox()
    runtime = DaytonaRuntime(Settings(daytona_api_key="test"))
    runtime._client = FakeClient([sandbox])  # type: ignore[assignment]
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


async def test_signed_preview_url_uses_configured_ttl() -> None:
    sandbox = FakeSandbox()
    runtime = DaytonaRuntime(
        Settings(daytona_api_key="test", daytona_preview_url_ttl_seconds=900)
    )
    runtime._client = FakeClient([sandbox])  # type: ignore[assignment]

    assert await runtime.signed_preview_url(workspace(), 5173) == (
        "https://5173-sandbox-1.example/?token=signed"
    )


async def test_preview_link_returns_standard_url_and_token() -> None:
    sandbox = FakeSandbox()
    runtime = DaytonaRuntime(Settings(daytona_api_key="test"))
    runtime._client = FakeClient([sandbox])  # type: ignore[assignment]

    preview_link = await runtime.preview_link(workspace(), 5173)

    assert preview_link.url == "https://5173-sandbox-1.proxy.daytona.example"
    assert preview_link.token == "preview-token"


async def _append(values: list[str], value: str) -> None:
    values.append(value)
