from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import workspaces as workspaces_module
from app.core.config import Settings
from app.core.security import hash_password
from app.main import app
from app.models import User, Workspace
from app.services.repositories import (
    GitCommandError,
    GitFileStatus,
    GitStatus,
    InvalidRepositoryUrlError,
    NoChangesToCommitError,
    RepositoryAlreadyClonedError,
    RepositoryCredentialNotFoundError,
    RepositoryNotClonedError,
    RepositoryService,
    parse_porcelain_status,
    sanitize_repository_url,
)
from app.services.users import DuplicateUserEmailError
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher


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


class FakeWorkspaceService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def list_for_owner(self, owner_id: UUID) -> list[Workspace]:
        return [self.workspace] if self.workspace.owner_id == owner_id else []

    async def get_for_owner(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        if self.workspace.id != workspace_id or self.workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return self.workspace

    async def list_env_vars(self, workspace_id: UUID, owner_id: UUID) -> list[object]:
        await self.get_for_owner(workspace_id, owner_id)
        return []


class FakeRepositoryService:
    def __init__(self, workspace: Workspace, cipher: WorkspaceSecretCipher) -> None:
        self.workspace = workspace
        self.cipher = cipher
        self.encrypted_token: str | None = None
        self.status_response = GitStatus(
            cloned=True,
            branch="main",
            is_clean=False,
            files=[GitFileStatus(path="README.md", status="M")],
        )
        self.diff_response = "diff --git a/README.md b/README.md\n+hello\n"
        self.error: Exception | None = None
        self.calls: list[str] = []

    async def connect(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        repo_url: str,
        token: str,
    ) -> Workspace:
        self._ensure_workspace(workspace_id, owner_id)
        self.workspace.repo_url = sanitize_repository_url(repo_url)
        self.encrypted_token = self.cipher.encrypt(token)
        self.calls.append("connect")
        return self.workspace

    async def clone(self, workspace_id: UUID, owner_id: UUID) -> None:
        self._ensure_operation("clone", workspace_id, owner_id)

    async def status(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
    ) -> GitStatus:
        self._ensure_workspace(workspace_id, owner_id)
        self.calls.append(f"status:{worktree_path}")
        return self.status_response

    async def diff(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str | None = None,
        *,
        worktree_path: str | None = None,
    ) -> str:
        self._ensure_workspace(workspace_id, owner_id)
        self.calls.append(f"diff:{path}:{worktree_path}")
        return self.diff_response

    async def pull(self, workspace_id: UUID, owner_id: UUID) -> None:
        self._ensure_operation("pull", workspace_id, owner_id)

    async def commit(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        message: str,
        *,
        worktree_path: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        self._ensure_operation(f"commit:{message}", workspace_id, owner_id)

    async def push(self, workspace_id: UUID, owner_id: UUID) -> None:
        self._ensure_operation("push", workspace_id, owner_id)

    def _ensure_operation(self, call: str, workspace_id: UUID, owner_id: UUID) -> None:
        self._ensure_workspace(workspace_id, owner_id)
        self.calls.append(call)
        if self.error is not None:
            raise self.error

    def _ensure_workspace(self, workspace_id: UUID, owner_id: UUID) -> None:
        if self.workspace.id != workspace_id or self.workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError


@pytest.fixture
def repository_context(tmp_path):
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
        daytona_sandbox_id="sandbox-demo",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    settings = Settings(workspace_root=tmp_path)
    repo_service = FakeRepositoryService(
        workspace=workspace,
        cipher=WorkspaceSecretCipher(settings.workspace_env_encryption_key),
    )
    workspace_service = FakeWorkspaceService(workspace)

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[workspaces_module.get_workspace_service] = lambda: workspace_service
    app.dependency_overrides[workspaces_module.get_repository_service] = lambda: repo_service
    yield user, workspace, repo_service
    app.dependency_overrides.clear()


async def login(client: AsyncClient, email: str = "owner@example.com") -> None:
    await client.post("/auth/login", json={"email": email, "password": "password"})


async def test_repository_endpoints_require_auth(repository_context):
    _user, workspace, _repo_service = repository_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace.id}/repo/status")

    assert response.status_code == 401


async def test_connect_sanitizes_url_encrypts_token_and_never_returns_token(repository_context):
    _user, workspace, repo_service = repository_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/repo/connect",
            json={
                "repo_url": "https://ghp_inline-secret@github.com/example/demo",
                "token": "raw-secret-token",
            },
        )

    assert response.status_code == 200
    assert response.json()["repo_url"] == "https://github.com/example/demo.git"
    assert "raw-secret-token" not in str(response.json())
    assert repo_service.encrypted_token != "raw-secret-token"
    assert repo_service.cipher.decrypt(repo_service.encrypted_token or "") == "raw-secret-token"


