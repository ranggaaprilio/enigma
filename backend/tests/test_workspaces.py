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
from app.services.git_providers import sanitize_repository_url
from app.services.sandbox import SandboxRuntimeError
from app.services.templates import TemplateNotFoundError
from app.services.users import DuplicateUserEmailError
from app.services.workspace_init import WorkspaceInitializationError
from app.services.workspaces import (
    BulkItem,
    InvalidEnvScopeError,
    InvalidEnvVarKeyError,
    SqlAlchemyWorkspaceService,
    WorkspaceAccessDeniedError,
    WorkspaceAlreadyExistsError,
    WorkspaceNotFoundError,
    WorkspaceRuntimeError,
    WorkspaceSecretCipher,
    validate_env_key,
)

KNOWN_TEMPLATE_ID = UUID("00000000-0000-4000-8000-000000000001")


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
        # Workspace-wide entries only, kept exactly as before scoping existed -- some tests
        # index into this directly.
        self.encrypted_values: dict[tuple[UUID, str], str] = {}
        # Service-scoped entries: (workspace_id, custom_command_id, key) -> encrypted value.
        self.scoped_encrypted_values: dict[tuple[UUID, UUID, str], str] = {}
        # Registered by tests that need `custom_command_id` scoping to validate, since this
        # fake has no real `custom_commands` table to check against.
        self.custom_command_workspace: dict[UUID, UUID] = {}
        self.unavailable_runtimes: set[str] = set()
        self.custom_command_names: dict[UUID, str] = {}

    def register_custom_command(
        self, command_id: UUID, workspace_id: UUID, name: str = "Service"
    ) -> None:
        self.custom_command_workspace[command_id] = workspace_id
        self.custom_command_names[command_id] = name

    async def list_for_owner(self, owner_id: UUID) -> list[Workspace]:
        return [
            workspace for workspace in self.workspaces.values() if workspace.owner_id == owner_id
        ]

    async def get_for_owner(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace

    async def get_for_manager(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        return await self.get_for_owner(workspace_id, user_id)

    async def create(
        self,
        owner_id: UUID,
        name: str,
        repo_url: str | None,
        default_branch: str,
        snapshot: str | None = None,
        *,
        source: str = "repository",
        template_id: UUID | None = None,
        runtime: str | None = None,
    ) -> Workspace:
        if runtime is not None and runtime in self.unavailable_runtimes:
            from app.services.sandbox.availability import RuntimeChoiceError

            raise RuntimeChoiceError(
                "Local Docker is not available: The Docker daemon is not reachable"
            )
        if await self.list_for_owner(owner_id):
            raise WorkspaceAlreadyExistsError
        if source == "repository":
            repo_url = sanitize_repository_url(repo_url or "")
        elif source == "template":
            if template_id != KNOWN_TEMPLATE_ID:
                raise TemplateNotFoundError
            repo_url = None
        else:
            repo_url = None

        now = datetime.now(UTC)
        workspace_id = uuid4()
        workspace = Workspace(
            id=workspace_id,
            owner_id=owner_id,
            name=name,
            slug=name.lower().replace(" ", "-"),
            repo_url=repo_url,
            source=source,
            template_id=template_id,
            default_branch=default_branch,
            runtime=runtime or "daytona",
            sandbox_id=f"sandbox-{workspace_id}",
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
        app_root: str | None = None,
        env_file_path: str | None = None,
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
        if app_root is not None:
            workspace.app_root = app_root
        if env_file_path is not None:
            workspace.env_file_path = env_file_path
        workspace.updated_at = datetime.now(UTC)
        return workspace

    async def delete(self, workspace_id: UUID, owner_id: UUID) -> None:
        await self.get_for_owner(workspace_id, owner_id)
        del self.workspaces[workspace_id]

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[WorkspaceEnvVar]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        rows = self.env_vars.get(workspace.id, [])
        return sorted(rows, key=lambda row: (row.custom_command_id is not None, row.key))

    def _check_scope(self, workspace_id: UUID, custom_command_id: UUID | None) -> None:
        if custom_command_id is None:
            return
        if self.custom_command_workspace.get(custom_command_id) != workspace_id:
            raise InvalidEnvScopeError

    def _find_env_var(
        self, workspace_id: UUID, key: str, custom_command_id: UUID | None
    ) -> WorkspaceEnvVar | None:
        return next(
            (
                existing
                for existing in self.env_vars.get(workspace_id, [])
                if existing.key == key and existing.custom_command_id == custom_command_id
            ),
            None,
        )

    def _record_encrypted_value(
        self, workspace_id: UUID, key: str, custom_command_id: UUID | None, encrypted_value: str
    ) -> None:
        if custom_command_id is None:
            self.encrypted_values[(workspace_id, key)] = encrypted_value
        else:
            self.scoped_encrypted_values[(workspace_id, custom_command_id, key)] = encrypted_value

    async def upsert_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        value: str,
        *,
        custom_command_id: UUID | None = None,
    ) -> tuple[WorkspaceEnvVar, str]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        self._check_scope(workspace.id, custom_command_id)
        normalized_key = validate_env_key(key)
        encrypted_value = self.cipher.encrypt(value)
        existing = self._find_env_var(workspace.id, normalized_key, custom_command_id)
        if existing is None:
            now = datetime.now(UTC)
            env_var = WorkspaceEnvVar(
                id=uuid4(),
                workspace_id=workspace.id,
                custom_command_id=custom_command_id,
                scope="service" if custom_command_id else "workspace",
                key=normalized_key,
                encrypted_value=encrypted_value,
                created_at=now,
                updated_at=now,
            )
            self.env_vars.setdefault(workspace.id, []).append(env_var)
            write_status = "created"
        else:
            old_value = self.cipher.decrypt(existing.encrypted_value)
            write_status = "unchanged" if old_value == value else "updated"
            existing.encrypted_value = encrypted_value
            existing.updated_at = datetime.now(UTC)
            env_var = existing
        self._record_encrypted_value(
            workspace.id, normalized_key, custom_command_id, encrypted_value
        )
        return env_var, write_status

    async def bulk_upsert_env_vars(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        values: dict[str, str],
        *,
        custom_command_id: UUID | None = None,
        dry_run: bool = False,
    ) -> list[BulkItem]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        self._check_scope(workspace.id, custom_command_id)

        items: list[BulkItem] = []
        for key, value in values.items():
            try:
                normalized_key = validate_env_key(key)
            except InvalidEnvVarKeyError:
                items.append(
                    BulkItem(
                        key=key,
                        status="invalid",
                        message="must use uppercase letters, numbers, and underscores",
                    )
                )
                continue

            existing = self._find_env_var(workspace.id, normalized_key, custom_command_id)
            if existing is None:
                write_status = "created"
                if not dry_run:
                    now = datetime.now(UTC)
                    encrypted_value = self.cipher.encrypt(value)
                    env_var = WorkspaceEnvVar(
                        id=uuid4(),
                        workspace_id=workspace.id,
                        custom_command_id=custom_command_id,
                        scope="service" if custom_command_id else "workspace",
                        key=normalized_key,
                        encrypted_value=encrypted_value,
                        created_at=now,
                        updated_at=now,
                    )
                    self.env_vars.setdefault(workspace.id, []).append(env_var)
                    self._record_encrypted_value(
                        workspace.id, normalized_key, custom_command_id, encrypted_value
                    )
            else:
                old_value = self.cipher.decrypt(existing.encrypted_value)
                write_status = "unchanged" if old_value == value else "updated"
                if not dry_run and write_status == "updated":
                    encrypted_value = self.cipher.encrypt(value)
                    existing.encrypted_value = encrypted_value
                    existing.updated_at = datetime.now(UTC)
                    self._record_encrypted_value(
                        workspace.id, normalized_key, custom_command_id, encrypted_value
                    )
            items.append(BulkItem(key=normalized_key, status=write_status))
        return items

    async def delete_env_var(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        key: str,
        *,
        custom_command_id: UUID | None = None,
    ) -> None:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        normalized_key = validate_env_key(key)
        self.env_vars[workspace.id] = [
            existing
            for existing in self.env_vars.get(workspace.id, [])
            if not (
                existing.key == normalized_key and existing.custom_command_id == custom_command_id
            )
        ]

    async def bulk_delete_env_vars(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        keys: list[str],
        *,
        custom_command_id: UUID | None = None,
    ) -> tuple[list[str], list[str]]:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        self._check_scope(workspace.id, custom_command_id)

        deleted: list[str] = []
        missing: list[str] = []
        for key in keys:
            try:
                normalized_key = validate_env_key(key)
            except InvalidEnvVarKeyError:
                missing.append(key)
                continue
            existing = self._find_env_var(workspace.id, normalized_key, custom_command_id)
            if existing is None:
                missing.append(normalized_key)
            else:
                deleted.append(normalized_key)
                self.env_vars[workspace.id] = [
                    item for item in self.env_vars.get(workspace.id, []) if item is not existing
                ]
        return deleted, missing

    async def env_template(self, workspace_id: UUID, owner_id: UUID) -> str:
        workspace = await self.get_for_owner(workspace_id, owner_id)
        env_vars = self.env_vars.get(workspace.id, [])
        workspace_wide = sorted(
            (row for row in env_vars if row.custom_command_id is None), key=lambda row: row.key
        )
        by_command: dict[UUID, list[WorkspaceEnvVar]] = {}
        for row in env_vars:
            if row.custom_command_id is not None:
                by_command.setdefault(row.custom_command_id, []).append(row)

        sections: list[str] = []
        if workspace_wide:
            lines = "\n".join(f"{row.key}=" for row in workspace_wide)
            sections.append(f"# Workspace-wide\n{lines}\n")
        for command_id, rows in by_command.items():
            name = self.custom_command_names.get(command_id, "Unknown service")
            lines = "\n".join(f"{row.key}=" for row in sorted(rows, key=lambda row: row.key))
            sections.append(f"# Service: {name} ({command_id})\n{lines}\n")
        return "\n".join(sections)


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


async def _create_workspace_via_api(client: AsyncClient, name: str) -> str:
    response = await client.post(
        "/workspaces",
        json={"name": name, "repo_url": f"https://github.com/example/{name.lower()}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_bulk_upsert_env_vars_reports_per_item_status_and_counts(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "Bulk")

        await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "SAME", "value": "same-value"},
        )
        await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "CHANGED", "value": "old-value"},
        )

        text = "SAME=same-value\nCHANGED=new-value\nNEW_ONE=a\nNEW_TWO=b\n1BAD=nope\n"
        response = await client.post(
            f"/workspaces/{workspace_id}/env-vars/bulk",
            json={"text": text},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] == 2
    assert payload["updated"] == 1
    assert payload["unchanged"] == 1
    assert payload["invalid"] == 1
    assert payload["dry_run"] is False
    body_text = str(payload)
    for value in ("same-value", "new-value", "old-value", "nope"):
        assert value not in body_text


