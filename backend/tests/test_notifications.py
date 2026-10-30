from datetime import UTC, datetime
from uuid import UUID, uuid4

from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import notifications as notifications_module
from app.core.security import hash_password
from app.main import app
from app.models import AuditLog, Notification, User, Workspace
from app.services.notifications import NotificationNotFoundError
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


class FakeNotificationService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.notification = Notification(
            id=uuid4(),
            user_id=user.id,
            workspace_id=workspace.id,
            notification_type="agent_run_completed",
            title="Agent run completed",
            message="The agent run completed successfully.",
            read_at=None,
            created_at=datetime.now(UTC),
        )

    async def list_for_user(self, user_id: UUID, limit: int = 50) -> list[Notification]:
        return [self.notification] if self.notification.user_id == user_id else []

    async def mark_read(self, notification_id: UUID, user_id: UUID) -> Notification:
        if self.notification.id != notification_id or self.notification.user_id != user_id:
            raise NotificationNotFoundError
        self.notification.read_at = datetime.now(UTC)
        return self.notification

    async def mark_all_read(self, user_id: UUID) -> list[Notification]:
        if self.notification.user_id == user_id:
            self.notification.read_at = datetime.now(UTC)
            return [self.notification]
        return []


class FakeAuditLogService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace

    async def list_for_workspace(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 100,
    ) -> list[AuditLog]:
        if workspace_id != self.workspace.id or user_id != self.user.id:
            raise WorkspaceNotFoundError
        return [
            AuditLog(
                id=uuid4(),
                user_id=user_id,
                workspace_id=workspace_id,
                action="repo_commit",
                target_type="repository",
                target_id=str(workspace_id),
                log_metadata={"message": "Update README"},
                created_at=datetime.now(UTC),
            )
        ]


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_notification_endpoints_require_auth():
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/notifications")

    app.dependency_overrides.clear()
    assert response.status_code == 401


async def test_user_can_list_and_mark_notifications_read():
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
        sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = FakeNotificationService(user, workspace)
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[notifications_module.get_notification_service] = lambda: service

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        listed = await client.get("/notifications")
        notification_id = listed.json()[0]["id"]
        marked = await client.patch(f"/notifications/{notification_id}/read")
        read_all = await client.post("/notifications/read-all")

    app.dependency_overrides.clear()
    assert listed.status_code == 200
    assert listed.json()[0]["type"] == "agent_run_completed"
    assert marked.json()["read_at"] is not None
    assert read_all.json()[0]["read_at"] is not None


async def test_audit_logs_are_scoped_to_workspace_owner():
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
        sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[notifications_module.get_audit_log_service] = lambda: (
        FakeAuditLogService(user, workspace)
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        allowed = await client.get(f"/workspaces/{workspace.id}/audit-logs")
        denied = await client.get(f"/workspaces/{uuid4()}/audit-logs")

    app.dependency_overrides.clear()
    assert allowed.status_code == 200
    assert allowed.json()[0]["action"] == "repo_commit"
    assert denied.status_code == 404
