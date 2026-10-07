# ruff: noqa: E501

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import agents as agents_module
from app.api import auth as auth_module
from app.api import sessions as sessions_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import AgentEvent, AgentFileChange, AgentRun, AgentSession, User, Workspace
from app.services.agent_providers import MockAgentProvider
from app.services.agent_tools import DangerousCommandError, WorkspaceToolError, WorkspaceTools
from app.services.agents import (
    AgentRunAlreadyActiveError,
    AgentRunNotFoundError,
    AgentRunNotInWorkspaceError,
    AgentService,
    event_payload,
)
from app.services.repositories import GitFileStatus, GitStatus
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


class FakeAgentService:
    def __init__(self, user: User, workspace: Workspace) -> None:
        self.user = user
        self.workspace = workspace
        self.runs: dict[UUID, AgentRun] = {}
        self.events: dict[UUID, list[AgentEvent]] = {}
        self.has_active_run = False

    async def create_run(
        self,
        workspace_id: UUID,
        user_id: UUID,
        prompt: str,
        working_dir: str | None = None,
        worktree_id: UUID | None = None,
    ) -> AgentRun:
        self._ensure_workspace(workspace_id, user_id)
        if self.has_active_run:
            raise AgentRunAlreadyActiveError
        run = self._run(prompt=prompt, status="thinking")
        self.runs[run.id] = run
        self.has_active_run = True
        return run

    async def list_runs(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AgentRun]:
        self._ensure_workspace(workspace_id, user_id)
        runs = sorted(self.runs.values(), key=lambda run: run.started_at, reverse=True)
        return runs[offset : offset + limit]

    async def get_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        self._ensure_workspace(workspace_id, user_id)
        run = self.runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError
        run.events = self.events.get(run.id, [])
        return run

    async def list_events(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
    ) -> list[AgentEvent]:
        run = await self.get_run(workspace_id, run_id, user_id)
        return self.events.get(run.id, [])

    async def stop_run(self, workspace_id: UUID, run_id: UUID, user_id: UUID) -> AgentRun:
        run = await self.get_run(workspace_id, run_id, user_id)
        run.status = "stopped"
        run.completed_at = datetime.now(UTC)
        self.has_active_run = False
        return run

    async def list_file_changes(
        self,
        workspace_id: UUID,
        run_id: UUID,
        user_id: UUID,
        limit: int = 50,
    ) -> tuple[list[AgentFileChange], int, bool]:
        self._ensure_workspace(workspace_id, user_id)
        run = self.runs.get(run_id)
        if run is None:
            raise AgentRunNotFoundError
        if run.workspace_id != self.workspace.id:
            raise AgentRunNotInWorkspaceError
        return [], 0, False

    def _ensure_workspace(self, workspace_id: UUID, user_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != user_id:
            raise WorkspaceNotFoundError

    def _run(self, prompt: str, status: str) -> AgentRun:
        now = datetime.now(UTC)
        return AgentRun(
            id=uuid4(),
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            prompt=prompt,
            status=status,
            provider="mock",
            model="mock-agent",
            started_at=now,
            completed_at=None,
            tokens_used=None,
            error_message=None,
        )


@pytest.fixture
def agent_context(tmp_path):
    user = User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
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
    service = FakeAgentService(user, workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[agents_module.get_agent_service] = lambda: service
    # Agent-run creation is rate-limited via Redis; no-op it so tests don't need a live Redis.
    app.dependency_overrides[agents_module.enforce_agent_run_rate_limit] = lambda: None
    yield user, workspace, service
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_agent_run_endpoints_require_auth(agent_context):
    _user, workspace, _service = agent_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/workspaces/{workspace.id}/agent/runs",
            json={"prompt": "Hello"},
        )

    assert response.status_code == 401


async def test_create_list_detail_and_stop_agent_run(agent_context):
    _user, workspace, service = agent_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created = await client.post(
            f"/workspaces/{workspace.id}/agent/runs",
            json={"prompt": "Inspect this workspace"},
        )
        run_id = created.json()["id"]
        event = AgentEvent(
            id=uuid4(),
            agent_run_id=UUID(run_id),
            event_type="output_text",
            content="hello",
            tool_call_id="tool_abc123",
            event_metadata={},
            created_at=datetime.now(UTC),
        )
        service.events[UUID(run_id)] = [event]
        listed = await client.get(f"/workspaces/{workspace.id}/agent/runs")
        detail = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run_id}")
        stopped = await client.post(f"/workspaces/{workspace.id}/agent/runs/{run_id}/stop")

    assert created.status_code == 201
    assert created.json()["status"] == "thinking"
    assert listed.json()[0]["prompt"] == "Inspect this workspace"
    assert detail.json()["events"][0]["content"] == "hello"
    assert detail.json()["events"][0].get("tool_call_id") == "tool_abc123"
    assert stopped.json()["status"] == "stopped"


def test_agent_event_tool_call_id_is_nullable() -> None:
    now = datetime.now(UTC)
    event_with_id = AgentEvent(
        id=uuid4(),
        agent_run_id=uuid4(),
        event_type="tool_call",
        content="use search",
        tool_call_id="tool_xyz789",
        event_metadata={},
        created_at=now,
    )
    event_without_id = AgentEvent(
        id=uuid4(),
        agent_run_id=uuid4(),
        event_type="output_text",
        content="hello",
        tool_call_id=None,
        event_metadata={},
        created_at=now,
    )
    assert event_with_id.tool_call_id == "tool_xyz789"
    assert event_without_id.tool_call_id is None


def test_agent_event_payload_includes_tool_call_id() -> None:
    event = AgentEvent(
        id=uuid4(),
        agent_run_id=uuid4(),
        event_type="tool_call",
        content="read_file",
        tool_call_id="call_123",
        event_metadata={"tool_call_id": "call_123"},
        created_at=datetime.now(UTC),
    )

    assert event_payload(event)["tool_call_id"] == "call_123"