async def test_bulk_upsert_dry_run_persists_nothing(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "DryRun")

        response = await client.post(
            f"/workspaces/{workspace_id}/env-vars/bulk",
            json={"values": {"NEW_KEY": "value"}, "dry_run": True},
        )
        listed = await client.get(f"/workspaces/{workspace_id}/env-vars")

    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["created"] == 1
    assert payload["items"] == [{"key": "NEW_KEY", "status": "created", "message": None}]
    assert listed.json() == []


async def test_bulk_upsert_requires_exactly_one_of_text_or_values(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "Neither")

        neither = await client.post(f"/workspaces/{workspace_id}/env-vars/bulk", json={})
        both = await client.post(
            f"/workspaces/{workspace_id}/env-vars/bulk",
            json={"text": "A=1", "values": {"A": "1"}},
        )

    assert neither.status_code == 422
    assert both.status_code == 422


async def test_upsert_env_var_rejects_custom_command_from_a_different_workspace(workspace_context):
    user, service, _tmp_path = workspace_context
    foreign_workspace = await service.create(
        owner_id=uuid4(),
        name="Beta",
        repo_url="https://github.com/example/beta",
        default_branch="main",
    )
    foreign_command_id = uuid4()
    service.register_custom_command(foreign_command_id, foreign_workspace.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "Alpha")
        response = await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "FOO", "value": "bar", "custom_command_id": str(foreign_command_id)},
        )

    assert response.status_code == 400


