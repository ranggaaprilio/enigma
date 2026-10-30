"""Scoping CRUD, affected-services propagation, egress warnings, and audit-metadata coverage
for Epic 12's env-var routes -- the pieces that need real `WorkspaceService`/`CustomCommand`
rows (for `affected_running_services`) or a real, FK-constrained `AuditLog` row (for the
audit-metadata fix), so they use the real `SqlAlchemyWorkspaceService` bound to a transactional
`db_session`, not the in-memory `FakeWorkspaceService` in `test_workspaces.py`.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import workspaces as workspaces_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import AuditLog, CustomCommand, User, Workspace, WorkspaceEnvVar, WorkspaceService
from app.services.workspace_env import affected_running_services, egress_warnings
from app.services.workspaces import SqlAlchemyWorkspaceService, WorkspaceSecretCipher


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


async def make_workspace(session) -> tuple[User, Workspace]:
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
        sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


async def make_service_command(
    session,
    workspace: Workspace,
    *,
    name: str = "API",
    command: str = "uvicorn app.main:app --port 8000",
    port: int | None = 8000,
) -> CustomCommand:
    now = datetime.now(UTC)
    cmd = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name=name,
        command=command,
        role="custom",
        kind="service",
        port=port,
        sort_order=0,
        requires_confirmation=False,
        created_at=now,
        updated_at=now,
    )
    session.add(cmd)
    await session.flush()
    return cmd


async def make_running_service(
    session,
    workspace: Workspace,
    *,
    custom_command_id=None,
    status: str = "running",
) -> WorkspaceService:
    now = datetime.now(UTC)
    row = WorkspaceService(
        id=uuid4(),
        workspace_id=workspace.id,
        custom_command_id=custom_command_id,
        status=status,
        desired=True,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


def _override_current_user(user: User):
    async def _override():
        return user

    return _override


def _override_db(session):
    async def _override():
        yield session

    return _override


def _override_workspace_service(session):
    async def _override():
        return SqlAlchemyWorkspaceService(
            session=session,
            cipher=WorkspaceSecretCipher(get_settings().workspace_env_encryption_key),
            runtime=object(),
        )

    return _override


# --- affected_running_services (direct) ---------------------------------------------------


async def test_workspace_wide_change_affects_every_active_service_except_one_with_override(
    db_session,
):
    _user, workspace = await make_workspace(db_session)
    web = await make_service_command(db_session, workspace, name="Web", port=5173)
    api = await make_service_command(db_session, workspace, name="API", port=8000)
    await make_running_service(db_session, workspace, custom_command_id=web.id)
    await make_running_service(db_session, workspace, custom_command_id=api.id)
    # API has its own scoped override for PORT -- the workspace-wide change is a no-op for it.
    db_session.add(
        WorkspaceEnvVar(
            id=uuid4(),
            workspace_id=workspace.id,
            custom_command_id=api.id,
            scope="service",
            key="PORT",
            encrypted_value="does-not-matter",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    affected = await affected_running_services(db_session, workspace.id, [("PORT", None)])

    assert {item.custom_command_id for item in affected} == {web.id}


async def test_service_scoped_change_only_affects_that_services_active_row(db_session):
    _user, workspace = await make_workspace(db_session)
    web = await make_service_command(db_session, workspace, name="Web", port=5173)
    api = await make_service_command(db_session, workspace, name="API", port=8000)
    await make_running_service(db_session, workspace, custom_command_id=web.id)
    await make_running_service(db_session, workspace, custom_command_id=api.id)

    affected = await affected_running_services(db_session, workspace.id, [("PORT", api.id)])

    assert len(affected) == 1
    assert affected[0].custom_command_id == api.id
    assert affected[0].name == "API"


async def test_built_in_dev_row_is_affected_by_workspace_wide_changes(db_session):
    _user, workspace = await make_workspace(db_session)
    await make_running_service(db_session, workspace, custom_command_id=None)

    affected = await affected_running_services(db_session, workspace.id, [("FOO", None)])

    assert len(affected) == 1
    assert affected[0].custom_command_id is None
    assert affected[0].name == "Dev Server"


async def test_no_active_services_returns_empty(db_session):
    _user, workspace = await make_workspace(db_session)
    web = await make_service_command(db_session, workspace)
    await make_running_service(db_session, workspace, custom_command_id=web.id, status="stopped")

    affected = await affected_running_services(db_session, workspace.id, [("FOO", None)])

    assert affected == []


# --- egress_warnings (direct) ---------------------------------------------------------------


def test_egress_warnings_off_when_block_all_is_false():
    settings = Settings(daytona_network_block_all=False)

    warnings = egress_warnings({"DATABASE_URL": "postgres://10.0.0.5:5432/db"}, settings)

    assert warnings == []


def test_egress_warnings_flags_ip_outside_allow_list():
    settings = Settings(daytona_network_block_all=True, daytona_network_allow_list=["10.0.0.0/8"])

    warnings = egress_warnings({"DATABASE_URL": "postgres://203.0.113.5:5432/db"}, settings)

    assert len(warnings) == 1
    assert "DATABASE_URL" in warnings[0]
    assert "203.0.113.5" in warnings[0]
    assert "5432" in warnings[0]
    assert "postgres://203.0.113.5:5432/db" not in warnings[0]


def test_egress_warnings_silent_for_ip_inside_allow_list():
    settings = Settings(daytona_network_block_all=True, daytona_network_allow_list=["10.0.0.0/8"])

    warnings = egress_warnings({"DATABASE_URL": "postgres://10.1.2.3:5432/db"}, settings)

    assert warnings == []


def test_egress_warnings_softer_advisory_for_hostname():
    settings = Settings(daytona_network_block_all=True, daytona_network_allow_list=["10.0.0.0/8"])

    warnings = egress_warnings({"API_URL": "https://api.example.com:443/v1"}, settings)

    assert len(warnings) == 1
    assert "api.example.com" in warnings[0]
    assert "egress is blocked by default" in warnings[0]


@pytest.mark.parametrize(
    "value",
    [
        "postgres://localhost:5432/db",
        "postgres://127.0.0.1:5432/db",
        "postgres://sub.localhost:5432/db",
        "postgres://[::1]:5432/db",
        "host=127.0.0.1 port=5432 dbname=demo",
    ],
)
def test_egress_warnings_never_flags_loopback_hosts(value: str):
    settings = Settings(daytona_network_block_all=True, daytona_network_allow_list=[])

    warnings = egress_warnings({"DATABASE_URL": value}, settings)

    assert warnings == []


def test_egress_warnings_handles_libpq_style_dsn():
    settings = Settings(daytona_network_block_all=True, daytona_network_allow_list=["10.0.0.0/8"])

    warnings = egress_warnings(
        {"PGCONN": "host=203.0.113.9 port=5432 dbname=demo user=app"}, settings
    )

    assert len(warnings) == 1
    assert "203.0.113.9" in warnings[0]
    assert "5432" in warnings[0]
    assert "app" not in warnings[0]


# --- API-level: affected_services + audit metadata -------------------------------------------


async def test_put_env_var_response_includes_affected_services_for_matching_scope(db_session):
    _user, workspace = await make_workspace(db_session)
    web = await make_service_command(db_session, workspace, name="Web", port=5173)
    await make_running_service(db_session, workspace, custom_command_id=web.id)

    app.dependency_overrides[auth_module.get_current_user] = _override_current_user(_user)
    app.dependency_overrides[get_db_session] = _override_db(db_session)
    app.dependency_overrides[workspaces_module.get_workspace_service] = (
        _override_workspace_service(db_session)
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                f"/workspaces/{workspace.id}/env-vars",
                json={"key": "SHARED_KEY", "value": "v1"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["affected_services"]) == 1
    assert payload["affected_services"][0]["custom_command_id"] == str(web.id)
    assert payload["affected_services"][0]["name"] == "Web"


async def test_put_env_var_response_has_no_affected_services_for_a_different_scope(db_session):
    _user, workspace = await make_workspace(db_session)
    web = await make_service_command(db_session, workspace, name="Web", port=5173)
    await make_running_service(db_session, workspace, custom_command_id=web.id)

    app.dependency_overrides[auth_module.get_current_user] = _override_current_user(_user)
    app.dependency_overrides[get_db_session] = _override_db(db_session)
    app.dependency_overrides[workspaces_module.get_workspace_service] = (
        _override_workspace_service(db_session)
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Scoped to a *different* (non-existent-as-running) command id: no active row
            # shares that custom_command_id, so nothing is affected.
            other_command = await make_service_command(db_session, workspace, name="Other")
            response = await client.put(
                f"/workspaces/{workspace.id}/env-vars",
                json={
                    "key": "ONLY_OTHER",
                    "value": "v1",
                    "custom_command_id": str(other_command.id),
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["affected_services"] == []


async def test_audit_metadata_persists_variable_name_but_never_the_value(db_session):
    user, workspace = await make_workspace(db_session)

    app.dependency_overrides[auth_module.get_current_user] = _override_current_user(user)
    app.dependency_overrides[get_db_session] = _override_db(db_session)
    app.dependency_overrides[workspaces_module.get_workspace_service] = (
        _override_workspace_service(db_session)
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                f"/workspaces/{workspace.id}/env-vars",
                json={"key": "DATABASE_PASSWORD", "value": "super-secret-value"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.workspace_id == workspace.id,
            AuditLog.action == "workspace_env_var_upsert",
        )
    )
    audit_log = result.scalar_one()
    # This is the bug the epic fixes: `metadata={"key": ...}` used to be silently dropped by
    # `safe_metadata` (any field whose name contains "key" is stripped) -- renaming the field to
    # "variable" is what makes it actually persist.
    assert audit_log.log_metadata.get("variable") == "DATABASE_PASSWORD"
    assert "super-secret-value" not in str(audit_log.log_metadata)
