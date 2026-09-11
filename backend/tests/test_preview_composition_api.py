"""Epic 13: `GET /workspaces/{id}/preview/composition` -- API-level, dependency-override fakes."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import preview as preview_module
from app.core.security import hash_password
from app.main import app
from app.services.daytona_runtime import DaytonaRuntimeError
from app.services.preview_composition import ComposedService, PreviewComposition
from app.services.workspace_services import WorkspaceServiceWorkspacePathError
from app.services.workspaces import WorkspaceNotFoundError


class FakeUserStore:
    def __init__(self, user=None) -> None:
        self.user = user

    async def get_by_email(self, email: str):
        return self.user if self.user and self.user.email == email else None

    async def get_by_id(self, user_id: UUID):
        return self.user if self.user and self.user.id == user_id else None

    async def register_user(self, email, name, password_hash):
        raise NotImplementedError


class FakeRateLimiter:
    async def check(self, email, request) -> None:
        return None

    async def reset(self, email, request) -> None:
        return None


class FakeCompositionService:
    def __init__(self, composition=None, error: Exception | None = None) -> None:
        self.composition = composition
        self.error = error
        self.calls: list[tuple[UUID, UUID, bool]] = []

    async def compose(self, workspace_id, user_id, *, check_proxy=False):
        self.calls.append((workspace_id, user_id, check_proxy))
        if self.error is not None:
            raise self.error
        return self.composition


def _user():
    from app.models import User

    return User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _composition(workspace_id) -> PreviewComposition:
    service = ComposedService(
        service_id=uuid4(),
        custom_command_id=None,
        name="Dev Server",
        role="dev",
        kind="service",
        port=5173,
        status="running",
        health_status="unknown",
        health_checked_at=None,
        is_entry_point=True,
        entry_point_source="inferred",
        branch="main",
        worktree_id=None,
        preview_session_id=uuid4(),
        capability_url="https://cap.localhost:8001/",
        capability_url_rotates=True,
        alias=None,
        alias_url=None,
        alias_prime_url=None,
        localhost_url="http://127.0.0.1:5173",
        diagnoses=[],
    )
    return PreviewComposition(
        workspace_id=workspace_id,
        preview_alias_enabled=False,
        proxy_base_url="http://localhost:8001",
        entry_point_service_id=service.service_id,
        services=[service],
        advisories=[],
    )


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


@pytest.fixture
def composition_context():
    user = _user()
    workspace_id = uuid4()
    fake_service = FakeCompositionService(composition=_composition(workspace_id))

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[preview_module.get_preview_composition_service] = (
        lambda: fake_service
    )
    yield user, workspace_id, fake_service
    app.dependency_overrides.clear()


async def test_composition_endpoint_returns_the_composed_shape(composition_context):
    _user, workspace_id, fake_service = composition_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{workspace_id}/preview/composition")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == str(workspace_id)
    assert len(body["services"]) == 1
    assert body["services"][0]["is_entry_point"] is True
    assert body["entry_point_service_id"] == body["services"][0]["service_id"]
    assert fake_service.calls[0][2] is False  # check_proxy defaults to False


async def test_composition_endpoint_forwards_check_proxy_query_param(composition_context):
    _user, workspace_id, fake_service = composition_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        await client.get(
            f"/workspaces/{workspace_id}/preview/composition", params={"check_proxy": "true"}
        )

    assert fake_service.calls[-1][2] is True


async def test_composition_endpoint_maps_workspace_not_found_to_404(composition_context):
    _user, workspace_id, fake_service = composition_context
    fake_service.error = WorkspaceNotFoundError()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{workspace_id}/preview/composition")

    assert response.status_code == 404


async def test_composition_endpoint_maps_sandbox_not_ready_to_409(composition_context):
    _user, workspace_id, fake_service = composition_context
    fake_service.error = WorkspaceServiceWorkspacePathError()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{workspace_id}/preview/composition")

    assert response.status_code == 409


async def test_composition_endpoint_maps_runtime_error_to_503(composition_context):
    _user, workspace_id, fake_service = composition_context
    fake_service.error = DaytonaRuntimeError("sandbox unreachable")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{workspace_id}/preview/composition")

    assert response.status_code == 503


async def test_composition_endpoint_requires_auth(composition_context):
    _user, workspace_id, _fake_service = composition_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace_id}/preview/composition")

    assert response.status_code == 401


async def test_alias_url_stability_property_survives_json_round_trip(composition_context):
    """The stability property (alias_url populated while capability_url reflects run state)
    must survive the response schema's validation unchanged."""
    _user, workspace_id, fake_service = composition_context
    composition = fake_service.composition
    stable_alias_url = "https://demo-dev-abc123.localhost:8001/"
    composition.services[0] = ComposedService(
        **{
            **composition.services[0].__dict__,
            "status": "stopped",
            "capability_url": None,
            "alias_url": stable_alias_url,
        },
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{workspace_id}/preview/composition")

    body = response.json()
    assert body["services"][0]["capability_url"] is None
    assert body["services"][0]["alias_url"] == stable_alias_url
