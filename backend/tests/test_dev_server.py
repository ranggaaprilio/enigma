import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import dev_server as dev_server_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models import (
    CustomCommand,
    User,
    Workspace,
    WorkspaceEnvVar,
    WorkspaceService,
    WorkspaceWorktree,
)
from app.services.daytona_runtime import RuntimeCommandResult
from app.services.dev_server import (
    DevServerNotClonedError,
    DevServerService,
    DevServerState,
)
from app.services.users import DuplicateUserEmailError
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher
from app.services.worktrees import WorktreeNotFoundError


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
        dev_process_alive: bool = False,
        tail_output: str = "",
    ) -> None:
        self.sandbox = sandbox
        self.status_value = status_value
        self.open_ports = open_ports or set()
        self.dev_process_alive = dev_process_alive
        self.tail_output = tail_output
        self.commands: list[str] = []
        self.cwds: list[str | None] = []
        self.envs: list[dict | None] = []

    async def get_sandbox(self, workspace, *, start: bool = True) -> FakeSandbox:
        return self.sandbox

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.commands.append(command)
        self.cwds.append(cwd)
        self.envs.append(env)
        if "mise install (binary)" in command:
            return RuntimeCommandResult(exit_code=0, stdout=self.tail_output, stderr="")
        if "ENIGMA_PROBE_EOF" in command:
            return self._probe_response(command)
        if "ENIGMA_PORTS_EOF" in command:
            return self._port_scan_response(command)
        if command.startswith("cat "):
            return RuntimeCommandResult(exit_code=0, stdout=f"{self.status_value}\n", stderr="")
        if "pgrep" in command:
            # `_dev_process_alive` reads the pgrep exit code from stdout ("0" = alive).
            return RuntimeCommandResult(
                exit_code=0,
                stdout="0\n" if self.dev_process_alive else "1\n",
                stderr="",
            )
        if "python3 -c" in command:
            for port in self.open_ports:
                if f"127.0.0.1',{port}" in command:
                    return RuntimeCommandResult(exit_code=0, stdout="", stderr="")
            return RuntimeCommandResult(exit_code=1, stdout="", stderr="")
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")

    def _probe_response(self, command: str) -> RuntimeCommandResult:
        """`service_scripts.build_probe_script` embeds one `probe(id, status_path, pid_path,
        grep_token, port, health_path)` call per row it wants a status for. This fake answers
        every row identically from its own `status_value`/`dev_process_alive`/`open_ports`
        knobs, which is enough because these tests only ever probe a single (the primary) row
        at a time. `health_path` is captured but ignored -- these tests emit 4-field lines
        (health defaults to "unknown"), since none of them exercise the health probe itself
        (see test_service_health.py for that)."""
        lines = []
        for row_id, port_token in re.findall(
            r"probe\('([^']*)', '[^']*', '[^']*', '[^']*', (None|\d+), (?:None|'[^']*')\)", command
        ):
            pid_alive = 1 if self.dev_process_alive else 0
            if port_token == "None":
                port_open = 0
            else:
                port_open = 1 if int(port_token) in self.open_ports else 0
            lines.append(f"{row_id} {self.status_value} {pid_alive} {port_open}")
        stdout = ("\n".join(lines) + "\n") if lines else ""
        return RuntimeCommandResult(exit_code=0, stdout=stdout, stderr="")

    def _port_scan_response(self, command: str) -> RuntimeCommandResult:
        """`sandbox_probe._build_port_scan_script`'s heredoc, used by port discovery."""
        match = re.search(r"ports = \[([^\]]*)\]", command)
        candidates = (
            [int(token) for token in match.group(1).split(",") if token.strip()]
            if match
            else []
        )
        matched = [port for port in candidates if port in self.open_ports]
        return RuntimeCommandResult(
            exit_code=0, stdout=" ".join(str(port) for port in matched) + "\n", stderr=""
        )