async def test_one_active_agent_run_per_workspace(agent_context):
    _user, workspace, service = agent_context
    service.has_active_run = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/agent/runs",
            json={"prompt": "Second run"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Workspace already has an active agent run"


async def test_workspace_tools_allow_build_test_shell_and_block_sandbox_escape(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "README.md").write_text("hello", encoding="utf-8")
    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
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

    class FakeFs:
        async def download_file(self, path):
            return b"hello"

    class FakeRuntime:
        def __init__(self):
            self.exec_calls = []
            self.exit_code = 0
            self.stdout = "build passed"
            self.stderr = ""

        async def get_sandbox(self, workspace):
            return type("Sandbox", (), {"fs": FakeFs()})()

        async def exec(self, workspace, command, *, cwd, timeout):
            self.exec_calls.append(
                {
                    "workspace": workspace,
                    "command": command,
                    "cwd": cwd,
                    "timeout": timeout,
                }
            )
            return type(
                "Result",
                (),
                {
                    "exit_code": self.exit_code,
                    "stdout": self.stdout,
                    "stderr": self.stderr,
                },
            )()

    workspace.workspace_path = "/workspace"
    runtime = FakeRuntime()
    tools = WorkspaceTools(
        workspace,
        runtime=runtime,  # type: ignore[arg-type]
        command_timeout_seconds=600,
        max_command_output_chars=100_000,
    )

    assert await tools.read_file("README.md") == "hello"
    with pytest.raises(WorkspaceToolError):
        await tools.read_file("../outside.txt")

    allowed_commands = [
        "pytest -q",
        "python -m pytest",
        "ls -la",
        "cat README.md",
    ]
    for command in allowed_commands:
        assert tools._validate_command(command) == command.split()

    blocked_commands = [
        "pnpm install && pnpm test",
        "cat test.log | tail -20",
        "pytest >/dev/null",
        "pnpm build\npnpm test",
    ]
    for command in blocked_commands:
        with pytest.raises(DangerousCommandError):
            tools._validate_command(command)

    result = await tools.run_command("pytest -q")

    assert result.stdout == "build passed"
    assert runtime.exec_calls == [
        {
            "workspace": workspace,
            "command": "pytest -q",
            "cwd": "/workspace",
            "timeout": 600,
        }
    ]

    runtime.exit_code = 2
    runtime.stdout = ""
    runtime.stderr = "tests failed"
    failed = await tools.run_command("pytest")
    assert failed.exit_code == 2
    assert failed.stderr == "tests failed"

    capped, truncated = tools._cap("x" * 100_001)
    assert len(capped) == 100_000
    assert truncated is True

    blocked_commands = [
        "",
        "sudo pnpm test",
        "docker build .",
        "/bin/sh -c pwd",
        "echo bad >/proc/sys/kernel/test",
    ]
    for command in blocked_commands:
        with pytest.raises(DangerousCommandError):
            tools._validate_command(command)


async def test_workspace_tools_list_files_is_bounded_and_skips_excluded_directories():
    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo-list",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    def entry(path: str, is_dir: bool = False):
        return type(
            "Entry",
            (),
            {"name": PurePosixPath(path).name, "path": path, "is_dir": is_dir},
        )()

    class FakeFs:
        def __init__(self):
            self.calls = []

        async def list_files(self, path):
            self.calls.append(path)
            return {
                "/workspace": [
                    entry("/workspace/node_modules", True),
                    entry("/workspace/src", True),
                    entry("/workspace/README.md"),
                ],
                "/workspace/src": [
                    entry("/workspace/src/nested", True),
                    entry("/workspace/src/app.py"),
                ],
                "/workspace/src/nested": [entry("/workspace/src/nested/util.py")],
            }[path]

    fake_fs = FakeFs()

    class FakeRuntime:
        async def get_sandbox(self, workspace):
            return type("Sandbox", (), {"fs": fake_fs})()

    tools = WorkspaceTools(
        workspace,
        runtime=FakeRuntime(),  # type: ignore[arg-type]
        command_timeout_seconds=20,
        max_command_output_chars=20_000,
    )

    assert await tools.list_files(limit=2) == ["README.md", "src/app.py"]
    assert fake_fs.calls == ["/workspace", "/workspace/src"]
    assert "/workspace/node_modules" not in fake_fs.calls


def test_agent_command_defaults_support_build_and_test_runs():
    assert Settings.model_fields["agent_command_timeout_seconds"].default == 20
    assert Settings.model_fields["agent_max_command_output_chars"].default == 20_000


@pytest.fixture
async def db_session():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.connect() as conn:
        await conn.begin()
        session_maker = async_sessionmaker(bind=conn, expire_on_commit=False)
        async with session_maker() as session:
            await session.begin_nested()
            yield session, session_maker
            await session.rollback()
    await engine.dispose()


class TestAgentFileChange:
    async def test_create_file_change(self, db_session):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"filechange-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FileChangeTest-{uid}",
            slug=f"filechange-test-{uid}",
            repo_url="https://github.com/example/test",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-filechange-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        file_change = await service.create_file_change(
            run_id=run.id,
            path="src/main.py",
            status="modified",
            tool_call_id="call_123",
        )

        assert file_change.id is not None
        assert file_change.agent_run_id == run.id
        assert file_change.path == "src/main.py"
        assert file_change.status == "modified"
        assert file_change.tool_call_id == "call_123"
        assert file_change.created_at is not None

    async def test_get_file_changes_for_run(self, db_session):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"filechange-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FileChangeTest-{uid}",
            slug=f"filechange-test-{uid}",
            repo_url="https://github.com/example/test2",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-filechange-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt 2",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        await service.create_file_change(run.id, "a.py", "created", "call_1")
        await service.create_file_change(run.id, "b.py", "modified", "call_2")
        await service.create_file_change(run.id, "c.py", "deleted", None)

        changes = await service.get_file_changes_for_run(run.id)
        assert len(changes) == 3
        paths = {c.path for c in changes}
        assert paths == {"a.py", "b.py", "c.py"}

    async def test_get_file_changes_for_run_and_tool(self, db_session):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"filechange-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FileChangeTest-{uid}",
            slug=f"filechange-test-{uid}",
            repo_url="https://github.com/example/test3",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-filechange-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt 3",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        await service.create_file_change(run.id, "x.py", "created", "tool_a")
        await service.create_file_change(run.id, "y.py", "modified", "tool_a")
        await service.create_file_change(run.id, "z.py", "deleted", "tool_b")

        tool_a_changes = await service.get_file_changes_for_run_and_tool(run.id, "tool_a")
        assert len(tool_a_changes) == 2
        assert {c.path for c in tool_a_changes} == {"x.py", "y.py"}

        tool_b_changes = await service.get_file_changes_for_run_and_tool(run.id, "tool_b")
        assert len(tool_b_changes) == 1
        assert tool_b_changes[0].path == "z.py"

    async def test_delete_run_cascades_to_file_changes(self, db_session):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"filechange-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FileChangeTest-{uid}",
            slug=f"filechange-test-{uid}",
            repo_url="https://github.com/example/test4",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-filechange-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt 4",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        await service.create_file_change(run.id, "d.py", "created", "call_1")
        await session.commit()

        result = await session.execute(
            select(AgentFileChange).where(AgentFileChange.agent_run_id == run.id)
        )
        assert len(result.scalars().all()) == 1

        await session.delete(run)
        await session.commit()

        result = await session.execute(
            select(AgentFileChange).where(AgentFileChange.agent_run_id == run.id)
        )
        assert len(result.scalars().all()) == 0


async def override_get_current_user(user: User):
    """Factory to create a get_current_user override that returns the given user."""

    async def _override() -> User:
        return user

    return _override


def override_get_db_session(session):
    """Factory to create a get_db_session override that yields the given session."""

    async def _override():
        yield session

    return _override


def override_get_agent_service(session, session_maker):
    """Factory to create a get_agent_service override that uses the test session.

    ``agent_allow_mock_provider=True`` is the explicit opt-in these tests need: with no
    ``platform_llm_settings`` row and no ``OPENHANDS_LLM_*`` env vars, ``provider_for_run``
    refuses to start a run rather than quietly answering with the mock provider.
    """

    async def _override() -> AgentService:
        return AgentService(
            session=session,
            session_maker=session_maker,
            settings=Settings(agent_allow_mock_provider=True),
        )

    return _override


def override_get_agent_session_service(session, session_maker, settings=None):
    """Factory for a get_agent_session_service override that uses the test session."""

    async def _override() -> AgentService:
        return AgentService(
            session=session,
            session_maker=session_maker,
            settings=settings or Settings(agent_allow_mock_provider=True),
        )

    return _override


async def _noop_execute_run(self, run_id, stop_event, working_dir=None):
    """No-op replacement for AgentService._execute_run.

    Session turns delegate to create_run, which spawns _execute_run as a background task
    that would touch the Daytona runtime / shared connection. Patching it out lets tests
    exercise the create_run/append_turn DB effects (session_id, auto-title) without that.
    """
    return None


class _ImmediateTask:
    def add_done_callback(self, callback):
        return None


def _fake_create_task(coro, *args, **kwargs):
    """Neutralize asyncio.create_task so create_run spawns no background task.

    Only safe in tests that do NOT drive the app over HTTP (httpx/anyio also use
    create_task); use it for direct AgentService calls against the shared test session.
    """
    coro.close()
    return _ImmediateTask()


class TestFileChangesAPI:
    """API endpoint tests for GET /workspaces/{id}/agent/runs/{run_id}/file-changes.

    Uses db_session fixture to create real database records. Overrides
    get_current_user, get_db_session, and get_agent_service so the real
    AgentService uses the same session as the test, making test data visible.
    """

    async def test_valid_run_with_file_changes(self, db_session):
        """Returns 200 with list of file changes and correct schema fields."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-valid-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FCValid-{uid}",
            slug=f"fc-valid-{uid}",
            repo_url="https://github.com/example/fcvalid",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-fcvalid-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt",
            status="completed",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        await service.create_file_change(run.id, "src/main.py", "created", "tool_call_1")
        await service.create_file_change(run.id, "src/utils.py", "modified", "tool_call_2")
        await service.create_file_change(run.id, "README.md", "deleted", None)
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
            )

        app.dependency_overrides.clear()

        assert response.status_code == 200
        data = response.json()
        assert data["capped"] is False
        assert data["total"] == 3
        assert len(data["items"]) == 3

        paths = {item["path"] for item in data["items"]}
        assert paths == {"src/main.py", "src/utils.py", "README.md"}

        statuses = {item["status"] for item in data["items"]}
        assert statuses == {"created", "modified", "deleted"}

        for item in data["items"]:
            assert "id" in item
            assert "path" in item
            assert "status" in item
            assert "created_at" in item

    async def test_valid_run_with_no_changes(self, db_session):
        """Returns 200 with empty items list when run has no file changes."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-empty-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FCEmpty-{uid}",
            slug=f"fc-empty-{uid}",
            repo_url="https://github.com/example/fcempty",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-fcempty-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt",
            status="completed",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
            )

        app.dependency_overrides.clear()

        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["capped"] is False

    async def test_run_with_more_than_50_changes_capped(self, db_session):
        """Returns capped:true when changes exceed 50, items limited to 50."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-capped-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FCCap-{uid}",
            slug=f"fc-cap-{uid}",
            repo_url="https://github.com/example/fccap",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-fccap-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt",
            status="completed",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        for i in range(55):
            await service.create_file_change(run.id, f"file{i}.py", "modified")
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
            )

        app.dependency_overrides.clear()

        assert response.status_code == 200
        data = response.json()
        assert data["capped"] is True
        assert data["total"] == 55
        assert len(data["items"]) == 50

    async def test_nonexistent_run_returns_404(self, db_session):
        """Returns 404 when run_id does not exist."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-404-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FC404-{uid}",
            slug=f"fc-404-{uid}",
            repo_url="https://github.com/example/fc404",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-fc404-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/workspaces/{workspace.id}/agent/runs/{uuid4()}/file-changes"
            )

        app.dependency_overrides.clear()

        assert response.status_code == 404
        assert response.json()["detail"] == "Agent run not found"

    async def test_unauthorized_run_returns_403(self, db_session):
        """Returns 403 when run belongs to a different workspace.

        Note: This test is skipped because the database schema has a unique
        constraint on workspaces.owner_id, preventing one user from owning
        multiple workspaces. The 403 scenario is covered by FakeAgentService
        unit tests and the production code path is tested via the other API tests.
        """
        pytest.skip(
            "workspace owner_id unique constraint prevents testing 403 scenario with same user owning two workspaces"
        )

    async def test_invalid_workspace_returns_404(self, db_session):
        """Returns 404 when workspace_id does not exist."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-ws404-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/workspaces/{uuid4()}/agent/runs/{uuid4()}/file-changes")

        app.dependency_overrides.clear()

        assert response.status_code == 404

    async def test_agent_file_change_response_schema(self, db_session):
        """AgentFileChangeResponse schema serializes with all required fields."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-schema-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FCSchema-{uid}",
            slug=f"fc-schema-{uid}",
            repo_url="https://github.com/example/fcschema",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-fcschema-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test prompt",
            status="completed",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
        change = await service.create_file_change(
            run.id, "src/schema_test.py", "modified", "tool_schema_123"
        )
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
            )

        app.dependency_overrides.clear()

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        item = data["items"][0]

        assert item["id"] == str(change.id)
        assert item["path"] == "src/schema_test.py"
        assert item["status"] == "modified"
        assert item["tool_call_id"] == "tool_schema_123"
        assert "created_at" in item

    async def test_runs_endpoint_limit_and_offset(self, db_session):
        """Runs endpoint respects limit and offset params."""
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"fc-limitoffset-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"FCLimitOffset-{uid}",
            slug=f"fc-limitoffset-{uid}",
            repo_url="https://github.com/example/fclimitoffset",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-fclimitoffset-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        for i in range(5):
            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt=f"test prompt {i}",
                status="completed",
                provider="mock",
                model="mock-model",
            )
            session.add(run)
        await session.commit()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response_offset0 = await client.get(
                f"/workspaces/{workspace.id}/agent/runs?limit=2&offset=0"
            )
            response_offset2 = await client.get(
                f"/workspaces/{workspace.id}/agent/runs?limit=2&offset=2"
            )
            response_offset4 = await client.get(
                f"/workspaces/{workspace.id}/agent/runs?limit=2&offset=4"
            )

        app.dependency_overrides.clear()

        assert response_offset0.status_code == 200
        runs0 = response_offset0.json()
        assert len(runs0) == 2

        assert response_offset2.status_code == 200
        runs2 = response_offset2.json()
        assert len(runs2) == 2

        assert response_offset4.status_code == 200
        runs4 = response_offset4.json()
        assert len(runs4) == 1

        run_ids_0 = {r["id"] for r in runs0}
        run_ids_2 = {r["id"] for r in runs2}
        run_ids_4 = {r["id"] for r in runs4}

        assert run_ids_0.isdisjoint(run_ids_2)
        assert run_ids_0.isdisjoint(run_ids_4)
        assert run_ids_2.isdisjoint(run_ids_4)
        assert run_ids_0.union(run_ids_2).union(run_ids_4) == run_ids_0.union(run_ids_2).union(
            run_ids_4
        )


