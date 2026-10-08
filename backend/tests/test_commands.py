from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api import auth as auth_module
from app.api import commands as commands_module
from app.core.config import Settings
from app.core.security import hash_password
from app.main import app
from app.models import (
    CommandRun,
    CustomCommand,
    User,
    Workspace,
    WorkspaceEnvVar,
    WorkspaceWorktree,
)
from app.services.agent_tools import DangerousCommandError
from app.services.commands import (
    CommandConfirmationRequiredError,
    CommandRuntime,
    CommandService,
    CustomCommandInvalidError,
    CustomCommandNotFoundError,
    validate_user_command,
)
from app.services.git_providers import redact_secrets
from app.services.run_paths import (
    RunPathError,
    resolve_run_cwd,
    validate_relative_subpath,
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


class FakeCommandService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.commands: dict[UUID, CustomCommand] = {}
        self.runs: dict[UUID, list[CommandRun]] = {}

    async def create_command(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
        command: str,
        description: str | None,
        icon: str | None,
        hotkey: str | None,
        requires_confirmation: bool,
        role: str = "custom",
        kind: str | None = None,
        cwd: str | None = None,
        port: int | None = None,
        enabled: bool = True,
        timeout_seconds: int | None = None,
        health_path: str | None = None,
        is_entry_point: bool = False,
    ) -> CustomCommand:
        self._ensure_workspace(workspace_id, user_id)
        now = datetime.now(UTC)
        # Mirrors CommandService.create_command: a `dev`-role command defaults to a service.
        effective_kind = kind if kind is not None else ("service" if role == "dev" else "task")
        if is_entry_point and effective_kind != "service":
            raise CustomCommandInvalidError(
                "Only a long-running service command can be the entry point."
            )
        if is_entry_point:
            for existing in self.commands.values():
                existing.is_entry_point = False
        custom_command = CustomCommand(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            command=command,
            description=description,
            icon=icon,
            hotkey=hotkey,
            requires_confirmation=requires_confirmation,
            role=role,
            kind=effective_kind,
            cwd=cwd,
            port=port,
            enabled=enabled,
            timeout_seconds=timeout_seconds,
            health_path=health_path,
            is_entry_point=is_entry_point,
            sort_order=len(self.commands) * 10,
            created_at=now,
            updated_at=now,
        )
        self.commands[custom_command.id] = custom_command
        return custom_command

    async def list_commands(self, workspace_id: UUID, user_id: UUID) -> list[CustomCommand]:
        self._ensure_workspace(workspace_id, user_id)
        return list(self.commands.values())

    async def update_command(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        values: dict,
    ) -> CustomCommand:
        self._ensure_workspace(workspace_id, user_id)
        custom_command = self.commands.get(command_id)
        if custom_command is None:
            raise CustomCommandNotFoundError
        if values.get("is_entry_point") is True:
            effective_kind = values.get("kind", custom_command.kind)
            if effective_kind != "service":
                raise CustomCommandInvalidError(
                    "Only a long-running service command can be the entry point."
                )
            for other in self.commands.values():
                if other.id != custom_command.id:
                    other.is_entry_point = False
        for key, value in values.items():
            setattr(custom_command, key, value)
        custom_command.updated_at = datetime.now(UTC)
        return custom_command

    async def delete_command(self, workspace_id: UUID, command_id: UUID, user_id: UUID) -> None:
        self._ensure_workspace(workspace_id, user_id)
        if command_id not in self.commands:
            raise CustomCommandNotFoundError
        del self.commands[command_id]

    async def import_commands(
        self,
        workspace_id: UUID,
        user_id: UUID,
        items: list[dict],
    ) -> list[CustomCommand]:
        self._ensure_workspace(workspace_id, user_id)
        created = []
        for item in items:
            created.append(
                await self.create_command(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    name=item["name"],
                    command=item["command"],
                    description=None,
                    icon=None,
                    hotkey=None,
                    requires_confirmation=False,
                    role=item.get("role", "custom"),
                    cwd=item.get("cwd"),
                    port=item.get("port"),
                )
            )
        return created

    async def reorder_commands(
        self,
        workspace_id: UUID,
        user_id: UUID,
        order: dict[UUID, int],
    ) -> list[CustomCommand]:
        self._ensure_workspace(workspace_id, user_id)
        for command_id, sort_order in order.items():
            if command_id in self.commands:
                self.commands[command_id].sort_order = sort_order
        return sorted(self.commands.values(), key=lambda row: row.sort_order)

    async def create_run(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        confirmed: bool,
        worktree_id: UUID | None = None,
    ) -> CommandRun:
        self._ensure_workspace(workspace_id, user_id)
        custom_command = self.commands.get(command_id)
        if custom_command is None:
            raise CustomCommandNotFoundError
        if custom_command.requires_confirmation and not confirmed:
            raise CommandConfirmationRequiredError
        now = datetime.now(UTC)
        run = CommandRun(
            id=uuid4(),
            custom_command_id=custom_command.id,
            workspace_id=workspace_id,
            user_id=user_id,
            worktree_id=worktree_id,
            cwd=custom_command.cwd,
            status="running",
            exit_code=None,
            output="",
            started_at=now,
            completed_at=None,
        )
        self.runs.setdefault(custom_command.id, []).append(run)
        return run

    async def list_runs(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
    ) -> list[CommandRun]:
        self._ensure_workspace(workspace_id, user_id)
        if command_id not in self.commands:
            raise CustomCommandNotFoundError
        return self.runs.get(command_id, [])

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError


class FakeSession:
    def add(self, _value) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def refresh(self, _value) -> None:
        return None

    async def execute(self, _statement):
        # The conditional "finish a running run" UPDATE always matches its (running) row.
        return SimpleNamespace(scalar_one_or_none=lambda: uuid4())


@pytest.fixture
def command_context(tmp_path):
    user = User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace_path = tmp_path / str(user.id) / "workspace"
    workspace_path.mkdir(parents=True)
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = FakeCommandService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[commands_module.get_command_service] = lambda: service
    # `enforce_command_run_rate_limit` talks to Redis directly (mirrors the agents endpoint's
    # `enforce_agent_run_rate_limit`, overridden the same way in test_agents.py) rather than a
    # dependency this test setup already fakes, so a `/run` call here would need a live Redis
    # without this override.
    app.dependency_overrides[commands_module.enforce_command_run_rate_limit] = lambda: None
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_command_endpoints_require_auth(command_context):
    _user, workspace, _service = command_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace.id}/commands")

    assert response.status_code == 401