class FakeScalars:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> FakeScalars:
        return FakeScalars(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self) -> list:
        # Epic 13's build_template_context does a multi-column select (WorkspaceService,
        # CustomCommand); these tests never populate that joined query (see `execute` above),
        # so this is always `[]` here -- present only so `.all()` doesn't raise.
        return self._rows


class FakeSession:
    def __init__(
        self,
        workspace: Workspace,
        worktree: WorkspaceWorktree | None = None,
        registry: dict[str, CustomCommand] | None = None,
    ) -> None:
        self.workspace = workspace
        self.worktree = worktree
        # Registry entries keyed by lifecycle role, as DevServerService._registry_entry (and
        # WorkspaceServiceManager._registry_command) look them up. Empty (the default) means
        # "no run configuration" — the mise path.
        self.registry = registry or {}
        # WorkspaceService rows created by WorkspaceServiceManager during a test, keyed by id.
        self.services: dict[UUID, WorkspaceService] = {}
        # Seeded by tests that want env vars injected/redacted.
        self.env_vars: list[WorkspaceEnvVar] = []

    async def get(self, model, pk):
        if self.workspace.id == pk:
            return self.workspace
        if self.worktree is not None and self.worktree.id == pk:
            return self.worktree
        if pk in self.services:
            return self.services[pk]
        for entry in self.registry.values():
            if entry.id == pk:
                return entry
        return None

    async def execute(self, statement):
        compiled = statement.compile()
        params = compiled.params
        froms = statement.get_final_froms()
        # A joined FROM (Epic 13's build_template_context: workspace_services outerjoin
        # custom_commands) has no single `.name` -- these tests exercise the dev-server
        # facade's script/pid behaviour, not sibling-service template context, so an empty
        # result is the correct answer (no services -> no per-service ENIGMA_* exports).
        table_name = getattr(froms[0], "name", None) if froms else None

        if table_name == "custom_commands":
            role = params.get("role_1")
            entry = self.registry.get(role) if role is not None else None
            return FakeResult([entry] if entry is not None else [])

        if table_name == "workspace_services":
            rows = list(self.services.values())
            if "workspace_id_1" in params:
                rows = [r for r in rows if r.workspace_id == params["workspace_id_1"]]
            if "custom_command_id_1" in params:
                rows = [r for r in rows if r.custom_command_id == params["custom_command_id_1"]]
            elif "custom_command_id IS NULL" in str(compiled):
                rows = [r for r in rows if r.custom_command_id is None]
            if "id_1" in params:
                rows = [r for r in rows if r.id != params["id_1"]]
            if "worktree_id_1" in params:
                rows = [r for r in rows if r.worktree_id == params["worktree_id_1"]]
            if "status_1" in params:
                allowed = params["status_1"]
                rows = [r for r in rows if r.status in allowed]
            rows.sort(key=lambda r: r.created_at or datetime.min.replace(tzinfo=UTC))
            return FakeResult(rows)

        if table_name == "workspace_env_vars":
            rows = list(self.env_vars)
            if "workspace_id_1" in params:
                rows = [r for r in rows if r.workspace_id == params["workspace_id_1"]]
            if "custom_command_id_1" in params:
                rows = [r for r in rows if r.custom_command_id == params["custom_command_id_1"]]
            elif "custom_command_id IS NULL" in str(compiled):
                rows = [r for r in rows if r.custom_command_id is None]
            return FakeResult(rows)

        return FakeResult([])

    def add(self, obj: object) -> None:
        if isinstance(obj, WorkspaceService):
            self.services[obj.id] = obj

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def refresh(self, obj: object) -> None:
        pass

    async def delete(self, obj: object) -> None:
        if isinstance(obj, WorkspaceService):
            self.services.pop(obj.id, None)


def _worktree(workspace: Workspace, *, branch: str = "feature-x") -> WorkspaceWorktree:
    return WorkspaceWorktree(
        id=uuid4(),
        workspace_id=workspace.id,
        branch=branch,
        path=f"worktrees/{branch}",
        base_ref="main",
        is_base=False,
        status="active",
    )


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


