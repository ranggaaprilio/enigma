from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import commands as commands_module
from app.core.config import Settings
from app.core.security import hash_password
from app.main import app
from app.models import CommandRun, CustomCommand, User, Workspace
from app.services.agent_tools import DangerousCommandError
from app.services.commands import (
    CommandConfirmationRequiredError,
    CommandRuntime,
    CommandService,
    CustomCommandNotFoundError,
    validate_custom_command,
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
    ) -> CustomCommand:
        self._ensure_workspace(workspace_id, user_id)
        now = datetime.now(UTC)
        custom_command = CustomCommand(
            id=uuid4(),
            workspace_id=workspace_id,
            name=name,
            command=command,
            description=description,
            icon=icon,
            hotkey=hotkey,
            requires_confirmation=requires_confirmation,
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
        for key, value in values.items():
            setattr(custom_command, key, value)
        custom_command.updated_at = datetime.now(UTC)
        return custom_command

    async def delete_command(self, workspace_id: UUID, command_id: UUID, user_id: UUID) -> None:
        self._ensure_workspace(workspace_id, user_id)
        if command_id not in self.commands:
            raise CustomCommandNotFoundError
        del self.commands[command_id]

    async def create_run(
        self,
        workspace_id: UUID,
        command_id: UUID,
        user_id: UUID,
        confirmed: bool,
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
        local_path=str(workspace_path),
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = FakeCommandService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[commands_module.get_command_service] = lambda: service
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


def test_validate_custom_command_restrictions_and_confirmed_tools():
    assert validate_custom_command(
        "pytest --version",
        requires_confirmation=False,
        confirmed=False,
    ) == ["pytest", "--version"]
    assert validate_custom_command(
        "pnpm test",
        requires_confirmation=True,
        confirmed=True,
    ) == ["pnpm", "test"]

    with pytest.raises(DangerousCommandError):
        validate_custom_command(
            "echo ok && echo nope",
            requires_confirmation=False,
            confirmed=False,
        )
    with pytest.raises(DangerousCommandError):
        validate_custom_command("pnpm test", requires_confirmation=True, confirmed=False)
    with pytest.raises(DangerousCommandError):
        validate_custom_command("/bin/echo ok", requires_confirmation=False, confirmed=False)


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
    process = await asyncio_subprocess(["python", "-c", "print('abcdef')"], tmp_path)

    exit_code = await service._stream_process_output(FakeSession(), run, process)  # type: ignore[arg-type]
    await service._mark_finished(FakeSession(), run, "completed", exit_code)  # type: ignore[arg-type]

    assert exit_code == 0
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
    process = await asyncio_subprocess(["python", "-c", "raise SystemExit(7)"], tmp_path)

    exit_code = await service._stream_process_output(FakeSession(), run, process)  # type: ignore[arg-type]
    await service._mark_finished(FakeSession(), run, "failed", exit_code)  # type: ignore[arg-type]

    assert exit_code == 7
    assert run.status == "failed"
    assert run.exit_code == 7


async def asyncio_subprocess(argv: list[str], cwd: Path):
    import asyncio

    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
