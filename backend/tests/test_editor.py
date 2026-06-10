from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import editor as editor_module
from app.core.config import get_settings
from app.core.security import hash_password
from app.main import app
from app.models import EditorSession, User, Workspace
from app.services.editor import EditorService, EditorSessionNotFoundError
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


class FakeEditorService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.session: EditorSession | None = None
        self.stopped = False

    async def start_or_reuse(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        self._ensure_workspace(workspace_id, user_id)
        if self.session is None or self.session.status != "active":
            self.session = self._session("active")
        return self.session

    async def status(self, workspace_id: UUID, user_id: UUID) -> EditorSession | None:
        self._ensure_workspace(workspace_id, user_id)
        return self.session

    async def stop(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        self._ensure_workspace(workspace_id, user_id)
        if self.session is None:
            raise EditorSessionNotFoundError
        self.stopped = True
        self.session.status = "stopped"
        return self.session

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError

    def _session(self, status: str) -> EditorSession:
        now = datetime.now(UTC)
        editor_session = EditorSession(
            id=uuid4(),
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            url="http://localhost:8080/?folder=/workspace/demo",
            status=status,
            process_id=1234,
            bind_host="127.0.0.1",
            bind_port=13337,
            token_hash="token-hash",
            token_expires_at=now + timedelta(hours=1),
            started_at=now,
            last_active_at=now,
        )
        return editor_session


@pytest.fixture
def editor_context(tmp_path):
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
    service = FakeEditorService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[editor_module.get_editor_service] = lambda: service
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_editor_endpoints_require_auth(editor_context):
    _user, workspace, _service = editor_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/workspaces/{workspace.id}/editor/start")

    assert response.status_code == 401


async def test_start_status_and_stop_editor_session(editor_context):
    _user, workspace, service = editor_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        inactive = await client.get(f"/workspaces/{workspace.id}/editor/status")
        created = await client.post(f"/workspaces/{workspace.id}/editor/start")
        status_response = await client.get(f"/workspaces/{workspace.id}/editor/status")
        stopped = await client.post(f"/workspaces/{workspace.id}/editor/stop")

    assert inactive.json() == {"status": "inactive", "session": None}
    assert created.status_code == 201
    assert created.json()["status"] == "active"
    assert created.json()["url"] == "http://localhost:8080/?folder=/workspace/demo"
    assert status_response.json()["status"] == "active"
    assert stopped.json()["status"] == "stopped"
    assert service.stopped is True


def test_relative_workspace_path_resolves_to_sandbox_home():
    service = EditorService.__new__(EditorService)
    service.settings = get_settings()

    folder = service._editor_url("https://sandbox.proxy.daytona.example", "workspace")
    command = service._ensure_command("workspace")

    assert folder.endswith("?folder=/home/daytona/workspace")
    assert "/home/daytona/workspace" in command


def test_absolute_workspace_path_is_left_untouched():
    service = EditorService.__new__(EditorService)
    service.settings = get_settings()

    folder = service._editor_url("https://sandbox.proxy.daytona.example?x=1", "/srv/code")

    assert folder.endswith("&folder=/srv/code")


async def test_editor_workspace_ownership_is_enforced(editor_context):
    _user, _workspace, _service = editor_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{uuid4()}/editor/start")

    assert response.status_code == 404