async def test_create_update_run_list_and_delete_command(command_context):
    _user, workspace, service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Run tests",
                "command": "pytest --version",
                "description": "Check pytest",
                "icon": "test-tube",
                "hotkey": "T",
                "requires_confirmation": False,
            },
        )
        command_id = created.json()["id"]
        listed = await client.get(f"/workspaces/{workspace.id}/commands")
        updated = await client.patch(
            f"/workspaces/{workspace.id}/commands/{command_id}",
            json={"name": "Pytest"},
        )
        run = await client.post(f"/workspaces/{workspace.id}/commands/{command_id}/run")
        runs = await client.get(f"/workspaces/{workspace.id}/commands/{command_id}/runs")
        deleted = await client.delete(f"/workspaces/{workspace.id}/commands/{command_id}")

    assert created.status_code == 201
    assert created.json()["command"] == "pytest --version"
    assert listed.json()[0]["name"] == "Run tests"
    assert updated.json()["name"] == "Pytest"
    assert run.status_code == 201
    assert runs.json()[0]["id"] == run.json()["id"]
    assert deleted.json() == {"ok": True}
    assert UUID(command_id) not in service.commands


async def test_command_run_requires_confirmation(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Install",
                "command": "pnpm install",
                "requires_confirmation": True,
            },
        )
        command_id = created.json()["id"]
        rejected = await client.post(f"/workspaces/{workspace.id}/commands/{command_id}/run")
        accepted = await client.post(
            f"/workspaces/{workspace.id}/commands/{command_id}/run",
            json={"confirmed": True},
        )

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == "Command requires confirmation"
    assert accepted.status_code == 201


