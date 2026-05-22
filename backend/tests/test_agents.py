from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import agents as agents_module
from app.api import auth as auth_module
from app.core.security import hash_password
from app.main import app
from app.models import AgentEvent, AgentRun, User, Workspace
from app.services.agent_tools import DangerousCommandError, WorkspaceToolError, WorkspaceTools
from app.services.agents import AgentRunAlreadyActiveError, AgentRunNotFoundError
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


class FakeAgentService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.runs: dict[UUID, AgentRun] = {}
        self.events: dict[UUID, list[AgentEvent]] = {}
        self.has_active_run = False

    async def create_run(self, workspace_id: UUID, user_id: UUID, prompt: str) -> AgentRun:
        self._ensure_workspace(workspace_id, user_id)
        if self.has_active_run:
            raise AgentRunAlreadyActiveError
        run = self._run(prompt=prompt, status="thinking")
        self.runs[run.id] = run
        self.has_active_run = True
        return run

    async def list_runs(self, workspace_id: UUID, user_id: UUID) -> list[AgentRun]:
        self._ensure_workspace(workspace_id, user_id)
        return sorted(self.runs.values(), key=lambda run: run.started_at, reverse=True)

    async def get_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        self._ensure_workspace(workspace_id, user_id)
        run = self.runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError
        run.events = self.events.get(run.id, [])
        return run

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
    ) -> list[AgentEvent]:
        run = await self.get_run(workspace_id, run_id, user_id)
        return self.events.get(run.id, [])

    async def stop_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        run = await self.get_run(workspace_id, run_id, user_id)
        run.status = "stopped"
        run.completed_at = datetime.now(UTC)
        self.has_active_run = False
        return run

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError

    def _run(self, prompt: str, status: str) -> AgentRun:
        now = datetime.now(UTC)
        return AgentRun(
            id=uuid4(),
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            prompt=prompt,
            status=status,
            provider="mock",
            model="mock-agent",
            started_at=now,
            completed_at=None,
            tokens_used=None,
            error_message=None,
        )


@pytest.fixture
def agent_context(tmp_path):
    user = User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
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
    service = FakeAgentService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[agents_module.get_agent_service] = lambda: service
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_agent_run_endpoints_require_auth(agent_context):
    _user, workspace, _service = agent_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/workspaces/{workspace.id}/agent/runs",
            json={"prompt": "Hello"},
        )

    assert response.status_code == 401


async def test_create_list_detail_and_stop_agent_run(agent_context):
    _user, workspace, service = agent_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/agent/runs",
            json={"prompt": "Inspect this workspace"},
        )
        run_id = created.json()["id"]
        event = AgentEvent(
            id=uuid4(),
            agent_run_id=UUID(run_id),
            event_type="output_text",
            content="hello",
            event_metadata={},
            created_at=datetime.now(UTC),
        )
        service.events[UUID(run_id)] = [event]
        listed = await client.get(f"/workspaces/{workspace.id}/agent/runs")
        detail = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run_id}")
        stopped = await client.post(f"/workspaces/{workspace.id}/agent/runs/{run_id}/stop")

    assert created.status_code == 201
    assert created.json()["status"] == "thinking"
    assert listed.json()[0]["prompt"] == "Inspect this workspace"
    assert detail.json()["events"][0]["content"] == "hello"
    assert stopped.json()["status"] == "stopped"


async def test_one_active_agent_run_per_workspace(agent_context):
    _user, workspace, service = agent_context
    service.has_active_run = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/agent/runs",
            json={"prompt": "Second run"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace already has an active agent run"


def test_workspace_tools_reject_path_traversal_and_dangerous_commands(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "README.md").write_text("hello", encoding="utf-8")
    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        local_path=str(workspace_path),
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    tools = WorkspaceTools(workspace, command_timeout_seconds=1, max_command_output_chars=200)

    assert tools.read_file("README.md") == "hello"
    with pytest.raises(WorkspaceToolError):
        tools.read_file("../outside.txt")
    with pytest.raises(DangerousCommandError):
        tools._validate_command("rm -rf .")
    with pytest.raises(DangerousCommandError):
        tools._validate_command("echo ok && echo nope")