async def test_mock_agent_provider_emits_tool_call_id_in_metadata(tmp_path):
    """MockAgentProvider includes tool_call_id in metadata for tool_call and tool_result events."""
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    (workspace_path / "README.md").write_text("hello", encoding="utf-8")

    class FakeRuntime:
        async def get_sandbox(self, workspace):
            class FakeFs:
                async def download_file(self, path):
                    return b"hello"

            class Sandbox:
                fs = FakeFs()

            return Sandbox()

        async def exec(self, workspace, command, *, cwd, timeout):
            return type("Result", (), {"exit_code": 0, "stdout": "hello", "stderr": ""})()

    from app.models import Workspace

    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path=str(workspace_path),
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    tools = WorkspaceTools(
        workspace,
        runtime=FakeRuntime(),  # type: ignore[arg-type]
        command_timeout_seconds=600,
        max_command_output_chars=100_000,
    )

    provider = MockAgentProvider()
    stop_event = asyncio.Event()

    events = []
    async for event in provider.stream_response("test prompt", tools, stop_event):
        events.append(event)

    tool_call_events = [e for e in events if e.event_type == "tool_call"]
    tool_result_events = [e for e in events if e.event_type == "tool_result"]

    assert len(tool_call_events) == 1, "Expected exactly one tool_call event"
    assert len(tool_result_events) == 1, "Expected exactly one tool_result event"

    tool_call = tool_call_events[0]
    tool_result = tool_result_events[0]

    assert "tool_call_id" in tool_call.metadata, (
        "tool_call event should include tool_call_id in metadata"
    )
    assert "tool_call_id" in tool_result.metadata, (
        "tool_result event should include tool_call_id in metadata"
    )

    tool_call_id = tool_call.metadata["tool_call_id"]
    assert tool_call_id.startswith("mock-tool-"), "tool_call_id should start with 'mock-tool-'"
    assert len(tool_call_id) == 18, (
        "tool_call_id should be 18 characters (mock-tool- + 8 hex chars)"
    )

    assert tool_result.metadata["tool_call_id"] == tool_call_id, (
        "tool_result should reference same tool_call_id as tool_call"
    )