async def test_command_ownership_and_not_found_are_enforced(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        alien_workspace = await client.get(f"/workspaces/{uuid4()}/commands")
        missing_command = await client.patch(
            f"/workspaces/{workspace.id}/commands/{uuid4()}",
            json={"name": "Missing"},
        )

    assert alien_workspace.status_code == 404
    assert missing_command.status_code == 404
    assert missing_command.json()["detail"] == "Command not found"


def test_validate_user_command_allows_everyday_dev_commands():
    """Owner-authored registry entries are not denylisted.

    The workspace owner already has an unrestricted PTY into the same sandbox, so the old
    denylist only made ordinary commands unusable without ticking `requires_confirmation`.
    """
    assert validate_user_command("pytest --version") == ["sh", "-lc", "pytest --version"]
    # Previously rejected outright unless flagged requires_confirmation *and* confirmed.
    assert validate_user_command("cd api && pnpm dev") == ["sh", "-lc", "cd api && pnpm dev"]
    assert validate_user_command("pnpm install") == ["sh", "-lc", "pnpm install"]
    assert validate_user_command("  alembic upgrade head  ") == [
        "sh",
        "-lc",
        "alembic upgrade head",
    ]

    with pytest.raises(DangerousCommandError):
        validate_user_command("   ")


async def test_command_service_captures_output_status_and_truncates(tmp_path):
    run = CommandRun(
        id=uuid4(),
        custom_command_id=uuid4(),
        workspace_id=uuid4(),
        user_id=uuid4(),
        status="running",
        exit_code=None,
        output="",
        started_at=datetime.now(UTC),
        completed_at=None,
    )
    service = CommandService(
        session=FakeSession(),  # type: ignore[arg-type]
        session_maker=None,  # type: ignore[arg-type]
        settings=Settings(workspace_root=tmp_path, agent_max_command_output_chars=5),
        runtime=CommandRuntime(),
    )
    await service._append_output(FakeSession(), run, "abcdef")  # type: ignore[arg-type]
    await service._mark_finished(FakeSession(), run, "completed", 0)  # type: ignore[arg-type]

    assert run.status == "completed"
    assert run.exit_code == 0
    assert run.output == "abcde"


async def test_command_service_maps_nonzero_exit_to_failed(tmp_path):
    run = CommandRun(
        id=uuid4(),
        custom_command_id=uuid4(),
        workspace_id=uuid4(),
        user_id=uuid4(),
        status="running",
        exit_code=None,
        output="",
        started_at=datetime.now(UTC),
        completed_at=None,
    )
    service = CommandService(
        session=FakeSession(),  # type: ignore[arg-type]
        session_maker=None,  # type: ignore[arg-type]
        settings=Settings(workspace_root=tmp_path),
        runtime=CommandRuntime(),
    )
    await service._mark_finished(FakeSession(), run, "failed", 7)  # type: ignore[arg-type]

    assert run.status == "failed"
    assert run.exit_code == 7


# --- Epic 10: run configuration ------------------------------------------


def _registry_workspace(tmp_path, *, app_root: str | None = None) -> Workspace:
    now = datetime.now(UTC)
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        app_root=app_root,
        status="ready",
        created_at=now,
        updated_at=now,
    )


class RunPathSession:
    """Resolves worktrees by id the way the real session does, and nothing else."""

    def __init__(self, worktree: WorkspaceWorktree | None = None) -> None:
        self.worktree = worktree

    async def get(self, model, pk):
        if self.worktree is not None and self.worktree.id == pk:
            return self.worktree
        return None


async def test_resolve_run_cwd_composes_app_root_and_command_cwd(tmp_path):
    workspace = _registry_workspace(tmp_path, app_root="apps/api")

    cwd, relative, worktree_id, branch = await resolve_run_cwd(
        RunPathSession(),  # type: ignore[arg-type]
        workspace,
        worktree_id=None,
        command_cwd="scripts",
    )

    assert cwd == "workspace/apps/api/scripts"
    assert relative == "apps/api/scripts"
    assert worktree_id is None
    assert branch == "main"


