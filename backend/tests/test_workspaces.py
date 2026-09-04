from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import workspaces as workspaces_module
from app.api.workspace_support import workspace_response
from app.core.config import Settings
from app.core.security import hash_password
from app.main import app
from app.models import User, Workspace, WorkspaceEnvVar
from app.services.users import DuplicateUserEmailError
from app.services.workspaces import (
    InvalidEnvVarKeyError,
    SqlAlchemyWorkspaceService,
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspaceSecretCipher,
    validate_env_key,
)


class FakeUserStore:
    def __init__(self, user: User | None = None) -> None:
        self.user = user

    async def get_by_email(self, email: str) -> User | None:
        return self.user if self.user and self.user.email == email else None

    async def get_by_id(self, user_id: UUID) -> User | None:
        return self.user if self.user and self.user.id == user_id else None

    async def register_user(self, email: str, name: str | None, password_hash: str) -> User:
        raise DuplicateUserEmailError


class FakeWorkspaceService:
    def __init__(self, workspace_root: Path, cipher: WorkspaceSecretCipher) -> None:
        self.workspace_root = workspace_root
        self.cipher = cipher
        self.workspaces: dict[UUID, Workspace] = {}
        self.env_vars: dict[UUID, list[WorkspaceEnvVar]] = {}
        self.encrypted_values: dict[tuple[UUID, str], str] = {}

    async def list_for_owner(self, owner_id: UUID) -> list[Workspace]:
        return [
            workspace for workspace in self.workspaces.values() if workspace.owner_id == owner_id
        ]

    async def get_for_owner(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace

    async def create(
        self,
        owner_id: UUID,
        name: str,
        repo_url: str,
        default_branch: str,
        snapshot: str | None = None,
    ) -> Workspace:
        if await self.list_for_owner(owner_id):
            raise WorkspaceAlreadyExistsError

        now = datetime.now(UTC)
        workspace_id = uuid4()
        workspace = Workspace(
            id=workspace_id,
            owner_id=owner_id,
            name=name,
            slug=name.lower().replace(" ", "-"),
            repo_url=repo_url,
            default_branch=default_branch,
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-{workspace_id}",
            snapshot=snapshot,
            workspace_path="/workspace",
            status="ready",
            created_at=now,
            updated_at=now,
        )
        self.workspaces[workspace.id] = workspace
        return workspace

    async def update(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        name: str | None = None,
        repo_url: str | None = None,
        default_branch: str | None = None,
        snapshot: str | None = None,
    ) -> Workspace:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        if name is not None:
            workspace.name = name
            workspace.slug = name.lower().replace(" ", "-")
        if repo_url is not None:
            workspace.repo_url = repo_url
        if default_branch is not None:
            workspace.default_branch = default_branch
        if snapshot is not None:
            workspace.snapshot = snapshot
        workspace.updated_at = datetime.now(UTC)
        return workspace

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None:
        await self.get_for_owner(workspace_id, owner_id)
        del self.workspaces[workspace_id]

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[WorkspaceEnvVar]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        return self.env_vars.get(workspace.id, [])

    async def upsert_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        value: str,
    ) -> WorkspaceEnvVar:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        normalized_key = validate_env_key(key)
        encrypted_value = self.cipher.encrypt(value)
        self.encrypted_values[(workspace.id, normalized_key)] = encrypted_value
        now = datetime.now(UTC)
        env_var = WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            key=normalized_key,
            encrypted_value=encrypted_value,
            created_at=now,
            updated_at=now,
        )
        self.env_vars[workspace.id] = [
            existing
            for existing in self.env_vars.get(workspace.id, [])
            if existing.key != normalized_key
        ]
        self.env_vars[workspace.id].append(env_var)
        return env_var

    async def delete_env_var(self, workspace_id: UUID, owner_id: UUID, key: str) -> None:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        normalized_key = validate_env_key(key)
        self.env_vars[workspace.id] = [
            existing
            for existing in self.env_vars.get(workspace.id, [])
            if existing.key != normalized_key
        ]


class FakeRateLimiter:
    async def check(self, email: str, request) -> None:
        return None

    async def reset(self, email: str, request) -> None:
        return None


@pytest.fixture
def workspace_context(tmp_path):
    user = User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    settings = Settings(workspace_root=tmp_path)
    service = FakeWorkspaceService(
        workspace_root=tmp_path,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
    )

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[workspaces_module.get_workspace_service] = lambda: service
    yield user, service, tmp_path
    app.dependency_overrides.clear()


async def test_workspace_creation_requires_auth():
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/workspaces",
            json={"name": "Demo", "repo_url": "https://github.com/example/demo"},
        )

    app.dependency_overrides.clear()
    assert response.status_code == 401


async def test_user_can_create_one_workspace_and_directory(workspace_context):
    _user, _service, tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "password"},
        )
        first = await client.post(
            "/workspaces",
            json={
                "name": "Demo Workspace",
                "repo_url": "https://github.com/example/demo",
                "default_branch": "main",
            },
        )
        second = await client.post(
            "/workspaces",
            json={"name": "Other", "repo_url": "https://github.com/example/other"},
        )

    assert first.status_code == 201
    payload = first.json()
    assert payload["status"] == "ready"
    assert payload["runtime"] == "daytona"
    assert payload["sandbox_id"].startswith("sandbox-")
    assert payload["workspace_path"] == "/workspace"
    assert second.status_code == 409


