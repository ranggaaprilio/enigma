"""Epic 13: the composed preview descriptor.

Pure functions (`classify_service`, `entry_point_service`) are tested standalone; the service
itself is tested against real Postgres, reusing the same `db_session` fixture pattern as
`test_workspace_services.py` and `test_preview_alias.py`.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.security import hash_password
from app.models import CustomCommand, User, Workspace
from app.services.preview_alias import PreviewAliasService
from app.services.preview_composition import (
    PreviewCompositionService,
    classify_service,
    entry_point_service,
)
from app.services.sandbox import SandboxRuntimeError
from app.services.workspace_services import WorkspaceServiceManager

# ---------------------------------------------------------------------------
# classify_service (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "port", "expected"),
    [
        ("vite --host 0.0.0.0", None, "web"),
        ("npx next dev", None, "web"),
        ("uvicorn app.main:app --port 8000", 8000, "api"),
        ("python manage.py runserver", None, "api"),
        (None, 5173, "web"),
        (None, 8000, "api"),
        (None, 9999, "other"),
        ("node server.js", 9999, "other"),
    ],
)
def test_classify_service(command, port, expected):
    assert classify_service(command=command, port=port) == expected


def test_classify_service_signature_wins_over_port_ambiguity():
    """Spring Boot's own signature resolves the 8080 ambiguity without a special case."""
    assert classify_service(command="./gradlew bootRun", port=8080) == "api"


# ---------------------------------------------------------------------------
# entry_point_service (pure)
# ---------------------------------------------------------------------------


def test_entry_point_explicit_wins():
    services = [
        {
            "id": "a", "is_entry_point": False, "role": "dev",
            "status": "running", "command": "vite", "port": 5173,
        },
        {
            "id": "b", "is_entry_point": True, "role": "custom",
            "status": "stopped", "command": None, "port": None,
        },
    ]
    service_id, source = entry_point_service(services)
    assert service_id == "b"
    assert source == "explicit"


def test_entry_point_falls_back_to_the_single_dev_role_service():
    services = [
        {
            "id": "a", "is_entry_point": False, "role": "dev",
            "status": "stopped", "command": None, "port": None,
        },
        {
            "id": "b", "is_entry_point": False, "role": "custom",
            "status": "stopped", "command": None, "port": None,
        },
    ]
    service_id, source = entry_point_service(services)
    assert service_id == "a"
    assert source == "inferred"


def test_entry_point_falls_back_to_the_single_running_web_service():
    services = [
        {
            "id": "a", "is_entry_point": False, "role": "custom",
            "status": "running", "command": "vite", "port": 5173,
        },
        {
            "id": "b", "is_entry_point": False, "role": "custom",
            "status": "running", "command": "uvicorn app:app", "port": 8000,
        },
    ]
    service_id, source = entry_point_service(services)
    assert service_id == "a"  # only the web one, not the api one
    assert source == "inferred"


def test_entry_point_falls_back_to_the_only_running_service():
    services = [
        {
            "id": "a", "is_entry_point": False, "role": "custom",
            "status": "running", "command": "node server.js", "port": 9999,
        },
        {
            "id": "b", "is_entry_point": False, "role": "custom",
            "status": "stopped", "command": None, "port": None,
        },
    ]
    service_id, source = entry_point_service(services)
    assert service_id == "a"
    assert source == "inferred"


def test_entry_point_none_when_nothing_qualifies():
    services = [
        {
            "id": "a", "is_entry_point": False, "role": "custom",
            "status": "stopped", "command": None, "port": None,
        },
        {
            "id": "b", "is_entry_point": False, "role": "custom",
            "status": "stopped", "command": None, "port": None,
        },
    ]
    service_id, source = entry_point_service(services)
    assert service_id is None
    assert source == "inferred"


# ---------------------------------------------------------------------------
# PreviewCompositionService: real Postgres
# ---------------------------------------------------------------------------


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


async def _make_workspace(
    session, *, preview_alias_enabled: bool = False
) -> tuple[User, Workspace]:
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
        sandbox_id=f"sandbox-{uuid4().hex[:8]}",
        workspace_path="/workspace",
        status="ready",
        preview_alias_enabled=preview_alias_enabled,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(user)
    session.add(workspace)
    await session.flush()
    return user, workspace