async def test_resolve_run_cwd_runs_inside_the_selected_worktree(tmp_path):
    workspace = _registry_workspace(tmp_path, app_root="backend")
    worktree = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=workspace.id,
        branch="feature-x",
        path="worktrees/feature-x",
        base_ref="main",
        is_base=False,
        status="active",
    )

    cwd, relative, worktree_id, branch = await resolve_run_cwd(
        RunPathSession(worktree),  # type: ignore[arg-type]
        workspace,
        worktree_id=worktree.id,
        command_cwd=None,
    )

    assert cwd == "worktrees/feature-x/backend"
    assert relative == "backend"
    assert worktree_id == worktree.id
    assert branch == "feature-x"


async def test_resolve_run_cwd_rejects_a_worktree_from_another_workspace(tmp_path):
    workspace = _registry_workspace(tmp_path)
    foreign = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=uuid4(),
        branch="feature-x",
        path="worktrees/feature-x",
        base_ref="main",
        is_base=False,
        status="active",
    )

    with pytest.raises(WorktreeNotFoundError):
        await resolve_run_cwd(
            RunPathSession(foreign),  # type: ignore[arg-type]
            workspace,
            worktree_id=foreign.id,
            command_cwd=None,
        )


@pytest.mark.parametrize(
    "candidate", ["/etc", "/", "//", "../../etc", "apps/../../etc", "apps/../.."]
)
def test_validate_relative_subpath_rejects_escapes(candidate):
    with pytest.raises(RunPathError):
        validate_relative_subpath(candidate)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("apps/api/", "apps/api"),
        ("apps/api", "apps/api"),
        ("  apps/api  ", "apps/api"),
    ],
)
def test_validate_relative_subpath_normalizes(candidate, expected):
    assert validate_relative_subpath(candidate) == expected


def _timeout_service(tmp_path, **settings_kwargs) -> CommandService:
    return CommandService(
        session=FakeSession(),  # type: ignore[arg-type]
        session_maker=None,  # type: ignore[arg-type]
        settings=Settings(workspace_root=tmp_path, **settings_kwargs),
        runtime=CommandRuntime(),
    )


def test_run_timeout_uses_the_command_budget_not_the_agent_one(tmp_path):
    service = _timeout_service(tmp_path)
    command = CustomCommand(id=uuid4(), workspace_id=uuid4(), name="Install", command="pnpm i")

    # The old behaviour reused agent_command_timeout_seconds (20s), which `pnpm install`
    # cannot survive. User commands now get their own, much larger budget.
    assert service._run_timeout(command) == service.settings.command_run_timeout_seconds
    assert service._run_timeout(command) == 900


def test_run_timeout_honours_a_per_command_override(tmp_path):
    service = _timeout_service(tmp_path)
    command = CustomCommand(
        id=uuid4(), workspace_id=uuid4(), name="Slow", command="sleep 60", timeout_seconds=120
    )

    assert service._run_timeout(command) == 120


def test_run_timeout_is_clamped_to_the_platform_maximum(tmp_path):
    service = _timeout_service(tmp_path, command_run_max_timeout_seconds=300)
    command = CustomCommand(
        id=uuid4(), workspace_id=uuid4(), name="Forever", command="sleep 1", timeout_seconds=86400
    )

    assert service._run_timeout(command) == 300


async def test_command_output_redacts_injected_env_values(tmp_path):
    """An `env`-style command must not persist decrypted workspace secrets."""
    run = CommandRun(
        id=uuid4(),
        custom_command_id=uuid4(),
        workspace_id=uuid4(),
        user_id=uuid4(),
        status="running",
        exit_code=None,
        output="",
        started_at=datetime.now(UTC),
        completed_at=None,
    )
    service = CommandService(
        session=FakeSession(),  # type: ignore[arg-type]
        session_maker=None,  # type: ignore[arg-type]
        settings=Settings(workspace_root=tmp_path),
        runtime=CommandRuntime(),
    )

    await service._append_output(
        FakeSession(),  # type: ignore[arg-type]
        run,
        redact_secrets("DATABASE_PASSWORD=s3cr3t-value\n", "s3cr3t-value"),
    )

    assert "s3cr3t-value" not in run.output
    assert "[redacted]" in run.output