async def test_setup_returns_no_config_without_mise_toml():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    service = _service(runtime, workspace)

    state = await service.setup(workspace.id, user.id)

    assert state.status == "no-config"
    assert all("nohup" not in command for command in runtime.commands)


async def test_setup_requires_cloned_repo():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox(set()))
    service = _service(runtime, workspace)

    with pytest.raises(DevServerNotClonedError):
        await service.setup(workspace.id, user.id)


async def test_setup_installs_deps_without_running_dev():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = _service(runtime, workspace)

    state = await service.setup(workspace.id, user.id)

    assert state.status == "installing"
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "mise install" in launch
    assert "mise run install" in launch  # project deps are installed during setup
    assert "mise.run" in launch  # auto-install of the mise binary
    assert "mise run dev" not in launch  # setup must NOT run the app
    assert "setst ready" in launch  # prepared-but-not-running, generation-guarded
    # Setup alone must not mark the dev server as desired (no auto-resume on wake).
    assert not workspace.dev_autostart


async def test_setup_does_not_relaunch_when_already_installing():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="installing",
    )
    service = _service(runtime, workspace)

    state = await service.setup(workspace.id, user.id)

    assert state.status == "installing"
    assert all("nohup" not in command for command in runtime.commands)


async def test_setup_does_not_relaunch_when_ready():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="ready",
    )
    service = _service(runtime, workspace)

    state = await service.setup(workspace.id, user.id)

    assert state.status == "ready"
    assert all("nohup" not in command for command in runtime.commands)


async def test_run_launches_dev_task_and_marks_desired():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = _service(runtime, workspace)

    state = await service.run(workspace.id, user.id)

    assert state.status == "installing"  # "Starting app…" — dev server coming up
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "mise run dev" in launch
    assert "mise run install" in launch  # deps re-checked on every run (idempotent)
    # Running the app marks it desired so it auto-resumes after a sandbox wake.
    assert workspace.dev_autostart


async def test_run_no_ops_when_already_running():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="running",
        open_ports={5173},
    )
    service = _service(runtime, workspace)

    state = await service.run(workspace.id, user.id)

    assert state.status == "running"
    assert state.port == 5173
    assert all("nohup" not in command for command in runtime.commands)
    assert workspace.dev_autostart


# --- worktree-aware run/setup/status -------------------------------------


async def test_run_in_feature_worktree_uses_its_path_and_installs_deps():
    user = _user()
    workspace = _workspace(user)
    worktree = _worktree(workspace)
    runtime = FakeRuntime(
        FakeSandbox({"worktrees/feature-x/.git", "worktrees/feature-x/mise.toml"})
    )
    service = DevServerService(FakeSession(workspace, worktree), get_settings(), runtime)

    state = await service.run(workspace.id, user.id, worktree.id)

    assert state.status == "installing"
    assert state.worktree_id == worktree.id
    assert state.branch == "feature-x"
    # The dev task is launched in the worktree's working directory…
    launch_idx = next(i for i, c in enumerate(runtime.commands) if "nohup" in c)
    assert runtime.cwds[launch_idx] == "worktrees/feature-x"
    launch = runtime.commands[launch_idx]
    assert "mise run dev" in launch
    # …and a feature worktree installs its own project deps before serving.
    assert "mise run install" in launch
    assert workspace.dev_autostart
    assert workspace.dev_worktree_id == worktree.id


async def test_run_switching_worktree_stops_previous_server():
    user = _user()
    workspace = _workspace(user)
    worktree = _worktree(workspace)
    # A dev server is already running for the base clone (dev_worktree_id is None).
    runtime = FakeRuntime(
        FakeSandbox({"worktrees/feature-x/.git", "worktrees/feature-x/mise.toml"}),
        status_value="running",
        open_ports={5173},
    )
    service = DevServerService(FakeSession(workspace, worktree), get_settings(), runtime)

    state = await service.run(workspace.id, user.id, worktree.id)

    # Switching worktrees kills the running dev server before launching the new one.
    assert any("pkill" in c and "ise run dev" in c for c in runtime.commands)
    assert any("nohup" in c for c in runtime.commands)
    assert state.worktree_id == worktree.id
    assert workspace.dev_worktree_id == worktree.id