async def test_connect_rejects_non_github_urls(repository_context):
    _user, workspace, _repo_service = repository_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/repo/connect",
            json={"repo_url": "https://gitlab.com/example/demo", "token": "raw-secret-token"},
        )

    assert response.status_code == 400


async def test_repository_ownership_is_enforced(repository_context):
    user, workspace, _repo_service = repository_context
    workspace.owner_id = uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client, user.email)
        response = await client.get(f"/workspaces/{workspace.id}/repo/status")

    assert response.status_code == 404


async def test_repository_status_diff_and_operations(repository_context):
    _user, workspace, repo_service = repository_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        status_response = await client.get(f"/workspaces/{workspace.id}/repo/status")
        diff_response = await client.get(
            f"/workspaces/{workspace.id}/repo/diff",
            params={"path": "README.md"},
        )
        clone_response = await client.post(f"/workspaces/{workspace.id}/repo/clone")
        pull_response = await client.post(f"/workspaces/{workspace.id}/repo/pull")
        commit_response = await client.post(
            f"/workspaces/{workspace.id}/repo/commit",
            json={"message": "Update README"},
        )
        push_response = await client.post(f"/workspaces/{workspace.id}/repo/push")

    assert status_response.json() == {
        "cloned": True,
        "branch": "main",
        "is_clean": False,
        "files": [{"path": "README.md", "status": "M"}],
    }
    assert diff_response.json()["diff"] == repo_service.diff_response
    assert clone_response.status_code == 200
    assert pull_response.status_code == 200
    assert commit_response.status_code == 200
    assert push_response.status_code == 200
    assert repo_service.calls[-4:] == ["clone", "pull", "commit:Update README", "push"]


async def test_status_and_diff_scope_to_selected_worktree(repository_context):
    _user, workspace, repo_service = repository_context
    worktree_id = uuid4()
    worktree_path = "/workspace/worktrees/feat-login"

    class FakeWorktreeService:
        async def get_worktree(self, ws_id, owner_id, wt_id):
            assert ws_id == workspace.id
            assert wt_id == worktree_id
            return SimpleNamespace(path=worktree_path)

    app.dependency_overrides[workspaces_module.get_worktree_service] = FakeWorktreeService

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        status_response = await client.get(
            f"/workspaces/{workspace.id}/repo/status",
            params={"worktree_id": str(worktree_id)},
        )
        diff_response = await client.get(
            f"/workspaces/{workspace.id}/repo/diff",
            params={"path": "README.md", "worktree_id": str(worktree_id)},
        )

    assert status_response.status_code == 200
    assert diff_response.status_code == 200
    # The resolved worktree path is forwarded to the repository service for both reads.
    assert f"status:{worktree_path}" in repo_service.calls
    assert f"diff:README.md:{worktree_path}" in repo_service.calls


@pytest.mark.parametrize(
    ("service_error", "expected_status"),
    [
        (RepositoryCredentialNotFoundError(), 409),
        (RepositoryNotClonedError(), 409),
        (RepositoryAlreadyClonedError(), 409),
        (NoChangesToCommitError(), 409),
        (GitCommandError("remote rejected"), 400),
    ],
)
async def test_repository_operation_errors_map_to_http_statuses(
    repository_context,
    service_error,
    expected_status,
):
    _user, workspace, repo_service = repository_context
    repo_service.error = service_error

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{workspace.id}/repo/push")

    assert response.status_code == expected_status


def test_sanitize_repository_url_accepts_github_https_and_strips_credentials():
    assert (
        sanitize_repository_url("https://github.com/example/demo")
        == "https://github.com/example/demo.git"
    )
    assert (
        sanitize_repository_url("https://token@github.com/example/demo.git")
        == "https://github.com/example/demo.git"
    )


@pytest.mark.parametrize(
    "repo_url",
    [
        "git@github.com:example/demo.git",
        "https://gitlab.com/example/demo.git",
        "https://github.com/example",
        "https://github.com/example/demo/issues",
    ],
)
def test_sanitize_repository_url_rejects_unsupported_urls(repo_url):
    with pytest.raises(InvalidRepositoryUrlError):
        sanitize_repository_url(repo_url)


def test_parse_porcelain_status_handles_modified_untracked_and_renamed_files():
    output = "## main...origin/main\n M README.md\n?? app/new.py\nR  old.py -> new.py\n"

    assert parse_porcelain_status(output) == [
        GitFileStatus(path="README.md", status="M"),
        GitFileStatus(path="app/new.py", status="??"),
        GitFileStatus(path="new.py", status="R"),
    ]