async def test_create_command_accepts_registry_fields(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Serve API",
                "command": "uvicorn app.main:app --port 8000",
                "requires_confirmation": False,
                "role": "dev",
                "cwd": "backend",
                "port": 8000,
                "timeout_seconds": 1800,
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["role"] == "dev"
    # A `dev`-role command defaults to `service` (Epic 11) so it can run as a long-lived
    # process; an explicit `kind` in the request would still override this.
    assert body["kind"] == "service"
    assert body["cwd"] == "backend"
    assert body["port"] == 8000
    assert body["timeout_seconds"] == 1800
    assert body["enabled"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [("cwd", "../../etc"), ("cwd", "/etc"), ("port", 0), ("port", 70000), ("role", "deploy")],
)
async def test_create_command_rejects_unsafe_registry_values(command_context, field, value):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Bad",
                "command": "echo hi",
                "requires_confirmation": False,
                field: value,
            },
        )

    assert response.status_code == 422


async def test_create_command_rejects_health_path_without_leading_slash(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "API",
                "command": "uvicorn app.main:app",
                "requires_confirmation": False,
                "role": "dev",
                "health_path": "healthz",
            },
        )

    assert response.status_code == 422


async def test_create_command_rejects_task_kind_as_entry_point(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Lint",
                "command": "ruff check .",
                "requires_confirmation": False,
                "kind": "task",
                "is_entry_point": True,
            },
        )

    assert response.status_code == 400


async def test_create_command_accepts_health_path_and_entry_point(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "API",
                "command": "uvicorn app.main:app --port 8000",
                "requires_confirmation": False,
                "role": "dev",
                "port": 8000,
                "health_path": "/healthz",
                "is_entry_point": True,
            },
        )

    assert created.status_code == 201
    body = created.json()
    assert body["health_path"] == "/healthz"
    assert body["is_entry_point"] is True


async def test_creating_a_second_entry_point_clears_the_first(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        first = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Web",
                "command": "pnpm dev",
                "requires_confirmation": False,
                "role": "dev",
                "kind": "service",
                "is_entry_point": True,
            },
        )
        assert first.json()["is_entry_point"] is True

        second = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "API",
                "command": "uvicorn app.main:app",
                "requires_confirmation": False,
                "kind": "service",
                "is_entry_point": True,
            },
        )
        assert second.json()["is_entry_point"] is True

        listed = await client.get(f"/workspaces/{workspace.id}/commands")
        entry_points = [item for item in listed.json() if item["is_entry_point"]]
        assert len(entry_points) == 1
        assert entry_points[0]["id"] == second.json()["id"]


async def test_update_command_rejects_marking_a_task_as_entry_point(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={"name": "Lint", "command": "ruff check .", "requires_confirmation": False},
        )
        command_id = created.json()["id"]

        response = await client.patch(
            f"/workspaces/{workspace.id}/commands/{command_id}",
            json={"is_entry_point": True},
        )

    assert response.status_code == 400


async def test_update_command_clears_the_previous_entry_point(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        first = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Web",
                "command": "pnpm dev",
                "requires_confirmation": False,
                "kind": "service",
                "is_entry_point": True,
            },
        )
        second = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "API",
                "command": "uvicorn app.main:app",
                "requires_confirmation": False,
                "kind": "service",
            },
        )

        updated = await client.patch(
            f"/workspaces/{workspace.id}/commands/{second.json()['id']}",
            json={"is_entry_point": True},
        )
        assert updated.json()["is_entry_point"] is True

        listed = await client.get(f"/workspaces/{workspace.id}/commands")
        by_id = {item["id"]: item for item in listed.json()}
        assert by_id[first.json()["id"]]["is_entry_point"] is False
        assert by_id[second.json()["id"]]["is_entry_point"] is True


async def test_reorder_commands_updates_sort_order(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        first = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={"name": "First", "command": "echo 1", "requires_confirmation": False},
        )
        second = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={"name": "Second", "command": "echo 2", "requires_confirmation": False},
        )
        reordered = await client.post(
            f"/workspaces/{workspace.id}/commands/reorder",
            json={
                "items": [
                    {"id": second.json()["id"], "sort_order": 10},
                    {"id": first.json()["id"], "sort_order": 20},
                ]
            },
        )

    assert reordered.status_code == 200
    assert [row["name"] for row in reordered.json()] == ["Second", "First"]