async def test_run_unknown_worktree_raises():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = _service(runtime, workspace)

    with pytest.raises(WorktreeNotFoundError):
        await service.run(workspace.id, user.id, uuid4())


async def test_status_reports_running_worktree_branch():
    user = _user()
    workspace = _workspace(user)
    worktree = _worktree(workspace)
    workspace.dev_worktree_id = worktree.id
    runtime = FakeRuntime(
        FakeSandbox({"worktrees/feature-x/.git"}),
        status_value="running",
        open_ports={5173},
    )
    service = DevServerService(FakeSession(workspace, worktree), get_settings(), runtime)

    state = await service.status(workspace.id, user.id)

    assert state.status == "running"
    assert state.port == 5173
    assert state.worktree_id == worktree.id
    assert state.branch == "feature-x"


async def test_status_reports_base_branch_when_no_worktree():
    user = _user()
    workspace = _workspace(user)  # dev_worktree_id defaults to None (base clone)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}),
        status_value="running",
        open_ports={5173},
    )
    service = _service(runtime, workspace)

    state = await service.status(workspace.id, user.id)

    assert state.worktree_id is None
    assert state.branch == "main"


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


async def test_status_ready_passthrough():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}), status_value="ready")
    service = _service(runtime, workspace)

    state = await service.status(workspace.id, user.id)

    assert state.status == "ready"
    assert state.message and "Run App" in state.message


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


# --- ensure_running (auto-resume on wake) --------------------------------


async def test_ensure_does_not_start_when_not_desired():
    """A workspace whose dev server was never started must not be auto-launched."""
    user = _user()
    workspace = _workspace(user)  # dev_autostart defaults falsy
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="none",
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "idle"
    assert all("nohup" not in command for command in runtime.commands)


async def test_ensure_relaunches_when_desired_but_down():
    """Desired + down (status gone after wake) → relaunch the dev server."""
    user = _user()
    workspace = _workspace(user)
    workspace.dev_autostart = True
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="none",
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "installing"
    assert any("nohup" in command and "mise run dev" in command for command in runtime.commands)


async def test_ensure_no_op_when_already_running():
    user = _user()
    workspace = _workspace(user)
    workspace.dev_autostart = True
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="running",
        open_ports={5173},
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "running"
    assert state.port == 5173
    assert all("nohup" not in command for command in runtime.commands)


async def test_ensure_waits_when_process_alive_but_port_not_bound():
    """Stale 'running' status with no open port but a live process → don't double-launch."""
    user = _user()
    workspace = _workspace(user)
    workspace.dev_autostart = True
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="running",
        open_ports=set(),
        dev_process_alive=True,
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "starting"
    assert all("nohup" not in command for command in runtime.commands)


async def test_ensure_relaunches_when_status_stale_but_process_dead():
    """Stale 'running' status after wake (process dead, no port) → relaunch."""
    user = _user()
    workspace = _workspace(user)
    workspace.dev_autostart = True
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="running",
        open_ports=set(),
        dev_process_alive=False,
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "installing"
    assert any("nohup" in command and "mise run dev" in command for command in runtime.commands)


async def test_ensure_does_not_relaunch_on_error_status():
    """A surfaced setup error must not trigger a relaunch storm."""
    user = _user()
    workspace = _workspace(user)
    workspace.dev_autostart = True
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="error",
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "error"
    assert all("nohup" not in command for command in runtime.commands)


# --- API-level fake + tests ----------------------------------------------


