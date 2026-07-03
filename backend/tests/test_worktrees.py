import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth as auth_module
from app.api import workspaces as workspaces_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.main import app
from app.models import RepositoryCredential, User, Workspace
from app.services.github_client import (
    GitHubApiError,
    GitHubClient,
    PullRequestService,
    parse_owner_repo,
)
from app.services.users import DuplicateUserEmailError
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher
from app.services.worktrees import (
    CannotRemoveBaseWorktreeError,
    CannotSyncBaseWorktreeError,
    InvalidBranchNameError,
    MergeConflictError,
    WorktreeAlreadyExistsError,
    WorktreeDirtyError,
    WorktreeInfo,
    WorktreeLimitError,
    WorktreeService,
    slugify_branch,
    validate_branch,
)

# --------------------------------------------------------------------------------------
# Fakes / fixtures
# --------------------------------------------------------------------------------------


class FakeRuntime:
    """Records exec calls and returns canned results keyed by command prefix."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.responses: dict[str, tuple[int, str]] = {}
        self.status_output = ""  # `git status --porcelain`
        self.worktree_list = ""  # `git worktree list --porcelain`

    def set(self, prefix: str, exit_code: int, stdout: str = "") -> None:
        self.responses[prefix] = (exit_code, stdout)

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.calls.append((command, cwd))
        exit_code, stdout = 0, ""
        if command == "test -e .git":
            exit_code = 0
        elif command.startswith("git status --porcelain"):
            stdout = self.status_output
        elif command.startswith("git worktree list --porcelain"):
            stdout = self.worktree_list
        elif command.startswith("git rev-list"):
            stdout = "0\t0"
        for prefix, (code, out) in self.responses.items():
            if command.startswith(prefix):
                exit_code, stdout = code, out
        return type("R", (), {"exit_code": exit_code, "stdout": stdout, "stderr": ""})()


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


async def make_workspace(session, *, default_branch="main") -> tuple[User, Workspace]:
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
        default_branch=default_branch,
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add_all([user, workspace])
    await session.flush()
    return user, workspace


# --------------------------------------------------------------------------------------
# Branch validation / slug helpers
# --------------------------------------------------------------------------------------


def test_validate_branch_accepts_normal_refs():
    assert validate_branch("feat/001-add-auth") == "feat/001-add-auth"
    assert validate_branch("fix-login") == "fix-login"


@pytest.mark.parametrize(
    "value",
    ["--bad", "feat/..", "rm -rf /", "feat;drop", "with space", "trailing/", ""],
)
def test_validate_branch_rejects_metacharacters(value):
    with pytest.raises(InvalidBranchNameError):
        validate_branch(value)


def test_slugify_branch_confines_to_safe_chars():
    assert slugify_branch("feat/001-Add Auth!!") == "feat-001-add-auth"
    assert slugify_branch("///") == "feature"


# --------------------------------------------------------------------------------------
# WorktreeService
# --------------------------------------------------------------------------------------


async def test_create_worktree_runs_git_and_persists_row(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime()
    settings = Settings()
    service = WorktreeService(db_session, runtime, settings)

    row = await service.create_worktree(workspace.id, user.id, "feat/login")

    assert row.branch == "feat/login"
    assert row.path == "worktrees/feat-login"
    assert row.base_ref == "main"
    assert not row.is_base
    # The raw `git worktree add` ran against the managed sibling dir.
    assert any(
        cmd.startswith("git worktree add ../worktrees/feat-login -b feat/login main")
        for cmd, _ in runtime.calls
    )


async def test_create_worktree_rejects_duplicate_branch(db_session):
    user, workspace = await make_workspace(db_session)
    service = WorktreeService(db_session, FakeRuntime(), Settings())
    await service.create_worktree(workspace.id, user.id, "feat/login")
    with pytest.raises(WorktreeAlreadyExistsError):
        await service.create_worktree(workspace.id, user.id, "feat/login")


async def test_create_worktree_enforces_limit(db_session):
    user, workspace = await make_workspace(db_session)
    service = WorktreeService(db_session, FakeRuntime(), Settings(max_worktrees_per_workspace=1))
    await service.create_worktree(workspace.id, user.id, "feat/one")
    with pytest.raises(WorktreeLimitError):
        await service.create_worktree(workspace.id, user.id, "feat/two")


async def test_list_worktrees_includes_base_and_feature(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime()
    service = WorktreeService(db_session, runtime, Settings())
    await service.create_worktree(workspace.id, user.id, "feat/login")

    runtime.worktree_list = (
        "worktree /home/daytona/workspace\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /home/daytona/worktrees/feat-login\nHEAD def\nbranch refs/heads/feat/login\n"
    )
    runtime.status_output = ""  # clean

    infos = await service.list_worktrees(workspace.id, user.id)
    branches = {info.branch: info for info in infos}
    assert "main" in branches and branches["main"].is_base
    assert "feat/login" in branches and not branches["feat/login"].is_base
    assert branches["feat/login"].is_clean


async def test_remove_worktree_blocks_base_and_dirty(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime()
    service = WorktreeService(db_session, runtime, Settings())
    row = await service.create_worktree(workspace.id, user.id, "feat/login")

    # `git worktree list` reports both trees so reconciliation keeps the feature row.
    runtime.worktree_list = (
        "worktree /home/daytona/workspace\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /home/daytona/worktrees/feat-login\nHEAD def\nbranch refs/heads/feat/login\n"
    )
    # The base row is created lazily by list_worktrees.
    await service.list_worktrees(workspace.id, user.id)
    base = next(
        r for r in await service._list_rows(workspace.id) if r.is_base  # noqa: SLF001
    )
    with pytest.raises(CannotRemoveBaseWorktreeError):
        await service.remove_worktree(workspace.id, user.id, base.id)

    runtime.status_output = " M file.py"  # dirty
    with pytest.raises(WorktreeDirtyError):
        await service.remove_worktree(workspace.id, user.id, row.id)

    # Force removes even when dirty.
    await service.remove_worktree(workspace.id, user.id, row.id, force=True)
    assert any(cmd.startswith("git worktree remove --force") for cmd, _ in runtime.calls)


async def test_resolve_path_defaults_to_base(db_session):
    user, workspace = await make_workspace(db_session)
    service = WorktreeService(db_session, FakeRuntime(), Settings())
    row = await service.create_worktree(workspace.id, user.id, "feat/login")

    assert await service.resolve_path(workspace, None) == "workspace"
    assert await service.resolve_path(workspace, row.id) == "worktrees/feat-login"


async def test_sync_from_base_merges_origin_base_into_worktree(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime()
    service = WorktreeService(db_session, runtime, Settings())
    row = await service.create_worktree(workspace.id, user.id, "feat/login")

    await service.sync_from_base(workspace.id, user.id, row.id)

    # Merges the already-fetched remote base inside the worktree directory (base clone untouched).
    assert ("git merge --no-edit origin/main", "worktrees/feat-login") in runtime.calls


async def test_sync_from_base_rebase_strategy(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime()
    service = WorktreeService(db_session, runtime, Settings())
    row = await service.create_worktree(workspace.id, user.id, "feat/login")

    await service.sync_from_base(workspace.id, user.id, row.id, strategy="rebase")

    assert ("git rebase origin/main", "worktrees/feat-login") in runtime.calls


async def test_sync_from_base_conflict_raises(db_session):
    user, workspace = await make_workspace(db_session)
    runtime = FakeRuntime()
    runtime.set("git merge", 1, "CONFLICT (content): Merge conflict in README.md")
    service = WorktreeService(db_session, runtime, Settings())
    row = await service.create_worktree(workspace.id, user.id, "feat/login")

    with pytest.raises(MergeConflictError):
        await service.sync_from_base(workspace.id, user.id, row.id)


async def test_sync_from_base_rejects_base_worktree(db_session):
    user, workspace = await make_workspace(db_session)
    service = WorktreeService(db_session, FakeRuntime(), Settings())
    await service.list_worktrees(workspace.id, user.id)  # lazily creates the base row
    base = next(r for r in await service._list_rows(workspace.id) if r.is_base)  # noqa: SLF001

    with pytest.raises(CannotSyncBaseWorktreeError):
        await service.sync_from_base(workspace.id, user.id, base.id)


# --------------------------------------------------------------------------------------
# GitHubClient / PullRequestService
# --------------------------------------------------------------------------------------


def test_parse_owner_repo():
    assert parse_owner_repo("https://github.com/octocat/Hello-World.git") == (
        "octocat",
        "Hello-World",
    )


async def test_github_client_create_pull_request_sends_payload_and_auth():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "number": 42,
                "html_url": "https://github.com/o/r/pull/42",
                "state": "open",
                "title": "Add login",
                "head": {"ref": "feat/login"},
                "base": {"ref": "main"},
                "draft": False,
            },
        )

    client = GitHubClient(Settings(), transport=httpx.MockTransport(handler))
    payload = await client.create_pull_request(
        "tok", "o", "r", head="feat/login", base="main", title="Add login", body="b", draft=False
    )
    assert payload["number"] == 42
    assert captured["auth"] == "Bearer tok"
    assert captured["body"]["head"] == "feat/login"


async def test_github_client_redacts_token_in_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Validation failed"})

    client = GitHubClient(Settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubApiError) as excinfo:
        await client.get_pull_request("super-secret-token", "o", "r", 1)
    assert excinfo.value.status_code == 422
    assert "super-secret-token" not in str(excinfo.value)


async def test_pull_request_service_create_caches_row(db_session):
    user, workspace = await make_workspace(db_session)
    cipher = WorkspaceSecretCipher(Settings().workspace_env_encryption_key)
    db_session.add(
        RepositoryCredential(
            workspace_id=workspace.id,
            provider="github",
            auth_type="personal_access_token",
            encrypted_token=cipher.encrypt("ghp_token"),
        )
    )
    await db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "number": 7,
                "html_url": "https://github.com/example/demo/pull/7",
                "state": "open",
                "title": "Add login",
                "head": {"ref": "feat/login"},
                "base": {"ref": "main"},
                "draft": True,
            },
        )

    client = GitHubClient(Settings(), transport=httpx.MockTransport(handler))
    service = PullRequestService(db_session, cipher, Settings(), client=client)
    row = await service.create(
        workspace.id,
        user.id,
        head="feat/login",
        base=None,
        title="Add login",
        body=None,
        draft=True,
    )
    assert row.number == 7
    assert row.url == "https://github.com/example/demo/pull/7"
    assert row.head == "feat/login"

    cached = await service.list_open(workspace.id, user.id)
    assert [pr.number for pr in cached] == [7]


# --------------------------------------------------------------------------------------
# Endpoint tests (HTTP layer: auth, routing, error mapping) with dependency overrides
# --------------------------------------------------------------------------------------


class FakeUserStore:
    def __init__(self, user: User | None = None) -> None:
        self.user = user

    async def get_by_email(self, email: str) -> User | None:
        return self.user if self.user and self.user.email == email else None

    async def get_by_id(self, user_id):
        return self.user if self.user and self.user.id == user_id else None

    async def register_user(self, email, name, password_hash) -> User:
        raise DuplicateUserEmailError


class FakeRateLimiter:
    async def check(self, email, request) -> None:
        return None

    async def reset(self, email, request) -> None:
        return None


class FakeNoopAudit:
    async def record(self, **values) -> None:
        return None


class FakeNoopNotifications:
    async def create(self, **values) -> None:
        return None


class FakeWorktreeServiceForApi:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.error: Exception | None = None
        self.infos: list[WorktreeInfo] = []

    async def list_worktrees(self, workspace_id, owner_id):
        self._check(workspace_id, owner_id)
        if self.error:
            raise self.error
        return self.infos

    async def create_worktree(self, workspace_id, owner_id, branch, base=None):
        self._check(workspace_id, owner_id)
        if self.error:
            raise self.error
        return SimpleNamespace(
            id=uuid4(),
            branch=branch,
            path=f"worktrees/{branch}",
            base_ref=base or "main",
            is_base=False,
            status="active",
        )

    async def get_worktree(self, workspace_id, owner_id, worktree_id):
        self._check(workspace_id, owner_id)
        if self.error:
            raise self.error
        return SimpleNamespace(id=worktree_id, branch="feat/login", path="worktrees/feat-login")

    def _check(self, workspace_id, owner_id):
        if self.workspace.id != workspace_id or self.workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError


class FakePullRequestServiceForApi:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.error: Exception | None = None

    async def create(
        self, workspace_id, owner_id, *, head, base, title, body, draft, worktree_id=None
    ):
        if self.error:
            raise self.error
        return SimpleNamespace(
            id=uuid4(),
            worktree_id=worktree_id,
            number=11,
            url="https://github.com/example/demo/pull/11",
            state="open",
            head=head,
            base=base or "main",
            title=title,
        )

    async def list_open(self, workspace_id, owner_id):
        return []


@pytest.fixture
def api_context(tmp_path):
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
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    worktree_service = FakeWorktreeServiceForApi(workspace)
    pr_service = FakePullRequestServiceForApi(workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[workspaces_module.get_worktree_service] = lambda: worktree_service
    app.dependency_overrides[workspaces_module.get_pull_request_service] = lambda: pr_service
    app.dependency_overrides[workspaces_module.get_audit_log_service] = lambda: FakeNoopAudit()
    app.dependency_overrides[workspaces_module.get_notification_service] = (
        lambda: FakeNoopNotifications()
    )
    yield user, workspace, worktree_service, pr_service
    app.dependency_overrides.clear()


async def _login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_worktree_endpoints_require_auth(api_context):
    _user, workspace, _wt, _pr = api_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace.id}/repo/worktrees")
    assert response.status_code == 401


async def test_list_and_create_worktree(api_context):
    _user, workspace, worktree_service, _pr = api_context
    worktree_service.infos = [
        WorktreeInfo(
            id=uuid4(),
            branch="main",
            path="workspace",
            base_ref="main",
            is_base=True,
            status="active",
            is_clean=True,
            ahead=0,
            behind=0,
        )
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        listed = await client.get(f"/workspaces/{workspace.id}/repo/worktrees")
        created = await client.post(
            f"/workspaces/{workspace.id}/repo/worktrees",
            json={"branch": "feat/login"},
        )
    assert listed.status_code == 200
    assert listed.json()[0]["is_base"] is True
    assert created.status_code == 201
    assert created.json()["branch"] == "feat/login"


async def test_create_worktree_limit_maps_to_409(api_context):
    _user, workspace, worktree_service, _pr = api_context
    worktree_service.error = WorktreeLimitError("limit")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/repo/worktrees",
            json={"branch": "feat/login"},
        )
    assert response.status_code == 409


async def test_create_pull_request(api_context):
    _user, workspace, _wt, _pr = api_context
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/repo/pull-requests",
            json={"worktree_id": str(uuid4()), "title": "Add login"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["number"] == 11
    assert body["head"] == "feat/login"


async def test_create_pull_request_github_error_maps_status(api_context):
    _user, workspace, _wt, pr_service = api_context
    pr_service.error = GitHubApiError("Validation failed", status_code=422)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/repo/pull-requests",
            json={"head": "feat/login", "title": "Add login"},
        )
    assert response.status_code == 422