async def test_run_accepts_a_worktree_target(command_context):
    _user, workspace, service = command_context
    worktree_id = uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={"name": "Test", "command": "pytest", "requires_confirmation": False},
        )
        command_id = created.json()["id"]
        run = await client.post(
            f"/workspaces/{workspace.id}/commands/{command_id}/run",
            json={"confirmed": False, "worktree_id": str(worktree_id)},
        )

    assert run.status_code == 201
    assert run.json()["worktree_id"] == str(worktree_id)


@pytest.mark.parametrize("candidate", ["../secrets", "/etc/passwd", "apps/../../etc"])
def test_workspace_update_rejects_an_escaping_app_root(candidate):
    from app.schemas.workspaces import WorkspaceUpdateRequest

    with pytest.raises(ValidationError):
        WorkspaceUpdateRequest(app_root=candidate)


def test_workspace_update_normalizes_app_root():
    from app.schemas.workspaces import WorkspaceUpdateRequest

    assert WorkspaceUpdateRequest(app_root="  apps/api/  ").app_root == "apps/api"
    # An explicit empty value clears it back to the checkout root.
    assert WorkspaceUpdateRequest(app_root="").app_root is None


async def test_import_commands_creates_registry_entries(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        imported = await client.post(
            f"/workspaces/{workspace.id}/commands/import",
            json={
                "items": [
                    {
                        "name": "dev:api",
                        "command": "uvicorn app.main:app --port 8000",
                        "role": "dev",
                        "cwd": "backend",
                        "port": 8000,
                    },
                    {"name": "migrate", "command": "alembic upgrade head", "role": "migrate"},
                ]
            },
        )

    assert imported.status_code == 201
    body = imported.json()
    assert [row["name"] for row in body] == ["dev:api", "migrate"]
    assert body[0]["cwd"] == "backend"
    assert body[0]["port"] == 8000
    assert body[1]["role"] == "migrate"


async def test_import_rejects_an_escaping_cwd(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/commands/import",
            json={"items": [{"name": "bad", "command": "echo hi", "cwd": "../../etc"}]},
        )

    assert response.status_code == 422


async def test_list_and_apply_a_run_preset(command_context):
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        listed = await client.get(f"/workspaces/{workspace.id}/commands/presets")
        applied = await client.post(
            f"/workspaces/{workspace.id}/commands/presets/vite+fastapi/apply"
        )
        missing = await client.post(f"/workspaces/{workspace.id}/commands/presets/nope/apply")

    assert listed.status_code == 200
    assert "vite+fastapi" in {preset["slug"] for preset in listed.json()}

    assert applied.status_code == 201
    names = [row["name"] for row in applied.json()]
    assert "dev:web" in names and "dev:api" in names
    assert {row["port"] for row in applied.json() if row["port"]} == {5173, 8000}

    assert missing.status_code == 404


def test_redactable_env_values_skip_short_values_under_plain_keys():
    """`DEBUG=1` turned `10 passed in 1.23s` into `[redacted]0 passed in [redacted].23s`."""
    from app.services.commands import redactable_env_values

    env = {
        "DEBUG": "1",
        "PORT": "3000",
        "NODE_ENV": "development",
        "DATABASE_PASSWORD": "pw",
        "API_TOKEN": "abc",
        "DATABASE_URL": "postgres://u:p@h/db",
        "OPAQUE": "x" * 16,
        "EMPTY": "",
    }

    redacted = set(redactable_env_values(env))

    assert redacted == {"pw", "abc", "postgres://u:p@h/db", "x" * 16}
    assert redact_secrets("10 passed in 1.23s on port 3000\n", *redacted) == (
        "10 passed in 1.23s on port 3000\n"
    )


# --- PATCH must reject an explicit null on a NOT NULL column, not 500 -----------------


@pytest.mark.parametrize(
    "field", ["name", "command", "requires_confirmation", "role", "kind", "enabled"]
)
async def test_update_command_rejects_explicit_null_on_non_nullable_fields(command_context, field):
    """`{"role": null}` used to pass validation (the field is `X | None` so a PATCH can omit
    it), reach `setattr(row, "role", None)`, and blow up as an unhandled IntegrityError — a 500 —
    on commit against the NOT NULL/CHECK-constrained column. It must be a 422 instead."""
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={"name": "Original", "command": "echo hi", "requires_confirmation": False},
        )
        command_id = created.json()["id"]
        response = await client.patch(
            f"/workspaces/{workspace.id}/commands/{command_id}",
            json={field: None},
        )

    assert response.status_code == 422