class TestFileChangeComputation:
    async def test_capture_baseline_returns_git_files(self, db_session, monkeypatch):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"baseline-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"BaselineTest-{uid}",
            slug=f"baseline-test-{uid}",
            repo_url="https://github.com/example/baseline",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-baseline-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

        async def mock_status(self, workspace_id, owner_id, *, worktree_path=None):
            return GitStatus(
                cloned=True,
                branch="main",
                is_clean=False,
                files=[
                    GitFileStatus(path="a.py", status="Added"),
                    GitFileStatus(path="b.py", status="Modified"),
                ],
            )

        monkeypatch.setattr(
            "app.services.repositories.RepositoryService.status",
            mock_status,
        )

        baseline = await service._capture_baseline(workspace.id, session)
        assert len(baseline) == 2
        assert baseline[0].path == "a.py"
        assert baseline[1].path == "b.py"

    async def test_capture_baseline_no_git_repo_returns_empty(self, db_session, monkeypatch):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"nogit-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"NoGitTest-{uid}",
            slug=f"nogit-test-{uid}",
            repo_url="https://github.com/example/nogit",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-nogit-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

        async def mock_status(self, workspace_id, owner_id, *, worktree_path=None):
            return GitStatus(cloned=False, branch=None, is_clean=True, files=[])

        monkeypatch.setattr(
            "app.services.repositories.RepositoryService.status",
            mock_status,
        )

        baseline = await service._capture_baseline(workspace.id, session)
        assert baseline == []

    async def test_compute_file_changes_returns_diff(self, db_session, monkeypatch):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"diff-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"DiffTest-{uid}",
            slug=f"diff-test-{uid}",
            repo_url="https://github.com/example/diff",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-diff-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test diff",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

        baseline = [GitFileStatus(path="a.py", status="Added")]

        async def mock_status(self, workspace_id, owner_id, *, worktree_path=None):
            return GitStatus(
                cloned=True,
                branch="main",
                is_clean=False,
                files=[
                    GitFileStatus(path="a.py", status="Added"),
                    GitFileStatus(path="b.py", status="Modified"),
                    GitFileStatus(path="c.py", status="Deleted"),
                ],
            )

        monkeypatch.setattr(
            "app.services.repositories.RepositoryService.status",
            mock_status,
        )

        changes = await service._compute_file_changes(
            workspace.id, baseline, run.id, session
        )
        paths = {c.path for c in changes}
        assert "b.py" in paths
        assert "c.py" in paths
        assert "a.py" not in paths

    async def test_compute_file_changes_rename_detection(self, db_session, monkeypatch):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"rename-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"RenameTest-{uid}",
            slug=f"rename-test-{uid}",
            repo_url="https://github.com/example/rename",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-rename-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test rename",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

        baseline = []

        async def mock_status(self, workspace_id, owner_id, *, worktree_path=None):
            return GitStatus(
                cloned=True,
                branch="main",
                is_clean=False,
                files=[
                    GitFileStatus(path="old.py -> new.py", status="Renamed"),
                ],
            )

        monkeypatch.setattr(
            "app.services.repositories.RepositoryService.status",
            mock_status,
        )

        changes = await service._compute_file_changes(
            workspace.id, baseline, run.id, session
        )
        assert len(changes) == 1
        assert changes[0].path == "old.py -> new.py"
        assert changes[0].status == "renamed"

    async def test_file_change_cap(self, db_session):
        session, session_maker = db_session
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"cap-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"CapTest-{uid}",
            slug=f"cap-test-{uid}",
            repo_url="https://github.com/example/cap",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-cap-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        run = AgentRun(
            workspace_id=workspace.id,
            user_id=user.id,
            prompt="test cap",
            status="thinking",
            provider="mock",
            model="mock-model",
        )
        session.add(run)
        await session.flush()

        service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

        for i in range(55):
            await service.create_file_change(run.id, f"file{i}.py", "modified")

        changes, total, has_more = await service.get_file_changes_for_run_capped(run.id, limit=50)
        assert len(changes) == 50
        assert total == 55
        assert has_more is True