class FakeDevServerService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.state = DevServerState(status="idle")

    async def setup(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState:
        self._ensure_workspace(workspace_id, user_id)
        self.state = DevServerState(status="installing", message="Setting up environment…")
        return self.state

    async def run(
        self, workspace_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> DevServerState:
        self._ensure_workspace(workspace_id, user_id)
        self.state = DevServerState(status="installing", message="Starting app…")
        return self.state

    async def status(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
        self._ensure_workspace(workspace_id, user_id)
        return self.state

    async def ensure_running(self, workspace_id: UUID, user_id: UUID) -> DevServerState:
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
        response = await client.post(f"/workspaces/{workspace.id}/dev/setup")

    assert response.status_code == 401


async def test_dev_workspace_ownership_is_enforced(dev_server_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{uuid4()}/dev/status")

    assert response.status_code == 404


async def test_dev_setup_run_status_stop_round_trip(dev_server_context):
    _user, workspace, _service = dev_server_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        prepared = await client.post(f"/workspaces/{workspace.id}/dev/setup")
        ran = await client.post(f"/workspaces/{workspace.id}/dev/run")
        status = await client.get(f"/workspaces/{workspace.id}/dev/status")
        stopped = await client.post(f"/workspaces/{workspace.id}/dev/stop")

    assert prepared.json()["status"] == "installing"
    assert ran.json() == {"ok": True, "status": "installing", "message": "Starting app…"}
    assert status.json()["status"] == "installing"
    assert stopped.json()["status"] == "stopped"


async def test_dev_ensure_endpoint(dev_server_context):
    _user, workspace, _service = dev_server_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{workspace.id}/dev/ensure")

    assert response.status_code == 200
    body = response.json()
    assert "status" in body and "port" in body


# --- Epic 10: the registry drives what runs ------------------------------


def _registry_command(
    workspace: Workspace,
    *,
    role: str,
    command: str,
    cwd: str | None = None,
    port: int | None = None,
) -> CustomCommand:
    now = datetime.now(UTC)
    return CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name=role,
        command=command,
        role=role,
        kind="task",
        cwd=cwd,
        port=port,
        enabled=True,
        sort_order=0,
        requires_confirmation=False,
        created_at=now,
        updated_at=now,
    )


async def test_run_uses_the_registry_dev_command_without_a_mise_config():
    """The acceptance criterion: a repo with no mise.toml can still be served."""
    user = _user()
    workspace = _workspace(user)
    # Note: no mise.toml anywhere in the sandbox.
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    entry = _registry_command(
        workspace, role="dev", command="uvicorn app.main:app --port 8000", cwd="backend"
    )
    service = DevServerService(
        FakeSession(workspace, registry={"dev": entry}), get_settings(), runtime
    )

    state = await service.run(workspace.id, user.id)

    assert state.status == "installing"
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "uvicorn app.main:app --port 8000" in launch
    assert "mise run dev" not in launch
    # The command runs in its own working directory, not the checkout root.
    assert runtime.cwds[-1] == "workspace/backend"


async def test_run_composes_app_root_with_the_command_cwd():
    user = _user()
    workspace = _workspace(user)
    workspace.app_root = "apps"
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    entry = _registry_command(workspace, role="dev", command="pnpm dev", cwd="web")
    service = DevServerService(
        FakeSession(workspace, registry={"dev": entry}), get_settings(), runtime
    )

    await service.run(workspace.id, user.id)

    assert runtime.cwds[-1] == "workspace/apps/web"


async def test_run_without_a_registry_entry_still_uses_mise():
    """Existing workspaces must behave exactly as they did before the migration."""
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = DevServerService(FakeSession(workspace), get_settings(), runtime)

    state = await service.run(workspace.id, user.id)

    assert state.status == "installing"
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "mise run dev" in launch
    assert runtime.cwds[-1] == "workspace"


async def test_setup_uses_the_registry_install_command():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    entry = _registry_command(workspace, role="install", command="pnpm install", cwd="frontend")
    service = DevServerService(
        FakeSession(workspace, registry={"install": entry}), get_settings(), runtime
    )

    state = await service.setup(workspace.id, user.id)

    assert state.status == "installing"
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "pnpm install" in launch
    assert runtime.cwds[-1] == "workspace/frontend"


async def test_status_prefers_the_port_the_dev_command_declares():
    """First-match probing would report 5173 and hide an API on 8000."""
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}), status_value="running", open_ports={5173, 8000}
    )
    entry = _registry_command(workspace, role="dev", command="uvicorn app.main:app", port=8000)
    service = DevServerService(
        FakeSession(workspace, registry={"dev": entry}), get_settings(), runtime
    )

    state = await service.status(workspace.id, user.id)

    assert state.status == "running"
    assert state.port == 8000