async def test_update_command_still_allows_omitting_fields(command_context):
    """The null check must not regress the ordinary partial-update case: omitted fields keep
    their stored value, and nullable fields (description, cwd, ...) may still be nulled out."""
    _user, workspace, _service = command_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={
                "name": "Original",
                "command": "echo hi",
                "description": "has a description",
                "requires_confirmation": False,
                "role": "dev",
            },
        )
        command_id = created.json()["id"]
        updated = await client.patch(
            f"/workspaces/{workspace.id}/commands/{command_id}",
            json={"name": "Renamed", "description": None},
        )

    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "Renamed"
    assert body["description"] is None
    # Untouched fields keep their previous value.
    assert body["command"] == "echo hi"
    assert body["role"] == "dev"


async def test_command_run_does_not_require_redis_in_tests(command_context, monkeypatch):
    """`enforce_command_run_rate_limit` talks to Redis directly rather than a dependency this
    fixture already fakes (see the `command_context` override), so a regression there would only
    surface as this test needing a live Redis instead of failing outright."""
    import app.services.redis as redis_module

    def _boom() -> None:
        raise AssertionError("run creation must not need a live Redis in tests")

    monkeypatch.setattr(redis_module, "get_redis_client", _boom)

    _user, workspace, _service = command_context
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/commands",
            json={"name": "Test", "command": "pytest", "requires_confirmation": False},
        )
        command_id = created.json()["id"]
        run = await client.post(f"/workspaces/{workspace.id}/commands/{command_id}/run")

    assert run.status_code == 201


# ---------------------------------------------------------------------------
# Real-DB integration: CommandService.create_command / update_command against the actual
# partial unique index (uq_custom_commands_entry_point). The FakeCommandService tests above
# assert the *contract*; this proves the real clear-then-set transaction actually satisfies the
# database constraint rather than raising an unhandled IntegrityError.
# ---------------------------------------------------------------------------


@pytest.fixture
async def real_db_session():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    settings = get_settings_for_db()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with session_maker() as session:
            await session.begin_nested()
            yield session, session_maker
            await session.rollback()
    await engine.dispose()


def get_settings_for_db():
    from app.core.config import get_settings as _get_settings

    return _get_settings()


