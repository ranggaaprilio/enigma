from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import dev_server as dev_server_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models import User, Workspace
from app.services.daytona_runtime import RuntimeCommandResult
from app.services.dev_server import (
    DevServerNotClonedError,
    DevServerService,
    DevServerState,
)
from app.services.users import DuplicateUserEmailError
from app.services.workspaces import WorkspaceNotFoundError


class FakeUserStore:
    def __init__(self, user: User | None = None) -> None:
        self.user = user

    async def get_by_email(self, email: str) -> User | None:
        return self.user if self.user and self.user.email == email else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self.user if self.user and self.user.id == user_id else None

    async def register_user(self, email: str, name: str | None, password_hash: str) -> User:
        raise DuplicateUserEmailError


class FakeRateLimiter:
    async def check(self, email: str, request) -> None:
        return None

    async def reset(self, email: str, request) -> None:
        return None


# --- Service-level fakes -------------------------------------------------


class FakeFileInfo:
    def __init__(self, is_dir: bool) -> None:
        self.is_dir = is_dir


class FakeFs:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing

    async def get_file_info(self, path: str) -> FakeFileInfo:
        if path not in self.existing:
            raise FileNotFoundError(path)
        return FakeFileInfo(is_dir=path.endswith("/.git"))


class FakeSandbox:
    def __init__(self, existing: set[str]) -> None:
        self.fs = FakeFs(existing)


class FakeRuntime:
    def __init__(
        self,
        sandbox: FakeSandbox,
        *,
        status_value: str = "none",
        open_ports: set[int] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.status_value = status_value
        self.open_ports = open_ports or set()
        self.commands: list[str] = []

    async def get_sandbox(self, workspace, *, start: bool = True) -> FakeSandbox:
        return self.sandbox

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.commands.append(command)
        if command.startswith("cat "):
            return RuntimeCommandResult(exit_code=0, stdout=f"{self.status_value}\n", stderr="")
        if "python3 -c" in command:
            for port in self.open_ports:
                if f"127.0.0.1',{port}" in command:
                    return RuntimeCommandResult(exit_code=0, stdout="", stderr="")
            return RuntimeCommandResult(exit_code=1, stdout="", stderr="")
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")


class FakeSession:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def get(self, model, pk):
        return self.workspace if self.workspace.id == pk else None


def _workspace(user: User) -> Workspace:
    now = datetime.now(UTC)
    return Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=now,
        updated_at=now,
    )


def _user() -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def _service(runtime: FakeRuntime, workspace: Workspace) -> DevServerService:
    return DevServerService(FakeSession(workspace), get_settings(), runtime)


async def test_start_returns_no_config_without_mise_toml():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    service = _service(runtime, workspace)

    state = await service.start(workspace.id, user.id)

    assert state.status == "no-config"
    assert all("nohup" not in command for command in runtime.commands)


async def test_start_requires_cloned_repo():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox(set()))
    service = _service(runtime, workspace)

    with pytest.raises(DevServerNotClonedError):
        await service.start(workspace.id, user.id)


async def test_start_launches_mise_setup_and_dev():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = _service(runtime, workspace)

    state = await service.start(workspace.id, user.id)

    assert state.status == "installing"
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "mise install" in launch
    assert "mise run install" in launch  # project deps before the dev task
    assert "mise run dev" in launch
    assert "mise.run" in launch  # auto-install of the mise binary


async def test_start_does_not_relaunch_when_already_installing():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="installing",
    )
    service = _service(runtime, workspace)

    state = await service.start(workspace.id, user.id)

    assert state.status == "installing"
    assert all("nohup" not in command for command in runtime.commands)


async def test_status_running_with_open_port():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}),
        status_value="running",
        open_ports={5173},
    )
    service = _service(runtime, workspace)

    state = await service.status(workspace.id, user.id)

    assert state.status == "running"
    assert state.port == 5173


async def test_status_running_without_open_port_is_starting():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}),
        status_value="running",
        open_ports=set(),
    )
    service = _service(runtime, workspace)

    state = await service.status(workspace.id, user.id)

    assert state.status == "starting"
    assert state.port is None


async def test_status_no_dev_task_passthrough():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), status_value="no-dev-task")
    service = _service(runtime, workspace)

    state = await service.status(workspace.id, user.id)

    assert state.status == "no-dev-task"


async def test_stop_issues_pkill():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    service = _service(runtime, workspace)

    state = await service.stop(workspace.id, user.id)

    assert state.status == "stopped"
    assert any(
        "pkill" in command and "ise run dev" in command for command in runtime.commands
    )


# --- API-level fake + tests ----------------------------------------------


class FakeDevServerService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.state = DevServerState(status="idle")

    async def start(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        self._ensure_workspace(workspace_id, user_id)
        self.state = DevServerState(status="installing", message="Setting up environment…")
        return self.state

    async def status(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        self._ensure_workspace(workspace_id, user_id)
        return self.state

    async def stop(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        self._ensure_workspace(workspace_id, user_id)
        self.state = DevServerState(status="stopped")
        return self.state

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError


@pytest.fixture
def dev_server_context():
    user = _user()
    workspace = _workspace(user)
    service = FakeDevServerService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[dev_server_module.get_dev_server_service] = lambda: service
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_dev_endpoints_require_auth(dev_server_context):
    _user, workspace, _service = dev_server_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/workspaces/{workspace.id}/dev/start")

    assert response.status_code == 401


async def test_dev_workspace_ownership_is_enforced(dev_server_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{uuid4()}/dev/status")

    assert response.status_code == 404


async def test_dev_start_status_stop_round_trip(dev_server_context):
    _user, workspace, _service = dev_server_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        started = await client.post(f"/workspaces/{workspace.id}/dev/start")
        status = await client.get(f"/workspaces/{workspace.id}/dev/status")
        stopped = await client.post(f"/workspaces/{workspace.id}/dev/stop")

    assert started.json()["status"] == "installing"
    assert status.json()["status"] == "installing"
    assert stopped.json()["status"] == "stopped"
