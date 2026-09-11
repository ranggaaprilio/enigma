from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import CustomCommand, User, Workspace, WorkspaceEnvVar
from app.services.workspace_env import (
    EnvFileError,
    load_all_values,
    materialize_env_file,
    prepare_launch_env,
    redactable_env_values,
    resolve,
)
from app.services.workspaces import WorkspaceSecretCipher


@pytest.fixture
async def db_session():
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


async def make_workspace(session, *, app_root: str | None = None, env_file_path: str | None = None):
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
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        app_root=app_root,
        env_file_path=env_file_path,
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


async def make_command(session, workspace, *, name: str = "API") -> CustomCommand:
    now = datetime.now(UTC)
    cmd = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name=name,
        command="echo hi",
        role="custom",
        kind="task",
        sort_order=0,
        requires_confirmation=False,
        created_at=now,
        updated_at=now,
    )
    session.add(cmd)
    await session.flush()
    return cmd


async def add_env_var(
    session,
    workspace,
    *,
    key: str,
    value: str,
    custom_command_id=None,
    scope: str = "workspace",
) -> None:
    cipher = WorkspaceSecretCipher(get_settings().workspace_env_encryption_key)
    session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=custom_command_id,
            scope=scope,
            key=key,
            encrypted_value=cipher.encrypt(value),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.flush()


# --- resolve / load_all_values ------------------------------------------------------


async def test_resolve_returns_only_workspace_wide_rows_without_a_command(db_session):
    _user, workspace = await make_workspace(db_session)
    await add_env_var(db_session, workspace, key="FOO", value="bar")

    values = await resolve(db_session, get_settings(), workspace.id)

    assert values == {"FOO": "bar"}


async def test_resolve_service_scoped_overrides_workspace_wide_by_key(db_session):
    _user, workspace = await make_workspace(db_session)
    command = await make_command(db_session, workspace)
    await add_env_var(db_session, workspace, key="FOO", value="base")
    await add_env_var(
        db_session,
        workspace,
        key="FOO",
        value="override",
        custom_command_id=command.id,
        scope="service",
    )
    await add_env_var(db_session, workspace, key="BASE_ONLY", value="stays")

    values = await resolve(db_session, get_settings(), workspace.id, custom_command_id=command.id)

    assert values == {"FOO": "override", "BASE_ONLY": "stays"}


async def test_resolve_without_custom_command_id_ignores_service_scoped_rows(db_session):
    _user, workspace = await make_workspace(db_session)
    command = await make_command(db_session, workspace)
    await add_env_var(
        db_session,
        workspace,
        key="ONLY_SERVICE",
        value="hidden",
        custom_command_id=command.id,
        scope="service",
    )

    values = await resolve(db_session, get_settings(), workspace.id)

    assert values == {}


async def test_load_all_values_includes_every_scope(db_session):
    _user, workspace = await make_workspace(db_session)
    command = await make_command(db_session, workspace)
    await add_env_var(db_session, workspace, key="WORKSPACE_WIDE", value="one")
    await add_env_var(
        db_session,
        workspace,
        key="SCOPED",
        value="two",
        custom_command_id=command.id,
        scope="service",
    )

    values = await load_all_values(db_session, get_settings(), workspace.id)

    assert values == {"WORKSPACE_WIDE": "one", "SCOPED": "two"}


def test_redactable_env_values_matches_secret_shaped_keys_and_long_values():
    env = {"SHORT": "x", "DATABASE_PASSWORD": "pw", "OPAQUE": "y" * 16}

    redacted = set(redactable_env_values(env))

    assert redacted == {"pw", "y" * 16}


# --- materialize_env_file / prepare_launch_env --------------------------------------


class FakeFileInfo:
    pass


class FakeFs:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.uploaded: list[tuple[str, bytes]] = []
        self.permissions: list[tuple[str, str]] = []

    async def get_file_info(self, path: str) -> FakeFileInfo:
        if path not in self.existing:
            raise FileNotFoundError(path)
        return FakeFileInfo()

    async def upload_file(self, content: bytes, path: str) -> None:
        self.uploaded.append((path, content))

    async def set_file_permissions(self, path: str, *, mode: str) -> None:
        self.permissions.append((path, mode))


class FakeSandbox:
    def __init__(self, existing: set[str]) -> None:
        self.fs = FakeFs(existing)


class FakeRuntime:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox
        self.commands: list[str] = []
        self.cwds: list[str | None] = []

    async def get_sandbox(self, workspace, *, start: bool = True) -> FakeSandbox:
        return self.sandbox

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.commands.append(command)
        self.cwds.append(cwd)

        class _Result:
            exit_code = 0
            stdout = ""
            stderr = ""

        return _Result()


def _workspace(*, app_root: str | None = None, env_file_path: str | None = None) -> Workspace:
    now = datetime.now(UTC)
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        app_root=app_root,
        env_file_path=env_file_path,
        status="ready",
        created_at=now,
        updated_at=now,
    )


async def test_materialize_env_file_noop_when_unset():
    workspace = _workspace(env_file_path=None)
    runtime = FakeRuntime(FakeSandbox({"workspace"}))

    result = await materialize_env_file(
        runtime, workspace, checkout="workspace", env={"FOO": "bar"}, timeout=30
    )

    assert result is None
    assert runtime.commands == []
    assert runtime.sandbox.fs.uploaded == []
    assert runtime.sandbox.fs.permissions == []


async def test_materialize_env_file_writes_600_and_appends_exclude():
    workspace = _workspace(app_root="apps/api", env_file_path=".env")
    runtime = FakeRuntime(FakeSandbox({"workspace/apps/api"}))
    secret_value = "sk-live-abcdef1234567890"

    result = await materialize_env_file(
        runtime,
        workspace,
        checkout="workspace",
        env={"API_KEY": secret_value},
        timeout=30,
    )

    assert result == "workspace/apps/api/.env"
    [(path, content)] = runtime.sandbox.fs.uploaded
    assert path == "workspace/apps/api/.env"
    assert f"API_KEY={secret_value}".encode() in content or (
        f"API_KEY='{secret_value}'".encode() in content
    )
    assert runtime.sandbox.fs.permissions == [("workspace/apps/api/.env", "600")]
    # The exclude-append command carries only the path, never the secret value.
    assert len(runtime.commands) == 1
    assert secret_value not in runtime.commands[0]
    assert "apps/api/.env" in runtime.commands[0]
    assert runtime.cwds == ["workspace"]


async def test_materialize_env_file_missing_parent_raises_env_file_error():
    workspace = _workspace(env_file_path="does/not/exist/.env")
    runtime = FakeRuntime(FakeSandbox({"workspace"}))

    with pytest.raises(EnvFileError):
        await materialize_env_file(
            runtime, workspace, checkout="workspace", env={"FOO": "bar"}, timeout=30
        )

    assert runtime.sandbox.fs.uploaded == []
    assert runtime.commands == []


async def test_prepare_launch_env_resolves_and_materializes(db_session):
    _user, workspace = await make_workspace(db_session, env_file_path=".env")
    await add_env_var(db_session, workspace, key="FOO", value="bar")
    runtime = FakeRuntime(FakeSandbox({"workspace"}))

    env = await prepare_launch_env(
        db_session,
        get_settings(),
        runtime,
        workspace,
        checkout="workspace",
        custom_command_id=None,
        timeout=30,
    )

    assert env == {"FOO": "bar"}
    [(path, content)] = runtime.sandbox.fs.uploaded
    assert path == "workspace/.env"
    assert b"FOO=bar" in content