async def test_workspace_ownership_checks_apply_to_detail_status_and_env_vars(workspace_context):
    user, service, _tmp_path = workspace_context
    alien_workspace = await service.create(
        owner_id=uuid4(),
        name="Alien",
        repo_url="https://github.com/example/alien",
        default_branch="main",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        detail = await client.get(f"/workspaces/{alien_workspace.id}")
        status_response = await client.get(f"/workspaces/{alien_workspace.id}/status")
        env_response = await client.put(
            f"/workspaces/{alien_workspace.id}/env-vars",
            json={"key": "SECRET_TOKEN", "value": "raw-secret"},
        )

    assert detail.status_code == 404
    assert status_response.status_code == 404
    assert env_response.status_code == 404


async def test_env_vars_are_encrypted_masked_and_validated(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace = await client.post(
            "/workspaces",
            json={"name": "Secrets", "repo_url": "https://github.com/example/secrets"},
        )
        workspace_id = workspace.json()["id"]
        invalid = await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "bad-key", "value": "raw-secret"},
        )
        created = await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "secret_token", "value": "raw-secret"},
        )
        listed = await client.get(f"/workspaces/{workspace_id}/env-vars")

    assert invalid.status_code == 400
    assert created.status_code == 200
    assert created.json()["key"] == "SECRET_TOKEN"
    assert created.json()["masked_value"] == "********"
    assert "raw-secret" not in str(created.json())
    assert listed.json()[0]["masked_value"] == "********"

    encrypted = service.encrypted_values[(UUID(workspace_id), "SECRET_TOKEN")]
    assert encrypted != "raw-secret"
    assert service.cipher.decrypt(encrypted) == "raw-secret"


def test_invalid_env_key_is_rejected():
    with pytest.raises(InvalidEnvVarKeyError):
        validate_env_key("bad-key")


async def test_sqlalchemy_workspace_create_refreshes_before_return(tmp_path):
    class EmptyResult:
        def scalar_one_or_none(self):
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[Workspace] = []
            self.commits = 0
            self.refreshed: list[Workspace] = []

        async def execute(self, statement):
            return EmptyResult()

        def add(self, workspace):
            self.added.append(workspace)

        async def flush(self):
            self.added[-1].id = uuid4()

        async def commit(self):
            self.commits += 1

        async def refresh(self, workspace):
            self.refreshed.append(workspace)
            now = datetime.now(UTC)
            workspace.created_at = workspace.created_at or now
            workspace.updated_at = now

    settings = Settings()
    session = FakeSession()

    class FakeRuntime:
        def __init__(self, runtime_settings):
            self.settings = runtime_settings

        async def provision(self, workspace_id, owner_id, snapshot):
            return type("Sandbox", (), {"id": f"sandbox-{workspace_id}"})()

    service = SqlAlchemyWorkspaceService(
        session=session,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=FakeRuntime(settings),  # type: ignore[arg-type]
    )

    workspace = await service.create(
        owner_id=uuid4(),
        name="Demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
    )

    assert workspace.status == "ready"
    assert workspace.daytona_sandbox_id == f"sandbox-{workspace.id}"
    assert workspace.workspace_path == "workspace"
    assert session.commits == 2
    assert session.refreshed == [workspace]
    assert workspace.updated_at is not None


async def test_workspace_payloads_expose_the_callers_role(workspace_context):
    _user, _service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "password"},
        )
        created = await client.post(
            "/workspaces",
            json={"name": "Demo", "repo_url": "https://github.com/example/demo"},
        )
        workspace_id = created.json()["id"]
        listed = await client.get("/workspaces")
        detail = await client.get(f"/workspaces/{workspace_id}")

    assert created.json()["viewer_role"] == "owner"
    assert [item["viewer_role"] for item in listed.json()] == ["owner"]
    assert detail.json()["viewer_role"] == "owner"


async def test_viewer_role_maps_team_membership_to_a_workspace_role():
    class StubResult:
        def __init__(self, value: str | None) -> None:
            self.value = value

        def scalar_one_or_none(self) -> str | None:
            return self.value

    class StubSession:
        def __init__(self, team_role: str | None) -> None:
            self.team_role = team_role

        async def execute(self, _statement):
            return StubResult(self.team_role)

    now = datetime.now(UTC)
    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        team_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        snapshot=None,
        workspace_path="workspace",
        status="ready",
        created_at=now,
        updated_at=now,
    )

    member = await workspace_response(
        StubSession("viewer"),  # type: ignore[arg-type]
        workspace,
        uuid4(),
    )
    owner = await workspace_response(
        StubSession(None),  # type: ignore[arg-type]
        workspace,
        workspace.owner_id,
    )

    assert member.viewer_role == "team_viewer"
    assert owner.viewer_role == "owner"
