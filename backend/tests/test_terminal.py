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
from app.services.terminal import TerminalRuntimeError, TerminalService, TerminalWorkspacePathError
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
            tmux_session_name=f"execute-{self.workspace.id.hex}",
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
        local_path=str(workspace_path),
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


def test_terminal_service_validates_workspace_path(tmp_path):
    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        local_path=str(tmp_path / "missing"),
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = TerminalService(session=None, workspace_root=tmp_path, tmux_binary="tmux")  # type: ignore[arg-type]

    with pytest.raises(TerminalWorkspacePathError):
        service._workspace_path(workspace)


def test_terminal_tmux_session_name_is_safe(tmp_path):
    service = TerminalService(session=None, workspace_root=tmp_path, tmux_binary="tmux")  # type: ignore[arg-type]
    workspace_id = uuid4()

    assert service._tmux_session_name(workspace_id) == f"execute-{workspace_id.hex}"


async def test_terminal_service_creates_detached_tmux_session_after_missing_check(tmp_path):
    service = TerminalService(session=None, workspace_root=tmp_path, tmux_binary="tmux")  # type: ignore[arg-type]
    terminal_session = TerminalSession(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=uuid4(),
        name="Terminal",
        tmux_session_name="execute-safe",
        status="active",
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
    )
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> None:
        calls.append(args)
        if args[0] == "has-session":
            raise TerminalRuntimeError("can't find session: execute-safe")

    service._run_tmux = fake_run_tmux  # type: ignore[method-assign]

    await service._ensure_tmux_session(terminal_session, tmp_path)

    assert calls == [
        ("has-session", "-t", "execute-safe"),
        ("new-session", "-d", "-s", "execute-safe", "-c", str(tmp_path)),
        ("set-option", "-t", "execute-safe", "mouse", "on"),
        ("set-option", "-t", "execute-safe", "history-limit", "10000"),
    ]


async def test_terminal_service_treats_missing_tmux_socket_as_missing_session(tmp_path):
    service = TerminalService(session=None, workspace_root=tmp_path, tmux_binary="tmux")  # type: ignore[arg-type]
    terminal_session = TerminalSession(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=uuid4(),
        name="Terminal",
        tmux_session_name="execute-safe",
        status="active",
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
    )
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> None:
        calls.append(args)
        if args[0] == "has-session":
            raise TerminalRuntimeError(
                "error connecting to /tmp/tmux-0/default (No such file or directory)"
            )

    service._run_tmux = fake_run_tmux  # type: ignore[method-assign]

    await service._ensure_tmux_session(terminal_session, tmp_path)

    assert calls == [
        ("has-session", "-t", "execute-safe"),
        ("new-session", "-d", "-s", "execute-safe", "-c", str(tmp_path)),
        ("set-option", "-t", "execute-safe", "mouse", "on"),
        ("set-option", "-t", "execute-safe", "history-limit", "10000"),
    ]


async def test_terminal_service_does_not_create_when_tmux_session_exists(tmp_path):
    service = TerminalService(session=None, workspace_root=tmp_path, tmux_binary="tmux")  # type: ignore[arg-type]
    terminal_session = TerminalSession(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=uuid4(),
        name="Terminal",
        tmux_session_name="execute-existing",
        status="active",
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
    )
    calls: list[tuple[str, ...]] = []

    async def fake_run_tmux(*args: str) -> None:
        calls.append(args)

    service._run_tmux = fake_run_tmux  # type: ignore[method-assign]

    await service._ensure_tmux_session(terminal_session, tmp_path)

    assert calls == [
        ("has-session", "-t", "execute-existing"),
        ("set-option", "-t", "execute-existing", "mouse", "on"),
        ("set-option", "-t", "execute-existing", "history-limit", "10000"),
    ]


def test_terminal_websocket_rejects_unauthenticated_connections():
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/workspaces/{uuid4()}/terminal/sessions/{uuid4()}"):
            pass
