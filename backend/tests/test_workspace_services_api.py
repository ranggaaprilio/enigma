from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.api import auth as auth_module
from app.api import workspace_services as workspace_services_module
from app.api.rate_limits import enforce_sandbox_rate_limit
from app.core.security import hash_password
from app.main import app
from app.models import User, Workspace, WorkspaceService
from app.services.users import DuplicateUserEmailError
from app.services.workspace_services import (
    WorkspaceServiceLimitError,
    WorkspaceServiceNotFoundError,
    WorkspaceServicePortConflictError,
)
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


def _service_row(
    workspace: Workspace,
    *,
    custom_command_id: UUID | None = None,
    status: str = "stopped",
    port: int | None = None,
    desired: bool = False,
) -> WorkspaceService:
    now = datetime.now(UTC)
    return WorkspaceService(
        id=uuid4(),
        workspace_id=workspace.id,
        custom_command_id=custom_command_id,
        worktree_id=None,
        port=port,
        status=status,
        desired=desired,
        pid=None,
        exit_code=None,
        last_error=None,
        started_at=None,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


class FakeWorkspaceServiceManager:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.rows: dict[UUID, WorkspaceService] = {}
        self.primary = _service_row(workspace, status="stopped")
        self.rows[self.primary.id] = self.primary
        self.limit_error: str | None = None
        self.port_conflict_error: str | None = None

    def add_row(self, row: WorkspaceService) -> None:
        self.rows[row.id] = row

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError

    def _get_row(self, service_id: UUID) -> WorkspaceService:
        row = self.rows.get(service_id)
        if row is None:
            raise WorkspaceServiceNotFoundError
        return row

    async def get_workspace_for(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        self._ensure_workspace(workspace_id, user_id)
        return self.workspace

    async def to_response(self, workspace: Workspace, row: WorkspaceService) -> dict:
        return {
            "id": row.id,
            "workspace_id": row.workspace_id,
            "custom_command_id": row.custom_command_id,
            "name": "Dev Server" if row.custom_command_id is None else "API",
            "role": "dev",
            "kind": "service",
            "command": None if row.custom_command_id is None else "uvicorn app.main:app",
            "port": row.port,
            "status": row.status,
            "desired": row.desired,
            "worktree_id": row.worktree_id,
            "branch": workspace.default_branch,
            "last_error": row.last_error,
            "exit_code": row.exit_code,
            "started_at": row.started_at,
            "last_seen_at": row.last_seen_at,
            "preview_session_id": None,
            "public_url": None,
        }

    async def to_response_list(
        self, workspace: Workspace, rows: list[WorkspaceService]
    ) -> list[dict]:
        return [await self.to_response(workspace, row) for row in rows]

    def environment_from_primary(self, primary: WorkspaceService) -> dict:
        return {"status": "idle", "message": None}

    def max_concurrent_services(self) -> int:
        return 4

    async def list(
        self, workspace_id: UUID, user_id: UUID
    ) -> tuple[list[WorkspaceService], WorkspaceService]:
        self._ensure_workspace(workspace_id, user_id)
        return list(self.rows.values()), self.primary

    async def ensure_running(
        self, workspace_id: UUID, user_id: UUID
    ) -> tuple[list[WorkspaceService], WorkspaceService]:
        return await self.list(workspace_id, user_id)

    async def start(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> tuple[WorkspaceService, str | None]:
        self._ensure_workspace(workspace_id, user_id)
        if self.limit_error:
            raise WorkspaceServiceLimitError(self.limit_error)
        if self.port_conflict_error:
            raise WorkspaceServicePortConflictError(self.port_conflict_error)
        row = self._get_row(service_id)
        row.status = "installing"
        row.desired = True
        row.worktree_id = worktree_id
        return row, None

    async def stop(self, workspace_id: UUID, service_id: UUID, user_id: UUID) -> WorkspaceService:
        self._ensure_workspace(workspace_id, user_id)
        row = self._get_row(service_id)
        row.status = "stopped"
        row.desired = False
        return row

    async def restart(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, worktree_id: UUID | None = None
    ) -> WorkspaceService:
        self._ensure_workspace(workspace_id, user_id)
        row = self._get_row(service_id)
        row.status = "installing"
        row.worktree_id = worktree_id or row.worktree_id
        return row

    async def logs_tail(
        self, workspace_id: UUID, service_id: UUID, user_id: UUID, tail: int
    ) -> str:
        self._ensure_workspace(workspace_id, user_id)
        self._get_row(service_id)
        return "line one\nline two\n"

    async def log_paths(self, workspace_id: UUID, service_id: UUID, user_id: UUID):
        raise NotImplementedError


@pytest.fixture
def workspace_services_context():
    user = User(
        id=uuid4(),
        email="owner@example.com",
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
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    manager = FakeWorkspaceServiceManager(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[enforce_sandbox_rate_limit] = lambda: None
    app.dependency_overrides[workspace_services_module.get_workspace_service_manager] = (
        lambda: manager
    )
    yield user, workspace, manager
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_list_services_requires_auth(workspace_services_context):
    _user, workspace, _manager = workspace_services_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace.id}/services")

    assert response.status_code == 401


async def test_list_services_enforces_ownership(workspace_services_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{uuid4()}/services")

    assert response.status_code == 404


async def test_list_ensure_start_stop_restart_round_trip(workspace_services_context):
    _user, workspace, manager = workspace_services_context
    api_command_id = uuid4()
    api_row = _service_row(workspace, custom_command_id=api_command_id, status="stopped")
    manager.add_row(api_row)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        listed = await client.get(f"/workspaces/{workspace.id}/services")
        ensured = await client.post(f"/workspaces/{workspace.id}/services/ensure")
        started = await client.post(f"/workspaces/{workspace.id}/services/{api_row.id}/start")
        stopped = await client.post(f"/workspaces/{workspace.id}/services/{api_row.id}/stop")
        restarted = await client.post(f"/workspaces/{workspace.id}/services/{api_row.id}/restart")

    assert listed.status_code == 200
    assert len(listed.json()["services"]) == 2
    assert listed.json()["limit"] == 4
    assert ensured.status_code == 200
    assert started.status_code == 200
    assert started.json()["service"]["status"] == "installing"
    assert started.json()["service"]["desired"] is True
    assert stopped.status_code == 200
    assert stopped.json()["service"]["status"] == "stopped"
    assert restarted.status_code == 200
    assert restarted.json()["service"]["status"] == "installing"


async def test_start_unknown_service_returns_404(workspace_services_context):
    _user, workspace, _manager = workspace_services_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{workspace.id}/services/{uuid4()}/start")

    assert response.status_code == 404


async def test_start_reports_the_running_service_cap_as_409(workspace_services_context):
    _user, workspace, manager = workspace_services_context
    manager.limit_error = "Stop a service first (4/4 running)."

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/services/{manager.primary.id}/start"
        )

    assert response.status_code == 409
    assert "4/4" in response.json()["detail"]


async def test_start_reports_a_port_conflict_as_409(workspace_services_context):
    _user, workspace, manager = workspace_services_context
    manager.port_conflict_error = "Port 8000 is already used by API. Stop it first."

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/services/{manager.primary.id}/start"
        )

    assert response.status_code == 409
    assert "8000" in response.json()["detail"]


async def test_get_service_logs_round_trip(workspace_services_context):
    _user, workspace, manager = workspace_services_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(
            f"/workspaces/{workspace.id}/services/{manager.primary.id}/logs"
        )

    assert response.status_code == 200
    assert response.json()["content"] == "line one\nline two\n"


def test_service_logs_websocket_rejects_unauthenticated_connections():
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/workspaces/{uuid4()}/services/{uuid4()}/logs/stream"
        ):
            pass