# --- process tracking: registry launches must be stoppable and detectable ----------------


async def test_registry_dev_launch_records_its_pid_and_stop_kills_that_tree():
    """`pkill -f 'mise run dev'` never matched a `sh -lc 'pnpm dev'` launch, so Stop orphaned
    the server and `ensure_running` then started a second one on top of it."""
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git"}))
    entry = _registry_command(workspace, role="dev", command="pnpm dev")
    service = DevServerService(
        FakeSession(workspace, registry={"dev": entry}), get_settings(), runtime
    )

    await service.run(workspace.id, user.id)
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "echo $$ > /tmp/enigma-dev.pid" in launch

    await service.stop(workspace.id, user.id)
    stop = runtime.commands[-1]
    # Children first, then the wrapper — a killed `sh -c` must not leave its server behind.
    assert "killtree" in stop and "pgrep -P" in stop
    assert "/tmp/enigma-dev.pid" in stop and "/tmp/enigma-dev-setup.pid" in stop
    assert stop.rstrip().endswith("echo stopped > /tmp/enigma-dev.status")


async def test_setup_records_a_separate_pid_so_liveness_ignores_it():
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = _service(runtime, workspace)

    await service.setup(workspace.id, user.id)

    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "echo $$ > /tmp/enigma-dev-setup.pid" in launch
    assert "/tmp/enigma-dev.pid" not in launch


async def test_liveness_check_reads_the_pid_file_before_the_legacy_pgrep():
    user = _user()
    workspace = _workspace(user)
    workspace.dev_autostart = True
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git", "workspace/mise.toml"}),
        status_value="running",
        dev_process_alive=True,
    )
    service = _service(runtime, workspace)

    state = await service.ensure_running(workspace.id, user.id)

    assert state.status == "starting"
    probe = next(command for command in runtime.commands if "ENIGMA_PROBE_EOF" in command)
    assert "/tmp/enigma-dev.pid" in probe
    # A recycled PID that now belongs to something else must not count as our server — the
    # probe script only counts a pid alive when its own argv mentions this token.
    assert "'enigma-dev'" in probe
    assert not any("nohup" in command for command in runtime.commands)


# --- working directories: app_root and the install entry's own cwd -------------------


async def test_mise_run_honours_app_root_when_the_config_lives_in_the_app_package():
    """A monorepo with `apps/api/mise.toml` and no root config: the terminal and command runs
    already start in apps/api, so the mise default must run (and look for config) there too."""
    user = _user()
    workspace = _workspace(user)
    workspace.app_root = "apps/api"
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/apps/api/mise.toml"}))
    service = _service(runtime, workspace)

    state = await service.run(workspace.id, user.id)

    assert state.status == "installing"
    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "mise run dev" in launch
    assert runtime.cwds[-1] == "workspace/apps/api"


async def test_mise_run_with_app_root_still_accepts_a_root_config():
    """mise resolves config upward from the cwd, so a root mise.toml serves an app_root too."""
    user = _user()
    workspace = _workspace(user)
    workspace.app_root = "apps/api"
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    service = _service(runtime, workspace)

    state = await service.run(workspace.id, user.id)

    assert state.status == "installing"
    assert runtime.cwds[-1] == "workspace/apps/api"


async def test_mise_setup_honours_app_root():
    user = _user()
    workspace = _workspace(user)
    workspace.app_root = "apps/api"
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/apps/api/mise.toml"}))
    service = _service(runtime, workspace)

    state = await service.setup(workspace.id, user.id)

    assert state.status == "installing"
    assert runtime.cwds[-1] == "workspace/apps/api"