async def test_same_key_scoped_to_two_different_commands_persists_independently(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "Scoped")

        command_a = uuid4()
        command_b = uuid4()
        service.register_custom_command(command_a, UUID(workspace_id), name="API")
        service.register_custom_command(command_b, UUID(workspace_id), name="Worker")

        first = await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "PORT", "value": "8000", "custom_command_id": str(command_a)},
        )
        second = await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "PORT", "value": "9000", "custom_command_id": str(command_b)},
        )
        listed = await client.get(f"/workspaces/{workspace_id}/env-vars")

    assert first.status_code == 200
    assert first.json()["scope"] == "service"
    assert first.json()["custom_command_id"] == str(command_a)
    assert second.status_code == 200
    assert second.json()["custom_command_id"] == str(command_b)

    scoped_rows = [row for row in listed.json() if row["key"] == "PORT"]
    assert len(scoped_rows) == 2
    assert {row["custom_command_id"] for row in scoped_rows} == {
        str(command_a),
        str(command_b),
    }


async def test_delete_env_var_with_custom_command_id_only_removes_that_scope(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "DeleteScope")

        command_id = uuid4()
        service.register_custom_command(command_id, UUID(workspace_id))

        await client.put(
            f"/workspaces/{workspace_id}/env-vars", json={"key": "FOO", "value": "wide"}
        )
        await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "FOO", "value": "scoped", "custom_command_id": str(command_id)},
        )
        delete_response = await client.delete(
            f"/workspaces/{workspace_id}/env-vars",
            params={"key": "FOO", "custom_command_id": str(command_id)},
        )
        listed = await client.get(f"/workspaces/{workspace_id}/env-vars")

    assert delete_response.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["custom_command_id"] is None
    assert rows[0]["scope"] == "workspace"