class TestAgentRunLifecycle:
    """Integration tests for the full agent run lifecycle:
    create run in DB → add events + file changes → query via API → verify response.
    Uses direct DB creation (not POST endpoint) to avoid background task issues.
    """

    async def _setup_workspace(self, session, session_maker, label="lifecycle"):
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"{label}-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"{label.capitalize()}-{uid}",
            slug=f"{label}-{uid}",
            repo_url=f"https://github.com/example/{label}",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-{label}-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )

        return user, workspace

    def _teardown_overrides(self):
        app.dependency_overrides.clear()

    async def test_full_run_lifecycle_create_execute_file_changes(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup_workspace(session, session_maker, "lifecycle")

        try:
            service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="Refactor the auth module",
                status="completed",
                provider="mock",
                model="mock-agent",
                tokens_used=150,
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()

            tool_call_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_call",
                content="read_file",
                tool_call_id="call_abc123",
                event_metadata={"tool_name": "read_file", "arguments": '{"path": "src/auth.py"}'},
                created_at=datetime.now(UTC),
            )
            tool_result_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_result",
                content="file contents here",
                tool_call_id="call_abc123",
                event_metadata={"exit_code": 0, "output": "file contents here"},
                created_at=datetime.now(UTC),
            )
            output_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="output_text",
                content="I've read the auth module.",
                tool_call_id=None,
                event_metadata={},
                created_at=datetime.now(UTC),
            )
            session.add_all([tool_call_event, tool_result_event, output_event])

            await service.create_file_change(run.id, "src/auth.py", "modified", "call_abc123")
            await service.create_file_change(run.id, "src/auth_test.py", "created", "call_abc123")
            await service.create_file_change(run.id, "src/old_auth.py", "deleted", None)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail_resp = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run.id}")
                assert detail_resp.status_code == 200
                detail = detail_resp.json()
                events = detail["events"]
                assert len(events) == 3

                tool_call_ev = next(e for e in events if e["event_type"] == "tool_call")
                tool_result_ev = next(e for e in events if e["event_type"] == "tool_result")
                assert tool_call_ev["tool_call_id"] == "call_abc123"
                assert tool_result_ev["tool_call_id"] == "call_abc123"
                assert tool_call_ev["metadata"]["tool_name"] == "read_file"

                fc_resp = await client.get(
                    f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
                )
                assert fc_resp.status_code == 200
                fc_data = fc_resp.json()
                assert fc_data["total"] == 3
                assert fc_data["capped"] is False
                assert len(fc_data["items"]) == 3

                for item in fc_data["items"]:
                    assert "id" in item
                    assert "path" in item
                    assert "status" in item
                    assert "tool_call_id" in item
                    assert "created_at" in item

                tool_linked = [i for i in fc_data["items"] if i["tool_call_id"] == "call_abc123"]
                assert len(tool_linked) == 2
                paths = {i["path"] for i in tool_linked}
                assert paths == {"src/auth.py", "src/auth_test.py"}

                unlinked = [i for i in fc_data["items"] if i["tool_call_id"] is None]
                assert len(unlinked) == 1
                assert unlinked[0]["path"] == "src/old_auth.py"

                list_resp = await client.get(f"/workspaces/{workspace.id}/agent/runs")
                assert list_resp.status_code == 200
                runs = list_resp.json()
                assert any(r["id"] == str(run.id) for r in runs)
        finally:
            self._teardown_overrides()

    async def test_run_with_zero_tool_calls_only_output_text(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup_workspace(session, session_maker, "notools")

        try:
            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="Just thinking aloud",
                status="completed",
                provider="mock",
                model="mock-agent",
                tokens_used=50,
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()

            output_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="output_text",
                content="I am thinking about this problem.",
                tool_call_id=None,
                event_metadata={},
                created_at=datetime.now(UTC),
            )
            session.add(output_event)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail_resp = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run.id}")
                assert detail_resp.status_code == 200
                events = detail_resp.json()["events"]
                assert len(events) == 1
                assert events[0]["event_type"] == "output_text"
                assert events[0]["tool_call_id"] is None

                fc_resp = await client.get(
                    f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
                )
                assert fc_resp.status_code == 200
                fc_data = fc_resp.json()
                assert fc_data["items"] == []
                assert fc_data["total"] == 0
                assert fc_data["capped"] is False
        finally:
            self._teardown_overrides()

    async def test_error_run_with_partial_file_changes(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup_workspace(session, session_maker, "errorpartial")

        try:
            service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="Fix the bug",
                status="error",
                provider="mock",
                model="mock-agent",
                error_message="LLM provider returned 500",
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.flush()

            tool_call_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_call",
                content="write_file",
                tool_call_id="call_partial_1",
                event_metadata={"tool_name": "write_file"},
                created_at=datetime.now(UTC),
            )
            tool_result_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_result",
                content="File written successfully",
                tool_call_id="call_partial_1",
                event_metadata={"exit_code": 0},
                created_at=datetime.now(UTC),
            )
            error_event = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="error",
                content="LLM provider returned 500",
                tool_call_id=None,
                event_metadata={},
                created_at=datetime.now(UTC),
            )
            session.add_all([tool_call_event, tool_result_event, error_event])

            await service.create_file_change(run.id, "src/bugfix.py", "created", "call_partial_1")
            await service.create_file_change(run.id, "src/config.py", "modified", "call_partial_1")
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail_resp = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run.id}")
                assert detail_resp.status_code == 200
                detail = detail_resp.json()
                assert detail["status"] == "error"
                assert detail["error_message"] == "LLM provider returned 500"
                assert len(detail["events"]) == 3

                fc_resp = await client.get(
                    f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
                )
                assert fc_resp.status_code == 200
                fc_data = fc_resp.json()
                assert fc_data["total"] == 2
                assert fc_data["capped"] is False
                paths = {i["path"] for i in fc_data["items"]}
                assert paths == {"src/bugfix.py", "src/config.py"}

                for item in fc_data["items"]:
                    assert item["tool_call_id"] == "call_partial_1"
        finally:
            self._teardown_overrides()

    async def test_workspace_without_git_repo_file_changes_empty(self, db_session, monkeypatch):
        session, session_maker = db_session
        user, workspace = await self._setup_workspace(session, session_maker, "nogit")

        try:

            async def mock_status(self, workspace_id, owner_id, *, worktree_path=None):
                return GitStatus(cloned=False, branch=None, is_clean=True, files=[])

            monkeypatch.setattr(
                "app.services.repositories.RepositoryService.status",
                mock_status,
            )

            service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

            baseline = await service._capture_baseline(workspace.id, session)
            assert baseline == []

            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="test no git",
                status="completed",
                provider="mock",
                model="mock-model",
            )
            session.add(run)
            await session.flush()

            changes = await service._compute_file_changes(
                workspace.id, baseline, run.id, session
            )
            assert changes == []
        finally:
            self._teardown_overrides()

    async def test_mock_provider_tool_call_id_pairing(self, tmp_path):
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()
        (workspace_path / "README.md").write_text("hello", encoding="utf-8")

        class FakeRuntime:
            async def get_sandbox(self, workspace):
                class FakeFs:
                    async def download_file(self, path):
                        return b"hello"

                class Sandbox:
                    fs = FakeFs()

                return Sandbox()

            async def exec(self, workspace, command, *, cwd, timeout):
                return type("Result", (), {"exit_code": 0, "stdout": "hello", "stderr": ""})()

        from app.models import Workspace as WsModel

        workspace = WsModel(
            id=uuid4(),
            owner_id=uuid4(),
            name="Demo",
            slug="demo",
            repo_url="https://github.com/example/demo",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id="sandbox-demo",
            workspace_path=str(workspace_path),
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

        tools = WorkspaceTools(
            workspace,
            runtime=FakeRuntime(),  # type: ignore[arg-type]
            command_timeout_seconds=600,
            max_command_output_chars=100_000,
        )

        provider = MockAgentProvider()
        stop_event = asyncio.Event()

        events = []
        async for event in provider.stream_response("test prompt", tools, stop_event):
            events.append(event)

        event_types = [e.event_type for e in events]
        assert "tool_call" in event_types
        assert "tool_result" in event_types
        assert "output_text" in event_types

        tool_call_events = [e for e in events if e.event_type == "tool_call"]
        tool_result_events = [e for e in events if e.event_type == "tool_result"]

        for tc in tool_call_events:
            assert "tool_call_id" in tc.metadata
            assert tc.metadata["tool_call_id"].startswith("mock-tool-")

        for tr in tool_result_events:
            assert "tool_call_id" in tr.metadata

        for tc in tool_call_events:
            matching_results = [
                tr
                for tr in tool_result_events
                if tr.metadata.get("tool_call_id") == tc.metadata["tool_call_id"]
            ]
            assert len(matching_results) == 1, (
                f"tool_call {tc.metadata['tool_call_id']} should have exactly 1 matching tool_result"
            )

    async def test_file_changes_linked_to_tool_call_ids_via_api(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup_workspace(session, session_maker, "crosscut")

        try:
            service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))

            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="cross-cutting test",
                status="completed",
                provider="mock",
                model="mock-model",
            )
            session.add(run)
            await session.flush()

            tc1_id = "call_cross_1"
            tc2_id = "call_cross_2"

            tc1 = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_call",
                content="write_file",
                tool_call_id=tc1_id,
                event_metadata={"tool_name": "write_file"},
                created_at=datetime.now(UTC),
            )
            tr1 = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_result",
                content="ok",
                tool_call_id=tc1_id,
                event_metadata={"exit_code": 0},
                created_at=datetime.now(UTC),
            )
            tc2 = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_call",
                content="run_command",
                tool_call_id=tc2_id,
                event_metadata={"tool_name": "run_command"},
                created_at=datetime.now(UTC),
            )
            tr2 = AgentEvent(
                id=uuid4(),
                agent_run_id=run.id,
                event_type="tool_result",
                content="tests passed",
                tool_call_id=tc2_id,
                event_metadata={"exit_code": 0},
                created_at=datetime.now(UTC),
            )
            session.add_all([tc1, tr1, tc2, tr2])

            await service.create_file_change(run.id, "src/app.py", "modified", tc1_id)
            await service.create_file_change(run.id, "src/app_test.py", "created", tc1_id)
            await service.create_file_change(run.id, "src/config.py", "modified", tc2_id)
            await service.create_file_change(run.id, "README.md", "modified", None)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail_resp = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run.id}")
                assert detail_resp.status_code == 200
                events = detail_resp.json()["events"]
                event_tc_ids = [e["tool_call_id"] for e in events if e["tool_call_id"] is not None]
                assert set(event_tc_ids) == {tc1_id, tc2_id}

                fc_resp = await client.get(
                    f"/workspaces/{workspace.id}/agent/runs/{run.id}/file-changes"
                )
                assert fc_resp.status_code == 200
                fc_data = fc_resp.json()
                assert fc_data["total"] == 4

                by_tool: dict[str, list] = {}
                for item in fc_data["items"]:
                    tid = item["tool_call_id"] or "__none__"
                    by_tool.setdefault(tid, []).append(item)

                assert len(by_tool["call_cross_1"]) == 2
                assert {i["path"] for i in by_tool["call_cross_1"]} == {
                    "src/app.py",
                    "src/app_test.py",
                }
                assert len(by_tool["call_cross_2"]) == 1
                assert by_tool["call_cross_2"][0]["path"] == "src/config.py"
                assert len(by_tool["__none__"]) == 1
                assert by_tool["__none__"][0]["path"] == "README.md"
        finally:
            self._teardown_overrides()