class FailingRuntime:
    """Any exec must raise -- proves `check_proxy=False` never touches the sandbox."""

    async def exec(self, *args, **kwargs):
        raise SandboxRuntimeError("must not be called")

    async def preview_link(self, *args, **kwargs):
        raise SandboxRuntimeError("must not be called")

    async def close(self) -> None:
        return None


async def test_compose_with_check_proxy_false_never_touches_the_sandbox(db_session):
    user, workspace = await _make_workspace(db_session)
    command = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name="Web",
        command="vite --host 0.0.0.0",
        role="dev",
        kind="service",
        enabled=True,
    )
    db_session.add(command)
    await db_session.flush()

    settings = get_settings()
    runtime = FailingRuntime()
    manager = WorkspaceServiceManager(db_session, settings, runtime)
    alias_service = PreviewAliasService(db_session, settings)
    composition_service = PreviewCompositionService(
        db_session, settings, service_manager=manager, alias_service=alias_service
    )

    result = await composition_service.compose(workspace.id, user.id, check_proxy=False)

    assert result.workspace_id == workspace.id
    assert len(result.services) >= 1


async def test_compose_alias_url_survives_the_service_being_stopped(db_session):
    """The stability property: alias_url stays populated even when the service (and therefore
    its rotating capability_url) is not currently running."""
    user, workspace = await _make_workspace(db_session, preview_alias_enabled=True)
    command = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name="API",
        command="uvicorn app.main:app",
        role="custom",
        kind="service",
        enabled=True,
    )
    db_session.add(command)
    await db_session.flush()

    settings = get_settings()
    alias_service = PreviewAliasService(db_session, settings)
    await alias_service.enable(workspace)

    manager = WorkspaceServiceManager(db_session, settings, FailingRuntime())
    composition_service = PreviewCompositionService(
        db_session, settings, service_manager=manager, alias_service=alias_service
    )

    result = await composition_service.compose(workspace.id, user.id, check_proxy=False)
    api_service = next(s for s in result.services if s.custom_command_id == command.id)

    assert api_service.status != "running"
    assert api_service.capability_url is None
    assert api_service.alias_url is not None  # stable, regardless of run state


async def test_compose_alias_url_is_none_when_aliases_disabled(db_session):
    user, workspace = await _make_workspace(db_session, preview_alias_enabled=False)
    settings = get_settings()
    manager = WorkspaceServiceManager(db_session, settings, FailingRuntime())
    alias_service = PreviewAliasService(db_session, settings)
    composition_service = PreviewCompositionService(
        db_session, settings, service_manager=manager, alias_service=alias_service
    )

    result = await composition_service.compose(workspace.id, user.id, check_proxy=False)

    assert result.preview_alias_enabled is False
    for service in result.services:
        assert service.alias_url is None
        assert service.alias_prime_url is None


async def test_compose_infers_the_builtin_dev_row_as_entry_point_when_nothing_is_explicit(
    db_session,
):
    """`WorkspaceServiceManager.list()` always ensures a primary row exists (the built-in mise
    `role='dev'` row, absent a `role='dev'` command) -- so with one *other*, non-dev service
    and nothing marked explicitly, the single dev-role row is what `entry_point_service` infers,
    which is the sensible default: the dev server is the thing you'd want "Open app" to open."""
    user, workspace = await _make_workspace(db_session)
    command = CustomCommand(
        id=uuid4(),
        workspace_id=workspace.id,
        name="API",
        command="uvicorn app.main:app",
        role="custom",
        kind="service",
        enabled=True,
    )
    db_session.add(command)
    await db_session.flush()

    settings = get_settings()
    manager = WorkspaceServiceManager(db_session, settings, FailingRuntime())
    composition_service = PreviewCompositionService(db_session, settings, service_manager=manager)

    result = await composition_service.compose(workspace.id, user.id, check_proxy=False)

    entry = next(s for s in result.services if s.is_entry_point)
    assert entry.role == "dev"
    assert entry.entry_point_source == "inferred"
    assert result.entry_point_service_id == entry.service_id
    # An entry point *was* found (inferred), so the "unset" advisory must not fire.
    codes = {a.code for a in result.advisories}
    assert "entry_point_unset" not in codes