async def test_bulk_delete_env_vars_reports_deleted_and_missing(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "BulkDelete")
        await client.put(
            f"/workspaces/{workspace_id}/env-vars", json={"key": "ONE", "value": "1"}
        )
        await client.put(
            f"/workspaces/{workspace_id}/env-vars", json={"key": "TWO", "value": "2"}
        )

        response = await client.post(
            f"/workspaces/{workspace_id}/env-vars/bulk-delete",
            json={"keys": ["ONE", "TWO", "THREE"]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert sorted(payload["deleted"]) == ["ONE", "TWO"]
    assert payload["missing"] == ["THREE"]


async def test_env_template_lists_keys_without_values_and_groups_by_scope(workspace_context):
    user, service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        workspace_id = await _create_workspace_via_api(client, "Template")

        command_id = uuid4()
        service.register_custom_command(command_id, UUID(workspace_id), name="API Server")

        await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "DATABASE_URL", "value": "postgres://secret-host/db"},
        )
        await client.put(
            f"/workspaces/{workspace_id}/env-vars",
            json={"key": "PORT", "value": "8000", "custom_command_id": str(command_id)},
        )
        response = await client.get(f"/workspaces/{workspace_id}/env-vars/template")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "DATABASE_URL=" in body
    assert "PORT=" in body
    assert "postgres://secret-host/db" not in body
    assert "# Workspace-wide" in body
    assert "# Service: API Server" in body


