from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import preview as preview_module
from app.core.config import Settings
from app.core.security import hash_password
from app.main import app
from app.models import PreviewSession, User, Workspace
from app.services.preview import PreviewNotFoundError, PreviewService, PreviewSessionNotFoundError
from app.services.preview_capability import (
    capability_from_host,
    create_preview_proxy_url,
    decode_preview_capability,
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


class FakePreviewService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.session: PreviewSession | None = None

    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[PreviewSession]:
        self._ensure_workspace(workspace_id, user_id)
        return [self.session] if self.session else []

    async def detect(self, workspace_id: UUID, user_id: UUID) -> PreviewSession:
        self._ensure_workspace(workspace_id, user_id)
        if self.session is None:
            self.session = self._session("active")
        return self.session

    async def refresh(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        preview_session = self._ensure_session(workspace_id, preview_session_id, user_id)
        preview_session.last_seen_at = datetime.now(UTC) + timedelta(seconds=1)
        preview_session.status = "active"
        return preview_session

    async def stop(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        preview_session = self._ensure_session(workspace_id, preview_session_id, user_id)
        preview_session.status = "stopped"
        return preview_session

    async def get_proxy_session(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        preview_session = self._ensure_session(workspace_id, preview_session_id, user_id)
        if preview_session.status != "active":
            raise PreviewSessionNotFoundError
        return preview_session

    def upstream_http_url(self, preview_session: PreviewSession, path: str) -> str:
        return f"http://127.0.0.1:{preview_session.port}/{path}"

    def upstream_ws_url(self, preview_session: PreviewSession, path: str) -> str:
        return f"ws://127.0.0.1:{preview_session.port}/{path}"

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError

    def _ensure_session(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        self._ensure_workspace(workspace_id, user_id)
        if self.session is None or self.session.id != preview_session_id:
            raise PreviewSessionNotFoundError
        return self.session

    def _session(self, status: str) -> PreviewSession:
        now = datetime.now(UTC)
        preview_session = PreviewSession(
            id=uuid4(),
            workspace_id=self.workspace.id,
            port=5173,
            public_url="",
            status=status,
            created_at=now,
            last_seen_at=now,
        )
        preview_session.public_url = "http://signed-capability.localhost:8001/"
        return preview_session


@pytest.fixture
def preview_context(tmp_path):
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
    service = FakePreviewService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[preview_module.get_preview_service] = lambda: service
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_preview_endpoints_require_auth(preview_context):
    _user, workspace, _service = preview_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/workspaces/{workspace.id}/preview/detect")

    assert response.status_code == 401


async def test_preview_workspace_ownership_is_enforced(preview_context):
    _user, _workspace, _service = preview_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{uuid4()}/preview/detect")

    assert response.status_code == 404


async def test_detect_list_refresh_and_stop_preview_session(preview_context):
    _user, workspace, service = preview_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        detected = await client.post(f"/workspaces/{workspace.id}/preview/detect")
        listed = await client.get(f"/workspaces/{workspace.id}/preview/sessions")
        session_id = detected.json()["session"]["id"]
        refreshed = await client.post(
            f"/workspaces/{workspace.id}/preview/sessions/{session_id}/refresh"
        )
        stopped = await client.post(
            f"/workspaces/{workspace.id}/preview/sessions/{session_id}/stop"
        )

    assert detected.json()["status"] == "active"
    assert detected.json()["session"]["port"] == 5173
    assert detected.json()["session"]["public_url"].endswith(".localhost:8001/")
    assert listed.json()[0]["id"] == session_id
    assert refreshed.json()["status"] == "active"
    assert stopped.json()["status"] == "stopped"
    assert service.session is not None


async def test_detect_returns_inactive_when_no_preview_is_found(preview_context):
    _user, workspace, service = preview_context

    async def no_preview(workspace_id: UUID, user_id: UUID) -> PreviewSession:
        service._ensure_workspace(workspace_id, user_id)
        raise PreviewNotFoundError

    service.detect = no_preview  # type: ignore[method-assign]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{workspace.id}/preview/detect")

    assert response.json() == {"status": "inactive", "session": None}


async def test_refresh_marks_unreachable_session_error(preview_context):
    _user, workspace, service = preview_context
    service.session = service._session("active")

    async def failed_refresh(
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        preview_session = service._ensure_session(workspace_id, preview_session_id, user_id)
        preview_session.status = "error"
        preview_session.last_seen_at = datetime.now(UTC) + timedelta(seconds=1)
        return preview_session

    service.refresh = failed_refresh  # type: ignore[method-assign]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/preview/sessions/{service.session.id}/refresh"
        )

    assert response.json()["status"] == "error"


async def test_preview_branch_resolution():
    """A preview's branch is resolved from its worktree; base/unknown → default branch."""
    from app.models import WorkspaceWorktree

    class _Session:
        def __init__(self, workspace, worktree):
            self.workspace = workspace
            self.worktree = worktree

        async def get(self, model, pk):
            if self.workspace.id == pk:
                return self.workspace
            if self.worktree is not None and self.worktree.id == pk:
                return self.worktree
            return None

    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
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
    worktree = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=workspace.id,
        branch="feature-x",
        path="worktrees/feature-x",
        base_ref="main",
        is_base=False,
        status="active",
    )
    service = PreviewService(
        session=_Session(workspace, worktree),  # type: ignore[arg-type]
        settings=Settings(
            auth_secret_key="preview-secret",
            daytona_preview_proxy_base_url="http://localhost:8001",
        ),
        runtime=object(),  # type: ignore[arg-type]
    )

    assert await service._branch_for(workspace, None) == "main"
    assert await service._branch_for(workspace, worktree.id) == "feature-x"
    # A worktree that no longer exists falls back to the base clone's branch.
    assert await service._branch_for(workspace, uuid4()) == "main"


def test_preview_service_reuses_a_valid_proxy_url(preview_context):
    _user, _workspace, fake_service = preview_context
    preview = fake_service._session("active")
    service = PreviewService(
        session=None,  # type: ignore[arg-type]
        settings=Settings(
            auth_secret_key="preview-secret",
            daytona_preview_proxy_base_url="http://localhost:8001",
        ),
        runtime=object(),  # type: ignore[arg-type]
    )

    first_url = service._proxy_url(preview)
    preview.public_url = first_url

    assert service._proxy_url(preview) == first_url


def test_preview_service_re_mints_a_proxy_url_inside_the_renewal_window(preview_context):
    """Epic 07: the signed URL is refreshed *before* it expires. Within the last minute of its
    life `_proxy_url` mints a new one, which the panel's 15s poll swaps into the iframe."""
    _user, _workspace, fake_service = preview_context
    preview = fake_service._session("active")
    settings = Settings(
        auth_secret_key="preview-secret",
        daytona_preview_proxy_base_url="http://localhost:8001",
    )
    service = PreviewService(
        session=None,  # type: ignore[arg-type]
        settings=settings,
        runtime=object(),  # type: ignore[arg-type]
    )
    ttl = min(settings.daytona_preview_proxy_ttl_seconds, settings.daytona_preview_url_ttl_seconds)
    # Minted long enough ago that only 30 seconds of its life remain.
    preview.public_url = create_preview_proxy_url(
        preview.id,
        preview.port,
        base_url=settings.daytona_preview_proxy_base_url,
        secret_key=settings.auth_secret_key,
        ttl_seconds=ttl,
        now=datetime.now(UTC) - timedelta(seconds=ttl - 30),
    )

    renewed = service._proxy_url(preview)

    assert renewed != preview.public_url
    capability = decode_preview_capability(
        capability_from_host(
            urlsplit(renewed).netloc, base_url=settings.daytona_preview_proxy_base_url
        ),
        secret_key=settings.auth_secret_key,
    )
    assert capability.expires_at - datetime.now(UTC) > timedelta(seconds=ttl - 60)