async def _make_real_workspace(session) -> tuple[User, Workspace]:
    user = User(
        id=uuid4(),
        email=f"owner-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug=f"demo-{uuid4().hex[:8]}",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    session.add(workspace)
    await session.flush()
    return user, workspace


async def add_workspace_env_var(session, workspace, *, key: str, value: str) -> None:
    cipher = WorkspaceSecretCipher(get_settings_for_db().workspace_env_encryption_key)
    session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=None,
            scope="workspace",
            key=key,
            encrypted_value=cipher.encrypt(value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def test_real_db_creating_a_second_entry_point_clears_the_first_without_integrity_error(
    real_db_session,
):
    session, session_maker = real_db_session
    user, workspace = await _make_real_workspace(session)
    service = CommandService(
        session=session,
        session_maker=session_maker,
        settings=get_settings_for_db(),
        runtime=CommandRuntime(),
    )

    first = await service.create_command(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Web",
        command="pnpm dev",
        description=None,
        icon=None,
        hotkey=None,
        requires_confirmation=False,
        kind="service",
        is_entry_point=True,
    )
    assert first.is_entry_point is True

    second = await service.create_command(
        workspace_id=workspace.id,
        user_id=user.id,
        name="API",
        command="uvicorn app.main:app",
        description=None,
        icon=None,
        hotkey=None,
        requires_confirmation=False,
        kind="service",
        is_entry_point=True,
    )
    assert second.is_entry_point is True

    await session.refresh(first)
    assert first.is_entry_point is False


async def test_real_db_update_clears_the_previous_entry_point_without_integrity_error(
    real_db_session,
):
    session, session_maker = real_db_session
    user, workspace = await _make_real_workspace(session)
    service = CommandService(
        session=session,
        session_maker=session_maker,
        settings=get_settings_for_db(),
        runtime=CommandRuntime(),
    )

    first = await service.create_command(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Web",
        command="pnpm dev",
        description=None,
        icon=None,
        hotkey=None,
        requires_confirmation=False,
        kind="service",
        is_entry_point=True,
    )
    second = await service.create_command(
        workspace_id=workspace.id,
        user_id=user.id,
        name="API",
        command="uvicorn app.main:app",
        description=None,
        icon=None,
        hotkey=None,
        requires_confirmation=False,
        kind="service",
    )

    updated = await service.update_command(
        workspace_id=workspace.id,
        command_id=second.id,
        user_id=user.id,
        values={"is_entry_point": True},
    )
    assert updated.is_entry_point is True

    await session.refresh(first)
    assert first.is_entry_point is False


async def test_real_db_marking_a_task_as_entry_point_raises_clean_error_not_integrity_error(
    real_db_session,
):
    session, session_maker = real_db_session
    user, workspace = await _make_real_workspace(session)
    service = CommandService(
        session=session,
        session_maker=session_maker,
        settings=get_settings_for_db(),
        runtime=CommandRuntime(),
    )

    with pytest.raises(CustomCommandInvalidError):
        await service.create_command(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Lint",
            command="ruff check .",
            description=None,
            icon=None,
            hotkey=None,
            requires_confirmation=False,
            kind="task",
            is_entry_point=True,
        )


# ---------------------------------------------------------------------------
# Real-DB integration: command runs get strict ${ENIGMA_*} substitution too.
# ---------------------------------------------------------------------------


class FakeCommandDaytonaRuntime:
    """Captures the `env` a command run would have received, without touching a sandbox."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def stream_command(
        self,
        workspace,
        session_id,
        command,
        *,
        cwd,
        env,
        timeout,
        on_stdout,
        on_stderr,
    ) -> int:
        self.calls.append({"command": command, "env": env})
        return 0


async def test_real_db_command_run_gets_enigma_exports_in_its_env(real_db_session):
    session, session_maker = real_db_session
    user, workspace = await _make_real_workspace(session)
    daytona_runtime = FakeCommandDaytonaRuntime()
    service = CommandService(
        session=session,
        session_maker=session_maker,
        settings=get_settings_for_db(),
        runtime=CommandRuntime(),
        daytona_runtime=daytona_runtime,
    )
    custom_command = await service.create_command(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Echo",
        command="echo hi",
        description=None,
        icon=None,
        hotkey=None,
        requires_confirmation=False,
    )

    run = await service.create_run(workspace.id, custom_command.id, user.id, confirmed=True)
    task = service.runtime.get_task(run.id)
    assert task is not None
    await task

    assert len(daytona_runtime.calls) == 1
    env = daytona_runtime.calls[0]["env"]
    assert env["ENIGMA_WORKSPACE_ID"] == str(workspace.id)
    assert env["ENIGMA_BRANCH"] == workspace.default_branch

    await session.refresh(run)
    assert run.status == "completed"


async def test_real_db_command_run_with_a_bad_template_fails_the_run_cleanly(real_db_session):
    session, session_maker = real_db_session
    user, workspace = await _make_real_workspace(session)
    daytona_runtime = FakeCommandDaytonaRuntime()
    service = CommandService(
        session=session,
        session_maker=session_maker,
        settings=get_settings_for_db(),
        runtime=CommandRuntime(),
        daytona_runtime=daytona_runtime,
    )
    custom_command = await service.create_command(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Bad template",
        command="echo hi",
        description=None,
        icon=None,
        hotkey=None,
        requires_confirmation=False,
    )
    await add_workspace_env_var(
        session, workspace, key="API_BASE_URL", value="${ENIGMA_PREVIEW_URL:nonexistent}"
    )

    run = await service.create_run(workspace.id, custom_command.id, user.id, confirmed=True)
    task = service.runtime.get_task(run.id)
    assert task is not None
    await task

    # The command process itself must never have been started.
    assert daytona_runtime.calls == []
    await session.refresh(run)
    assert run.status == "error"
    assert "nonexistent" in run.output