@pytest.mark.parametrize("default_runtime", ["daytona", "docker"])
async def test_sqlalchemy_workspace_create_refreshes_before_return(tmp_path, default_runtime):
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

    # New workspaces take the configured provider; pinned so a root `.env` cannot leak in.
    settings = Settings(sandbox_runtime_default=default_runtime)
    session = FakeSession()

    class FakeRuntime:
        def __init__(self, runtime_settings):
            self.settings = runtime_settings

        async def provision(self, workspace):
            return f"sandbox-{workspace.id}"

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
    assert workspace.runtime == default_runtime
    assert workspace.sandbox_id == f"sandbox-{workspace.id}"
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
        sandbox_id="sandbox-demo",
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


async def _login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_create_blank_workspace_has_no_repo_url(workspace_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            "/workspaces", json={"name": "Scratch", "source": "blank", "default_branch": "trunk"}
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["repo_url"] is None
    assert payload["source"] == "blank"
    assert payload["template_id"] is None
    assert payload["default_branch"] == "trunk"


async def test_create_workspace_from_template(workspace_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            "/workspaces",
            json={"name": "Starter", "source": "template", "template_id": str(KNOWN_TEMPLATE_ID)},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["repo_url"] is None
    assert payload["source"] == "template"
    assert payload["template_id"] == str(KNOWN_TEMPLATE_ID)


async def test_create_workspace_unknown_template_is_404(workspace_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            "/workspaces",
            json={"name": "Starter", "source": "template", "template_id": str(uuid4())},
        )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "A", "source": "repository"},  # repo_url missing
        {"name": "A", "source": "template"},  # template_id missing
        {"name": "A", "source": "blank", "repo_url": "https://github.com/e/d"},
        {"name": "A", "source": "template", "template_id": str(uuid4()), "repo_url": "https://x.io/a/b"},
        {"name": "A", "source": "zip"},
    ],
)
async def test_create_workspace_source_validation(workspace_context, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post("/workspaces", json=payload)

    assert response.status_code == 422


async def test_create_workspace_passes_the_chosen_runtime(workspace_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            "/workspaces", json={"name": "Local", "source": "blank", "runtime": "docker"}
        )

    assert response.status_code == 201, response.text
    assert response.json()["runtime"] == "docker"


async def test_create_workspace_with_an_unavailable_runtime_is_422(workspace_context):
    _, service, _ = workspace_context
    service.unavailable_runtimes.add("docker")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            "/workspaces", json={"name": "Local", "source": "blank", "runtime": "docker"}
        )
        unknown = await client.post(
            "/workspaces", json={"name": "Local", "source": "blank", "runtime": "k8s"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Local Docker is not available: The Docker daemon is not reachable"
    )
    assert service.workspaces == {}
    assert unknown.status_code == 422


async def test_create_workspace_rejects_invalid_repo_url(workspace_context):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            "/workspaces", json={"name": "Demo", "repo_url": "http://localhost/example/demo"}
        )

    assert response.status_code == 400


class _FakeCreateSession:
    def __init__(self) -> None:
        self.added: list[Workspace] = []
        self.commits = 0

    async def execute(self, statement):
        return type("EmptyResult", (), {"scalar_one_or_none": lambda self: None})()

    def add(self, workspace):
        self.added.append(workspace)

    async def flush(self):
        self.added[-1].id = uuid4()

    async def commit(self):
        self.commits += 1

    async def refresh(self, workspace):
        now = datetime.now(UTC)
        workspace.created_at = workspace.created_at or now
        workspace.updated_at = now


class _FakeProvisionRuntime:
    def __init__(self, runtime_settings):
        self.settings = runtime_settings

    async def provision(self, workspace):
        return f"sandbox-{workspace.id}"


class _RecordingInitializer:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def initialize_blank(self, workspace):
        self.calls.append("blank")
        if self.fail:
            raise WorkspaceInitializationError("git init failed")

    async def initialize_from_template(self, workspace, template):
        self.calls.append(f"template:{template.slug}")