async def test_clone_uses_daytona_git_with_branch_and_credentials(tmp_path):
    user_id = uuid4()
    workspace = Workspace(
        id=uuid4(),
        owner_id=user_id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    calls: list[dict[str, object]] = []

    class FakeFs:
        async def get_file_info(self, path):
            raise FileNotFoundError

    class FakeGit:
        async def clone(self, url, path, **kwargs):
            calls.append({"url": url, "path": path, **kwargs})

    class FakeRuntime:
        async def get_sandbox(self, workspace):
            return SimpleNamespace(fs=FakeFs(), git=FakeGit())

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        assert workspace_id == workspace.id
        assert owner_id == user_id
        return workspace

    async def fake_get_token(workspace_id: UUID) -> str:
        assert workspace_id == workspace.id
        return "raw-secret-token"

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_token = fake_get_token  # type: ignore[method-assign]

    await service.clone(workspace.id, user_id)

    assert calls == [
        {
            "url": workspace.repo_url,
            "path": "/workspace",
            "branch": "main",
            "username": "x-access-token",
            "password": "raw-secret-token",
        }
    ]


async def test_push_uses_authenticated_git_cli_in_worktree(tmp_path):
    import base64

    user_id = uuid4()
    workspace = Workspace(
        id=uuid4(),
        owner_id=user_id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    worktree_path = "/workspace/worktrees/feat-login"
    commands: list[tuple[str, str | None]] = []

    class FakeFs:
        async def get_file_info(self, path):
            return SimpleNamespace(name=path)  # exists => treated as cloned

    class FakeRuntime:
        async def get_sandbox(self, workspace):
            # No ``git`` attribute: falling back to the SDK's go-git push would raise here,
            # so a passing test proves the push runs through the git CLI instead.
            return SimpleNamespace(fs=FakeFs())

        async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
            commands.append((command, cwd))
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        return workspace

    async def fake_get_token(workspace_id: UUID) -> str:
        return "raw-secret-token"

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_token = fake_get_token  # type: ignore[method-assign]

    await service.push(workspace.id, user_id, worktree_path=worktree_path)

    command, cwd = commands[-1]
    # Pushes the current branch, creating + tracking the remote branch — works in a worktree.
    assert "push --set-upstream origin HEAD" in command
    assert cwd == worktree_path
    # Auth rides on an HTTP Basic header carrying the token; the raw token never appears verbatim.
    expected = base64.b64encode(b"x-access-token:raw-secret-token").decode()
    assert f"http.extraHeader=AUTHORIZATION: basic {expected}" in command
    assert "raw-secret-token" not in command


async def test_push_surfaces_sanitized_git_error(tmp_path):
    workspace = Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class FakeFs:
        async def get_file_info(self, path):
            return SimpleNamespace(name=path)

    class FakeRuntime:
        async def get_sandbox(self, workspace):
            return SimpleNamespace(fs=FakeFs())

        async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
            return SimpleNamespace(
                exit_code=1,
                stdout="remote: Permission to example/demo.git denied to raw-secret-token.",
                stderr="",
            )

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        return workspace

    async def fake_get_token(workspace_id: UUID) -> str:
        return "raw-secret-token"

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_token = fake_get_token  # type: ignore[method-assign]

    with pytest.raises(GitCommandError) as excinfo:
        await service.push(workspace.id, workspace.owner_id)

    # A failed push surfaces git's message (e.g. a real permission error) with the token redacted.
    assert "Permission to example/demo.git denied" in excinfo.value.message
    assert "raw-secret-token" not in excinfo.value.message


async def test_status_uses_git_cli_for_worktrees(tmp_path):
    user_id = uuid4()
    workspace = Workspace(
        id=uuid4(),
        owner_id=user_id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo.git",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    worktree_path = "/workspace/worktrees/feat-login"
    commands: list[tuple[str, str | None]] = []

    class FakeFs:
        async def get_file_info(self, path):
            return SimpleNamespace(name=path)  # exists => treated as cloned

    class FakeRuntime:
        async def get_sandbox(self, workspace):
            # No ``git`` attribute: if status() fell back to the SDK path it would raise.
            return SimpleNamespace(fs=FakeFs())

        async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
            commands.append((command, cwd))
            if command == "git status --porcelain":
                return SimpleNamespace(
                    exit_code=0, stdout=" M README.md\n?? new.py\n", stderr=""
                )
            if command == "git branch --show-current":
                return SimpleNamespace(exit_code=0, stdout="feat-login\n", stderr="")
            return SimpleNamespace(exit_code=1, stdout="", stderr="unexpected command")

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        return workspace

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]

    result = await service.status(workspace.id, user_id, worktree_path=worktree_path)

    assert result.cloned is True
    assert result.branch == "feat-login"
    assert result.is_clean is False
    assert result.files == [
        GitFileStatus(path="README.md", status="M"),
        GitFileStatus(path="new.py", status="??"),
    ]
    # Both reads run via the git CLI against the worktree directory, not the SDK.
    assert ("git status --porcelain", worktree_path) in commands
    assert ("git branch --show-current", worktree_path) in commands