async def test_worktree_run_installs_in_the_install_entrys_own_directory():
    """install=`pnpm install` at the repo root + dev=`pnpm dev` in apps/web: the install must
    run at the root (as Setup runs it), not inside apps/web."""
    user = _user()
    workspace = _workspace(user)
    worktree = _worktree(workspace)
    runtime = FakeRuntime(FakeSandbox({"worktrees/feature-x/.git"}))
    dev = _registry_command(workspace, role="dev", command="pnpm dev", cwd="apps/web")
    install = _registry_command(workspace, role="install", command="pnpm install")
    service = DevServerService(
        FakeSession(workspace, worktree, registry={"dev": dev, "install": install}),
        get_settings(),
        runtime,
    )

    await service.run(workspace.id, user.id, worktree.id)

    launch = next(command for command in runtime.commands if "nohup" in command)
    assert runtime.cwds[-1] == "worktrees/feature-x/apps/web"
    # The whole script is single-quoted for `nohup sh -c`, so inner quotes are escaped; check
    # the shape rather than the literal quoting.
    assert "( cd " in launch and "worktrees/feature-x'" in launch
    assert launch.index("( cd ") < launch.index("pnpm install") < launch.index("pnpm dev")


async def test_worktree_run_skips_the_cd_when_install_and_dev_share_a_directory():
    user = _user()
    workspace = _workspace(user)
    worktree = _worktree(workspace)
    runtime = FakeRuntime(FakeSandbox({"worktrees/feature-x/.git"}))
    dev = _registry_command(workspace, role="dev", command="pnpm dev", cwd="web")
    install = _registry_command(workspace, role="install", command="pnpm install", cwd="web")
    service = DevServerService(
        FakeSession(workspace, worktree, registry={"dev": dev, "install": install}),
        get_settings(),
        runtime,
    )

    await service.run(workspace.id, user.id, worktree.id)

    launch = next(command for command in runtime.commands if "nohup" in command)
    assert "( cd " not in launch
    assert "pnpm install" in launch


# --- env injection at launch (Epic 12) -----------------------------------


async def test_setup_passes_env_to_runtime_exec():
    """Injection happens exclusively through the runtime's `env` parameter -- a secret value
    must never appear in the launched script body (`runtime.commands`)."""
    user = _user()
    workspace = _workspace(user)
    runtime = FakeRuntime(FakeSandbox({"workspace/.git", "workspace/mise.toml"}))
    session = FakeSession(workspace)
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    secret_value = "sk-live-abcdef1234567890"
    session.env_vars.append(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=None,
            key="API_KEY",
            encrypted_value=cipher.encrypt(secret_value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    service = DevServerService(session, get_settings(), runtime)

    await service.setup(workspace.id, user.id)

    # Epic 13: every launch also carries ENIGMA_WORKSPACE_ID/ENIGMA_BRANCH -- assert the
    # user's own value survives untouched rather than pinning the exact dict.
    assert runtime.envs[-1]["API_KEY"] == secret_value
    assert all(secret_value not in command for command in runtime.commands)


async def test_error_state_log_tail_is_redacted():
    """`_read_log_tail`'s output reaches the browser via `DevServerState.message`, so it must
    be redacted exactly like every other log surface."""
    user = _user()
    workspace = _workspace(user)
    secret_value = "sk-live-abcdef1234567890"
    runtime = FakeRuntime(
        FakeSandbox({"workspace/.git"}),
        tail_output=f"connecting with API_KEY={secret_value}\n",
    )
    session = FakeSession(workspace)
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    session.env_vars.append(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=None,
            key="API_KEY",
            encrypted_value=cipher.encrypt(secret_value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    service = DevServerService(session, get_settings(), runtime)

    tail = await service._read_log_tail(workspace)

    assert secret_value not in tail
    assert "[redacted]" in tail