async def test_sqlalchemy_create_blank_runs_initializer_then_marks_ready():
    settings = Settings()
    initializer = _RecordingInitializer()
    service = SqlAlchemyWorkspaceService(
        session=_FakeCreateSession(),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=initializer,  # type: ignore[arg-type]
    )

    workspace = await service.create(
        owner_id=uuid4(), name="Scratch", repo_url=None, default_branch="main", source="blank"
    )

    assert initializer.calls == ["blank"]
    assert workspace.status == "ready"
    assert workspace.repo_url is None
    assert workspace.source == "blank"


async def test_sqlalchemy_create_marks_error_when_initializer_fails():
    settings = Settings()
    session = _FakeCreateSession()
    service = SqlAlchemyWorkspaceService(
        session=session,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=_RecordingInitializer(fail=True),  # type: ignore[arg-type]
    )

    with pytest.raises(WorkspaceInitializationError):
        await service.create(
            owner_id=uuid4(), name="Scratch", repo_url=None, default_branch="main", source="blank"
        )

    assert session.added[-1].status == "error"


def _availability(**statuses: str):
    from app.services.sandbox.availability import RuntimeAvailability

    async def availability(provider: str) -> RuntimeAvailability:
        status = statuses.get(provider, "unconfigured")
        return RuntimeAvailability(
            provider, status != "unconfigured", status, None if status == "ok" else "down"  # type: ignore[arg-type]
        )

    return availability


async def test_sqlalchemy_create_uses_the_requested_runtime():
    settings = Settings(
        sandbox_runtime_default="daytona", metrics_token="scrape-secret", docker_upkeep=True
    )
    service = SqlAlchemyWorkspaceService(
        session=_FakeCreateSession(),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=_RecordingInitializer(),  # type: ignore[arg-type]
        availability=_availability(daytona="ok", docker="ok"),
    )

    workspace = await service.create(
        owner_id=uuid4(), name="Local", repo_url=None, default_branch="main", source="blank",
        runtime="docker",
    )

    assert workspace.runtime == "docker"


async def test_sqlalchemy_create_refuses_an_unavailable_runtime_before_writing_a_row():
    from app.services.sandbox.availability import RuntimeChoiceError

    settings = Settings(sandbox_runtime_default="daytona")
    session = _FakeCreateSession()
    service = SqlAlchemyWorkspaceService(
        session=session,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=_RecordingInitializer(),  # type: ignore[arg-type]
        availability=_availability(daytona="ok", docker="error"),
    )

    with pytest.raises(RuntimeChoiceError):
        await service.create(
            owner_id=uuid4(), name="Local", repo_url=None, default_branch="main",
            source="blank", runtime="docker",
        )

    assert session.added == [] and session.commits == 0  # nothing half-provisioned


async def test_sqlalchemy_create_repository_sanitizes_url():
    settings = Settings()
    service = SqlAlchemyWorkspaceService(
        session=_FakeCreateSession(),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=_RecordingInitializer(),  # type: ignore[arg-type]
    )

    workspace = await service.create(
        owner_id=uuid4(),
        name="Demo",
        repo_url="https://token@gitlab.com/team/demo",
        default_branch="main",
    )

    assert workspace.repo_url == "https://gitlab.com/team/demo.git"
    assert workspace.source == "repository"


class _FakeReprovisionSession(_FakeCreateSession):
    """`_FakeCreateSession` plus the `get` that `get_for_owner` needs."""

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    async def get(self, model, workspace_id):
        return self.workspace if workspace_id == self.workspace.id else None


def _errored_workspace(owner_id, *, source: str = "blank", sandbox_id: str | None = None):
    return Workspace(
        id=uuid4(),
        owner_id=owner_id,
        name="Scratch",
        slug="scratch",
        repo_url=None,
        source=source,
        default_branch="main",
        runtime="daytona",
        workspace_path="workspace",
        status="error",
        sandbox_id=sandbox_id,
    )