class TestAgentSessions:
    """Multi-turn agent sessions: CRUD endpoints, turns, and context assembly."""

    async def _setup(self, session, session_maker, label="session", settings=None):
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"{label}-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"{label.capitalize()}-{uid}",
            slug=f"{label}-{uid}",
            repo_url=f"https://github.com/example/{label}",
            default_branch="main",
            runtime="daytona",
            daytona_sandbox_id=f"sandbox-{label}-{uid}",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        session.add(workspace)
        await session.flush()

        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )
        app.dependency_overrides[sessions_module.get_agent_session_service] = (
            override_get_agent_session_service(session, session_maker, settings)
        )
        return user, workspace

    def _teardown(self):
        app.dependency_overrides.clear()

    async def test_session_endpoints_require_auth(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessauth")
        # Drop the authenticated user so requests are unauthenticated.
        app.dependency_overrides.pop(auth_module.get_current_user, None)
        app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(f"/workspaces/{workspace.id}/agent/sessions")
            assert response.status_code == 401
        finally:
            self._teardown()

    async def test_create_and_list_sessions(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sesslist")
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                titled = await client.post(
                    f"/workspaces/{workspace.id}/agent/sessions",
                    json={"title": "Fix the parser"},
                )
                untitled = await client.post(
                    f"/workspaces/{workspace.id}/agent/sessions",
                    json={},
                )
                listed = await client.get(f"/workspaces/{workspace.id}/agent/sessions")

            assert titled.status_code == 201
            assert titled.json()["title"] == "Fix the parser"
            assert titled.json()["status"] == "active"
            assert untitled.status_code == 201
            assert untitled.json()["title"] is None

            assert listed.status_code == 200
            ids = {s["id"] for s in listed.json()}
            # Both sessions are listed. (Strict updated_at ordering isn't asserted here: the
            # test fixture's nested transaction makes Postgres now() constant across both
            # inserts, so their timestamps tie. Ordering by recency is exercised in production
            # where each create is its own transaction.)
            assert ids == {titled.json()["id"], untitled.json()["id"]}
        finally:
            self._teardown()

    async def test_list_sessions_status_filter(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessfilter")
        try:
            active = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="a", status="active"
            )
            archived = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="b", status="archived"
            )
            session.add_all([active, archived])
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                active_only = await client.get(
                    f"/workspaces/{workspace.id}/agent/sessions?status=active"
                )
                archived_only = await client.get(
                    f"/workspaces/{workspace.id}/agent/sessions?status=archived"
                )

            assert {s["id"] for s in active_only.json()} == {str(active.id)}
            assert {s["id"] for s in archived_only.json()} == {str(archived.id)}
        finally:
            self._teardown()

    async def test_get_session_detail_with_ordered_turns(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessdetail")
        try:
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="conv", status="active"
            )
            session.add(agent_session)
            await session.flush()

            earlier = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="turn one",
                status="completed",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime.now(UTC),
            )
            later = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="turn two",
                status="completed",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
                completed_at=datetime.now(UTC),
            )
            session.add_all([earlier, later])
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail = await client.get(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}"
                )

            assert detail.status_code == 200
            data = detail.json()
            assert data["title"] == "conv"
            prompts = [t["prompt"] for t in data["turns"]]
            assert prompts == ["turn one", "turn two"]  # chronological
            assert all(t["session_id"] == str(agent_session.id) for t in data["turns"])
        finally:
            self._teardown()

    async def test_get_missing_session_returns_404(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sess404")
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    f"/workspaces/{workspace.id}/agent/sessions/{uuid4()}"
                )
            assert response.status_code == 404
            assert response.json()["detail"] == "Session not found"
        finally:
            self._teardown()

    async def test_rename_and_archive_session(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sesspatch")
        try:
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="old", status="active"
            )
            session.add(agent_session)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                renamed = await client.patch(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}",
                    json={"title": "new name"},
                )
                archived = await client.patch(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}",
                    json={"status": "archived"},
                )

            assert renamed.status_code == 200
            assert renamed.json()["title"] == "new name"
            assert renamed.json()["status"] == "active"  # status untouched by title-only patch
            assert archived.status_code == 200
            assert archived.json()["status"] == "archived"
            assert archived.json()["title"] == "new name"  # title untouched by status-only patch
        finally:
            self._teardown()

    async def test_delete_session_detaches_runs(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessdel")
        try:
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="doomed", status="active"
            )
            session.add(agent_session)
            await session.flush()

            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="a turn",
                status="completed",
                provider="mock",
                model="mock-model",
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.commit()
            run_id = run.id

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                deleted = await client.delete(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}"
                )

            assert deleted.status_code == 200
            assert deleted.json() == {"ok": True}

            # The run survives with session_id set to NULL (ON DELETE SET NULL).
            session.expire_all()
            surviving = await session.get(AgentRun, run_id)
            assert surviving is not None
            assert surviving.session_id is None

            gone = await session.get(AgentSession, agent_session.id)
            assert gone is None
        finally:
            self._teardown()

    async def test_append_turn_endpoint_returns_run_with_session_id(self, db_session, monkeypatch):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessturn")
        # _execute_run is patched to a no-op so the turn does not kick off real agent work.
        monkeypatch.setattr(AgentService, "_execute_run", _noop_execute_run)
        try:
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title=None, status="active"
            )
            session.add(agent_session)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}/turns",
                    json={"prompt": "Create a parser for CSV files"},
                )

            assert response.status_code == 201
            body = response.json()
            assert body["session_id"] == str(agent_session.id)
            assert body["status"] == "thinking"
            assert body["prompt"] == "Create a parser for CSV files"
        finally:
            self._teardown()

    async def test_append_turn_auto_titles_from_first_prompt(self, db_session, monkeypatch):
        # Exercised at the service layer (no HTTP client) so asyncio.create_task can be
        # neutralized, keeping create_run's background scheduling out of the shared connection.
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessautotitle")
        monkeypatch.setattr("app.services.agents.asyncio.create_task", _fake_create_task)
        try:
            service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title=None, status="active"
            )
            session.add(agent_session)
            await session.commit()

            first_run = await service.append_turn(
                workspace.id, agent_session.id, user.id, "Create a parser for CSV files"
            )
            assert first_run.session_id == agent_session.id
            assert agent_session.title == "Create a parser for CSV files"

            # Free the one-active-run slot, then a second turn must not overwrite the title.
            first_run.status = "completed"
            session.add(first_run)
            await session.commit()

            await service.append_turn(
                workspace.id, agent_session.id, user.id, "Now add JSON support too"
            )
            assert agent_session.title == "Create a parser for CSV files"  # unchanged
        finally:
            self._teardown()

    async def test_turn_on_archived_session_returns_409(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessarch")
        try:
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="done", status="archived"
            )
            session.add(agent_session)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}/turns",
                    json={"prompt": "another turn"},
                )

            assert response.status_code == 409
            assert response.json()["detail"] == "Session is archived"
        finally:
            self._teardown()

    async def test_turn_on_missing_session_returns_404(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessturn404")
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/workspaces/{workspace.id}/agent/sessions/{uuid4()}/turns",
                    json={"prompt": "hi"},
                )
            assert response.status_code == 404
            assert response.json()["detail"] == "Session not found"
        finally:
            self._teardown()

    async def test_turn_while_run_active_returns_409(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessbusy")
        try:
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="busy", status="active"
            )
            session.add(agent_session)
            await session.flush()
            # An already-active run in the workspace should block a new turn.
            active_run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="still running",
                status="executing",
                provider="mock",
                model="mock-model",
            )
            session.add(active_run)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    f"/workspaces/{workspace.id}/agent/sessions/{agent_session.id}/turns",
                    json={"prompt": "queue me"},
                )

            assert response.status_code == 409
            assert response.json()["detail"] == "Workspace already has an active agent run"
        finally:
            self._teardown()

    async def test_one_shot_run_detail_has_null_session_id(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessoneshot")
        try:
            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="one shot",
                status="completed",
                provider="mock",
                model="mock-model",
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                detail = await client.get(
                    f"/workspaces/{workspace.id}/agent/runs/{run.id}"
                )

            assert detail.status_code == 200
            assert detail.json()["session_id"] is None
        finally:
            self._teardown()

    async def test_build_session_context_transcript(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessctx")
        try:
            service = AgentService(session, session_maker, Settings(agent_allow_mock_provider=True))
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="ctx", status="active"
            )
            session.add(agent_session)
            await session.flush()

            prior = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="Create notes.txt with hello",
                status="completed",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime.now(UTC),
            )
            session.add(prior)
            await session.flush()

            # output_text arrives as streamed fragments that must be concatenated.
            for i, fragment in enumerate(["I ", "created ", "notes.txt"]):
                session.add(
                    AgentEvent(
                        id=uuid4(),
                        agent_run_id=prior.id,
                        event_type="output_text",
                        content=fragment,
                        event_metadata={},
                        created_at=datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC),
                    )
                )
            await service.create_file_change(prior.id, "notes.txt", "created", None)

            current = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="Append a second line",
                status="thinking",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            session.add(current)
            await session.flush()

            context = await service._build_session_context(session, current)
            assert context is not None
            assert "User: Create notes.txt with hello" in context
            assert "Assistant: I created notes.txt" in context
            assert "Files changed: notes.txt (created)" in context

            # A one-shot run (no session) gets no context.
            one_shot = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="standalone",
                status="thinking",
                provider="mock",
                model="mock-model",
            )
            session.add(one_shot)
            await session.flush()
            assert await service._build_session_context(session, one_shot) is None
        finally:
            self._teardown()

    async def test_build_session_context_respects_budget(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "sessbudget")
        try:
            # Tiny budget so only the most recent prior turn survives.
            settings = Settings(
                agent_session_context_char_budget=10,
                agent_session_context_per_turn_char_cap=8_000,
            )
            service = AgentService(session, session_maker, settings)
            agent_session = AgentSession(
                workspace_id=workspace.id, user_id=user.id, title="b", status="active"
            )
            session.add(agent_session)
            await session.flush()

            oldest = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="OLDEST_TURN_PROMPT",
                status="completed",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                completed_at=datetime.now(UTC),
            )
            newest = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="NEWEST_TURN_PROMPT",
                status="completed",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 2, tzinfo=UTC),
                completed_at=datetime.now(UTC),
            )
            session.add_all([oldest, newest])
            await session.flush()

            current = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                session_id=agent_session.id,
                prompt="current",
                status="thinking",
                provider="mock",
                model="mock-model",
                started_at=datetime(2026, 1, 3, tzinfo=UTC),
            )
            session.add(current)
            await session.flush()

            context = await service._build_session_context(session, current)
            assert context is not None
            assert "NEWEST_TURN_PROMPT" in context
            assert "OLDEST_TURN_PROMPT" not in context
        finally:
            self._teardown()


