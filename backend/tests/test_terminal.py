from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.api import auth as auth_module
from app.api import terminal as terminal_module
from app.core.security import hash_password
from app.main import app
from app.models import TerminalSession, User, Workspace
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


class FakeTerminalService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.sessions: dict[UUID, TerminalSession] = {}
        self.stopped: list[UUID] = []

    async def create_or_reuse(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
    ) -> TerminalSession:
        self._ensure_workspace(workspace_id, user_id)
        active = next(
            (session for session in self.sessions.values() if session.status == "active"),
            None,
        )
        if active is not None:
            active.name = name
            return active
        now = datetime.now(UTC)
        terminal_session = TerminalSession(
            id=uuid4(),
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            name=name,
            pty_session_id=f"execute-{self.workspace.id.hex}",
            status="active",
            created_at=now,
            last_active_at=now,
        )
        self.sessions[terminal_session.id] = terminal_session
        return terminal_session

    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[TerminalSession]:
        self._ensure_workspace(workspace_id, user_id)
        return list(self.sessions.values())

    async def stop(self, workspace_id: UUID, terminal_session_id: UUID, user_id: UUID) -> None:
        self._ensure_workspace(workspace_id, user_id)
        terminal_session = self.sessions[terminal_session_id]
        terminal_session.status = "stopped"
        self.stopped.append(terminal_session_id)

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError


@pytest.fixture
def terminal_context(tmp_path):
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
    service = FakeTerminalService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[terminal_module.get_terminal_service] = lambda: service
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_terminal_session_endpoints_require_auth(terminal_context):
    _user, workspace, _service = terminal_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/workspaces/{workspace.id}/terminal/sessions")

    assert response.status_code == 401


async def test_create_list_and_delete_terminal_session(terminal_context):
    _user, workspace, service = terminal_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/terminal/sessions",
            json={"name": "Shell"},
        )
        reused = await client.post(f"/workspaces/{workspace.id}/terminal/sessions")
        listed = await client.get(f"/workspaces/{workspace.id}/terminal/sessions")
        deleted = await client.delete(
            f"/workspaces/{workspace.id}/terminal/sessions/{created.json()['id']}",
        )

    assert created.status_code == 201
    assert created.json()["name"] == "Shell"
    assert created.json()["pty_session_id"] == f"execute-{workspace.id.hex}"
    assert reused.json()["id"] == created.json()["id"]
    assert listed.json()[0]["status"] == "active"
    assert deleted.json() == {"ok": True}
    assert UUID(created.json()["id"]) in service.stopped


async def test_terminal_workspace_ownership_is_enforced(terminal_context):
    _user, _workspace, _service = terminal_context
    alien_workspace_id = uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{alien_workspace_id}/terminal/sessions")

    assert response.status_code == 404


def test_terminal_websocket_rejects_unauthenticated_connections():
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/workspaces/{uuid4()}/terminal/sessions/{uuid4()}"):
            pass


# ---------------------------------------------------------------------------
# Real-DB: TerminalService.environment() is non-strict -- the one Epic 13 exception where an
# unresolvable ${ENIGMA_*} template is omitted (not emptied) and reported, never fatal.
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with session_maker() as session:
            await session.begin_nested()
            yield session
            await session.rollback()
    await engine.dispose()


async def _make_workspace_for_env_test(session) -> Workspace:
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
        daytona_sandbox_id=f"sandbox-{uuid4().hex[:8]}",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    session.add(workspace)
    await session.flush()
    return workspace


async def test_terminal_environment_omits_an_unresolvable_template_and_reports_it(db_session):
    from app.core.config import get_settings
    from app.models import WorkspaceEnvVar
    from app.services.terminal import TerminalService
    from app.services.workspaces import WorkspaceSecretCipher

    workspace = await _make_workspace_for_env_test(db_session)
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=None,
            scope="workspace",
            key="GOOD",
            encrypted_value=cipher.encrypt("plain-value"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=None,
            scope="workspace",
            key="BAD",
            encrypted_value=cipher.encrypt("${ENIGMA_PREVIEW_URL:nonexistent}"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    service = TerminalService(db_session, get_settings())
    env, problems = await service.environment(workspace.id, workspace)

    assert env["GOOD"] == "plain-value"
    assert "BAD" not in env  # omitted, never emptied
    assert env["ENIGMA_WORKSPACE_ID"] == str(workspace.id)
    assert len(problems) == 1
    assert problems[0].reason == "unknown_service"
    assert "nonexistent" in problems[0].message