async def test_reprovision_recovers_a_workspace_whose_sandbox_never_came_up():
    settings = Settings()
    owner_id = uuid4()
    workspace = _errored_workspace(owner_id)
    initializer = _RecordingInitializer()
    service = SqlAlchemyWorkspaceService(
        session=_FakeReprovisionSession(workspace),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=initializer,  # type: ignore[arg-type]
    )

    recovered = await service.reprovision(workspace.id, owner_id)

    assert recovered.status == "ready"
    assert recovered.sandbox_id == f"sandbox-{workspace.id}"
    # It never finished creating, so the blank seed commit still has to run.
    assert initializer.calls == ["blank"]


async def test_reprovision_leaves_an_existing_sandbox_uninitialized():
    settings = Settings()
    owner_id = uuid4()
    workspace = _errored_workspace(owner_id)
    # The provider finds the same sandbox again (it looks it up by the workspace's name).
    workspace.sandbox_id = f"sandbox-{workspace.id}"
    initializer = _RecordingInitializer()
    service = SqlAlchemyWorkspaceService(
        session=_FakeReprovisionSession(workspace),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=initializer,  # type: ignore[arg-type]
    )

    recovered = await service.reprovision(workspace.id, owner_id)

    assert recovered.status == "ready"
    # Re-seeding would write over the working tree the sandbox already has.
    assert initializer.calls == []


async def test_reprovision_seeds_a_replacement_for_a_sandbox_that_is_gone():
    """A Docker container removed (or a Daytona sandbox deleted) behind our back: the provider
    makes a new, empty sandbox with a new id, so a blank or template workspace must be seeded
    again or it comes back "ready" without a git repository."""
    settings = Settings()
    owner_id = uuid4()
    workspace = _errored_workspace(owner_id, sandbox_id="container-that-was-removed")
    initializer = _RecordingInitializer()
    service = SqlAlchemyWorkspaceService(
        session=_FakeReprovisionSession(workspace),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FakeProvisionRuntime(settings),  # type: ignore[arg-type]
        initializer=initializer,  # type: ignore[arg-type]
    )

    recovered = await service.reprovision(workspace.id, owner_id)

    assert recovered.status == "ready"
    assert recovered.sandbox_id == f"sandbox-{workspace.id}"
    assert initializer.calls == ["blank"]


async def test_reprovision_marks_error_when_the_runtime_still_fails():
    settings = Settings()
    owner_id = uuid4()
    workspace = _errored_workspace(owner_id)

    class _FailingRuntime:
        def __init__(self, runtime_settings):
            self.settings = runtime_settings

        async def provision(self, workspace):
            raise SandboxRuntimeError("snapshot not found")

    service = SqlAlchemyWorkspaceService(
        session=_FakeReprovisionSession(workspace),  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=_FailingRuntime(settings),  # type: ignore[arg-type]
        initializer=_RecordingInitializer(),  # type: ignore[arg-type]
    )

    with pytest.raises(WorkspaceRuntimeError):
        await service.reprovision(workspace.id, owner_id)

    assert workspace.status == "error"


# --- PATCH env_file_path ---------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    ["/abs", "../x", "dir/", ".git/x"],
)
async def test_patch_env_file_path_rejects_invalid_values(workspace_context, candidate):
    user, _service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        created = await client.post(
            "/workspaces",
            json={"name": "Demo", "repo_url": "https://github.com/example/demo"},
        )
        workspace_id = created.json()["id"]
        response = await client.patch(
            f"/workspaces/{workspace_id}",
            json={"env_file_path": candidate},
        )

    assert response.status_code == 422


async def test_patch_env_file_path_accepts_and_echoes_a_valid_value(workspace_context):
    user, _service, _tmp_path = workspace_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/auth/login", json={"email": user.email, "password": "password"})
        created = await client.post(
            "/workspaces",
            json={"name": "Demo", "repo_url": "https://github.com/example/demo"},
        )
        workspace_id = created.json()["id"]
        response = await client.patch(
            f"/workspaces/{workspace_id}",
            json={"env_file_path": "apps/api/.env"},
        )

    assert response.status_code == 200
    assert response.json()["env_file_path"] == "apps/api/.env"