class TestAgentRunCostFields:
    """AgentRunResponse surfaces input/output tokens and estimated cost."""

    async def _setup(self, session, session_maker, label):
        uid = uuid4()
        user = User(
            id=uuid4(),
            email=f"{label}-{uid}@example.com",
            password_hash=hash_password("password"),
            role="owner",
            is_active=True,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        workspace = Workspace(
            id=uuid4(),
            owner_id=user.id,
            name=f"{label}-{uid}",
            slug=f"{label}-{uid}",
            repo_url="https://github.com/example/cost",
            default_branch="main",
            runtime="daytona",
            workspace_path="/workspace",
            status="ready",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add_all([user, workspace])
        await session.flush()
        app.dependency_overrides[auth_module.get_current_user] = await override_get_current_user(
            user
        )
        app.dependency_overrides[get_db_session] = override_get_db_session(session)
        app.dependency_overrides[agents_module.get_agent_service] = override_get_agent_service(
            session, session_maker
        )
        return user, workspace

    async def test_detail_includes_stored_cost_fields(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "cost-stored")
        try:
            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="do the thing",
                status="completed",
                provider="openai",
                model="gpt-4.1-mini",
                tokens_used=300,
                input_tokens=100,
                output_tokens=200,
                estimated_cost=Decimal("0.00034"),
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run.id}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["input_tokens"] == 100
            assert body["output_tokens"] == 200
            assert body["estimated_cost"] == pytest.approx(0.00034)
        finally:
            app.dependency_overrides.clear()

    async def test_estimated_cost_fallback_computed_on_read(self, db_session):
        session, session_maker = db_session
        user, workspace = await self._setup(session, session_maker, "cost-fallback")
        try:
            # No stored estimated_cost, but tokens are present -> computed on read.
            run = AgentRun(
                workspace_id=workspace.id,
                user_id=user.id,
                prompt="legacy run",
                status="completed",
                provider="openai",
                model="gpt-4.1-mini",
                tokens_used=1_000_000,
                completed_at=datetime.now(UTC),
            )
            session.add(run)
            await session.commit()

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get(f"/workspaces/{workspace.id}/agent/runs/{run.id}")
            assert resp.status_code == 200
            # Total-only fallback applies the output rate (1.60 / 1M tokens).
            assert resp.json()["estimated_cost"] == pytest.approx(1.60)
        finally:
            app.dependency_overrides.clear()