# --- team roles on a teammate's workspace -------------------------------------------------


class _TeamRoleSession:
    """`get` returns the workspace; `execute` answers the single `TeamMember.role` lookup."""

    def __init__(self, workspace: Workspace, team_role: str | None) -> None:
        self.workspace = workspace
        self.team_role = team_role
        self.committed = False

    async def get(self, _model, _id):
        return self.workspace

    async def execute(self, _statement):
        team_role = self.team_role

        class _Result:
            def scalar_one_or_none(self):
                return team_role

        return _Result()

    def add(self, _row) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _row) -> None:
        return None


def _team_workspace() -> Workspace:
    now = datetime.now(UTC)
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        team_id=uuid4(),
        name="Team",
        slug="team",
        repo_url="https://github.com/example/team",
        default_branch="main",
        runtime="daytona",
        sandbox_id="sandbox-team",
        workspace_path="workspace",
        status="ready",
        created_at=now,
        updated_at=now,
    )


def _team_service(workspace: Workspace, team_role: str | None):
    settings = Settings()
    session = _TeamRoleSession(workspace, team_role)
    service = SqlAlchemyWorkspaceService(
        session=session,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
        runtime=None,  # type: ignore[arg-type]
    )
    return service, session


@pytest.mark.parametrize("team_role", ["viewer", "member"])
async def test_only_a_team_lead_or_the_owner_may_change_a_team_workspace(team_role):
    workspace = _team_workspace()
    service, session = _team_service(workspace, team_role)

    with pytest.raises(WorkspaceAccessDeniedError):
        await service.update(workspace.id, uuid4(), name="Renamed")
    with pytest.raises(WorkspaceAccessDeniedError):
        await service.delete(workspace.id, uuid4())

    assert workspace.name == "Team"
    assert session.committed is False


async def test_team_lead_may_rename_but_not_repoint_the_owners_credential():
    workspace = _team_workspace()
    service, _session = _team_service(workspace, "lead")

    renamed = await service.update(workspace.id, uuid4(), name="Renamed")
    assert renamed.name == "Renamed"

    with pytest.raises(WorkspaceAccessDeniedError):
        await service.update(workspace.id, uuid4(), repo_url="https://evil.example/x/y")
    assert workspace.repo_url == "https://github.com/example/team"


async def test_owner_may_repoint_their_own_workspace():
    workspace = _team_workspace()
    service, _session = _team_service(workspace, None)

    updated = await service.update(
        workspace.id, workspace.owner_id, repo_url="https://github.com/example/other"
    )
    assert updated.repo_url == sanitize_repository_url("https://github.com/example/other")


async def test_team_viewer_cannot_drive_but_a_member_can():
    workspace = _team_workspace()

    viewer_service, _ = _team_service(workspace, "viewer")
    with pytest.raises(WorkspaceAccessDeniedError):
        await viewer_service.get_for_driver(workspace.id, uuid4())
    with pytest.raises(WorkspaceAccessDeniedError):
        await viewer_service.upsert_env_var(workspace.id, uuid4(), "API_KEY", "secret")

    member_service, _ = _team_service(workspace, "member")
    assert await member_service.get_for_driver(workspace.id, uuid4()) is workspace


async def test_non_member_still_gets_not_found_for_a_change():
    workspace = _team_workspace()
    service, _session = _team_service(workspace, None)

    with pytest.raises(WorkspaceNotFoundError):
        await service.update(workspace.id, uuid4(), name="Renamed")


async def test_update_route_maps_a_denied_team_role_to_403(workspace_context):
    _user, service, _tmp_path = workspace_context

    async def denied(**_kwargs):
        raise WorkspaceAccessDeniedError

    service.update = denied  # type: ignore[method-assign]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "password"},
        )
        response = await client.patch(f"/workspaces/{uuid4()}", json={"name": "Renamed"})

    assert response.status_code == 403
