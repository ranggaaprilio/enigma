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
from app.services.agents import AgentService
from app.services.repositories import (
    GIT_STATUS_COMMAND,
    GitAuth,
    GitCommandError,
    GitCommit,
    GitCommitDetail,
    GitFileStatus,
    GitStatus,
    InvalidRepositoryUrlError,
    NoChangesToCommitError,
    RepositoryAlreadyClonedError,
    RepositoryCredentialNotFoundError,
    RepositoryNotClonedError,
    RepositoryPathError,
    RepositoryService,
    branch_from_porcelain_header,
    parse_porcelain_status,
    porcelain_status_word,
    sanitize_git_output,
    sanitize_repository_url,
    validate_commit_sha,
)
from app.services.ssh_keys import generate_ed25519_keypair
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
        self.credential: SimpleNamespace | None = None
        self.auth_type: str | None = None
        self.username: str | None = None
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
        *,
        provider: str = "auto",
        auth_type: str = "personal_access_token",
        token: str | None = None,
        username: str | None = None,
    ) -> Workspace:
        self._ensure_workspace(workspace_id, owner_id)
        self.workspace.repo_url = sanitize_repository_url(
            repo_url, provider=provider, auth_type=auth_type
        )
        self.auth_type = auth_type
        self.username = username
        if auth_type == "ssh_key":
            self.encrypted_token = None
            if self.credential is None or self.credential.auth_type != "ssh_key":
                self._new_ssh_credential()
        else:
            self.encrypted_token = self.cipher.encrypt(token or "")
            self.credential = SimpleNamespace(
                provider=provider if provider != "auto" else "github",
                auth_type="personal_access_token",
                username=username,
                public_key=None,
                encrypted_private_key=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        self.calls.append("connect")
        return self.workspace

    async def get_credential(self, workspace_id: UUID, owner_id: UUID):
        self._ensure_workspace(workspace_id, owner_id)
        if self.credential is None:
            raise RepositoryCredentialNotFoundError
        return self.credential

    async def ensure_ssh_key(self, workspace_id: UUID, owner_id: UUID, *, rotate: bool = False):
        self._ensure_workspace(workspace_id, owner_id)
        if rotate or self.credential is None or self.credential.auth_type != "ssh_key":
            self._new_ssh_credential()
        self.calls.append(f"ssh_key:{rotate}")
        return self.credential

    def _new_ssh_credential(self) -> None:
        pair = generate_ed25519_keypair("enigma-test")
        self.credential = SimpleNamespace(
            provider="github",
            auth_type="ssh_key",
            username=None,
            public_key=pair.public_key,
            encrypted_private_key=self.cipher.encrypt(pair.private_key),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

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


async def test_connect_accepts_gitlab_and_rejects_http_and_mismatched_auth(repository_context):
    _user, workspace, repo_service = repository_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        gitlab = await client.post(
            f"/workspaces/{workspace.id}/repo/connect",
            json={"repo_url": "https://gitlab.com/example/demo", "token": "glpat-secret"},
        )
        http = await client.post(
            f"/workspaces/{workspace.id}/repo/connect",
            json={"repo_url": "http://gitlab.com/example/demo", "token": "glpat-secret"},
        )
        ssh_with_https = await client.post(
            f"/workspaces/{workspace.id}/repo/connect",
            json={"repo_url": "https://gitlab.com/example/demo", "auth_type": "ssh_key"},
        )

    assert gitlab.status_code == 200
    assert gitlab.json()["repo_url"] == "https://gitlab.com/example/demo.git"
    assert http.status_code == 400
    assert ssh_with_https.status_code == 400
    assert "SSH URL" in ssh_with_https.json()["detail"]
    assert repo_service.cipher.decrypt(repo_service.encrypted_token or "") == "glpat-secret"


@pytest.mark.parametrize(
    "payload",
    [
        {"repo_url": "https://github.com/example/demo"},  # token missing for PAT
        {"repo_url": "git@github.com:example/demo.git", "auth_type": "ssh_key", "token": "x"},
        {"repo_url": "https://github.com/example/demo", "token": "x", "provider": "svn"},
        {"repo_url": "https://github.com/example/demo", "token": "x", "auth_type": "password"},
    ],
)
async def test_connect_request_validation(repository_context, payload):
    _user, workspace, _repo_service = repository_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(f"/workspaces/{workspace.id}/repo/connect", json=payload)

    assert response.status_code == 422


async def test_ssh_key_endpoint_returns_public_key_and_never_private_material(
    repository_context,
):
    _user, workspace, repo_service = repository_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        missing = await client.get(f"/workspaces/{workspace.id}/repo/credential")
        generated = await client.post(f"/workspaces/{workspace.id}/repo/ssh-key")
        fetched = await client.get(f"/workspaces/{workspace.id}/repo/credential")
        connected = await client.post(
            f"/workspaces/{workspace.id}/repo/connect",
            json={"repo_url": "git@gitlab.com:example/demo.git", "auth_type": "ssh_key"},
        )

    assert missing.status_code == 404
    assert generated.status_code == 200
    body = generated.json()
    assert body["auth_type"] == "ssh_key"
    assert body["public_key"].startswith("ssh-ed25519 ")
    assert body["supports_pull_requests"] is False
    assert "PRIVATE KEY" not in generated.text
    assert "encrypted" not in body
    assert fetched.status_code == 200
    assert fetched.json()["public_key"] == body["public_key"]
    assert connected.status_code == 200
    assert connected.json()["repo_url"] == "ssh://git@gitlab.com/example/demo.git"
    # The key pair generated up front is reused by connect (deploy key stays valid).
    assert repo_service.credential.public_key == body["public_key"]


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


def test_sanitize_repository_url_accepts_https_and_ssh_and_strips_credentials():
    assert (
        sanitize_repository_url("https://github.com/example/demo")
        == "https://github.com/example/demo.git"
    )
    assert (
        sanitize_repository_url("https://token@github.com/example/demo.git")
        == "https://github.com/example/demo.git"
    )
    assert (
        sanitize_repository_url("https://gitlab.com/example/demo.git")
        == "https://gitlab.com/example/demo.git"
    )
    assert (
        sanitize_repository_url("git@github.com:example/demo.git")
        == "ssh://git@github.com/example/demo.git"
    )


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/example/demo.git",
        "https://github.com/example",
        "https://github.com/example/demo/issues",
        "https://localhost/example/demo",
    ],
)
def test_sanitize_repository_url_rejects_unsupported_urls(repo_url):
    with pytest.raises(InvalidRepositoryUrlError):
        sanitize_repository_url(repo_url)


def test_sanitize_git_output_redacts_any_host():
    output = "fatal: unable to access 'https://oauth2:glpat-x@gitlab.example.com/t/d.git/'"
    redacted = sanitize_git_output(output, "glpat-x")
    assert "glpat-x" not in redacted
    assert "https://[redacted]@gitlab.example.com" in redacted


def test_parse_porcelain_status_handles_modified_untracked_and_renamed_files():
    output = "## main...origin/main\n M README.md\n?? app/new.py\nR  old.py -> new.py\n"

    assert parse_porcelain_status(output) == [
        GitFileStatus(path="README.md", status="M"),
        GitFileStatus(path="app/new.py", status="??"),
        GitFileStatus(path="new.py", status="R"),
    ]


async def test_clone_uses_git_cli_with_pat_header_and_branch(tmp_path):
    import base64

    user_id = uuid4()
    workspace = _demo_workspace(user_id)
    commands: list[tuple[str, str | None]] = []
    created: list[str] = []

    class FakeFs:
        async def get_file_info(self, path):
            raise FileNotFoundError

        async def create_folder(self, path, mode):
            created.append(path)

    class FakeRuntime:
        async def fs(self, workspace):
            return FakeFs()

        async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
            commands.append((command, cwd))
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        assert workspace_id == workspace.id
        assert owner_id == user_id
        return workspace

    async def fake_get_auth(workspace_id: UUID) -> GitAuth:
        assert workspace_id == workspace.id
        return GitAuth(kind="token", username="x-access-token", token="raw-secret-token")

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_auth = fake_get_auth  # type: ignore[method-assign]

    await service.clone(workspace.id, user_id)

    assert created == ["/workspace"]
    command, cwd = commands[-1]
    assert cwd == "/workspace"
    assert "clone --branch main -- https://github.com/example/demo.git ." in command
    expected = base64.b64encode(b"x-access-token:raw-secret-token").decode()
    assert f"http.extraHeader=AUTHORIZATION: basic {expected}" in command
    assert "raw-secret-token" not in command


async def test_clone_requires_a_connected_remote():
    workspace = _demo_workspace()
    workspace.repo_url = None

    async def exec_impl(command, cwd):
        raise AssertionError("no git should run")

    service = _repo_service_with_exec(workspace, exec_impl)
    with pytest.raises(RepositoryCredentialNotFoundError):
        await service.clone(workspace.id, workspace.owner_id)


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
        async def fs(self, workspace):
            return FakeFs()

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

    async def fake_get_auth(workspace_id: UUID) -> GitAuth:
        return GitAuth(kind="token", username="x-access-token", token="raw-secret-token")

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_auth = fake_get_auth  # type: ignore[method-assign]

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
        async def fs(self, workspace):
            return FakeFs()

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

    async def fake_get_auth(workspace_id: UUID) -> GitAuth:
        return GitAuth(kind="token", username="x-access-token", token="raw-secret-token")

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_auth = fake_get_auth  # type: ignore[method-assign]

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
        async def fs(self, workspace):
            return FakeFs()

        async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
            commands.append((command, cwd))
            if command == GIT_STATUS_COMMAND:
                return SimpleNamespace(
                    exit_code=0,
                    stdout="## feat-login...origin/feat-login [ahead 1]\n M README.md\n?? new.py\n",
                    stderr="",
                )
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
    # Same vocabulary the SDK status returned, so the UI and the agent file-change badges
    # (`created` for a new file, not `modified`) are unchanged.
    assert result.files == [
        GitFileStatus(path="README.md", status="Modified"),
        GitFileStatus(path="new.py", status="Untracked"),
    ]
    assert [AgentService._map_git_status_to_badge(f.status) for f in result.files] == [
        "modified",
        "created",
    ]
    # One git CLI read against the worktree directory, not the SDK, not two round-trips.
    assert commands == [(GIT_STATUS_COMMAND, worktree_path)]


@pytest.mark.parametrize(
    ("code", "word"),
    [
        ("??", "Untracked"),
        ("M", "Modified"),
        ("A", "Added"),
        ("AM", "Modified"),
        ("D", "Deleted"),
        ("R", "Renamed"),
        ("MM", "Modified"),
        ("X", "X"),
    ],
)
def test_porcelain_status_word_matches_go_git_vocabulary(code: str, word: str) -> None:
    assert porcelain_status_word(code) == word


@pytest.mark.parametrize(
    ("output", "branch"),
    [
        ("## main\n M a.py\n", "main"),
        ("## main...origin/main [ahead 1, behind 2]\n", "main"),
        ("## No commits yet on feature/x\n?? a.py\n", "feature/x"),
        ("## HEAD (no branch)\n", None),
        (" M a.py\n", None),
    ],
)
def test_branch_from_porcelain_header(output: str, branch: str | None) -> None:
    assert branch_from_porcelain_header(output) == branch


# --------------------------------------------------------------------------------------
# History, fetch, and discard (worktree-aware git operations)
# --------------------------------------------------------------------------------------


def _demo_workspace(owner_id: UUID | None = None) -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=owner_id or uuid4(),
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


def _repo_service_with_exec(workspace: Workspace, exec_impl, *, token="raw-secret-token"):
    """Build a RepositoryService whose sandbox reports "cloned" and routes exec to ``exec_impl``."""

    class FakeFs:
        async def get_file_info(self, path):
            return SimpleNamespace(name=path)  # exists => treated as cloned

    class FakeRuntime:
        async def fs(self, ws):
            return FakeFs()

        async def exec(self, ws, command, *, cwd=None, env=None, timeout=None):
            return await exec_impl(command, cwd)

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        return workspace

    async def fake_get_auth(workspace_id: UUID) -> GitAuth:
        return GitAuth(kind="token", username="x-access-token", token=token)

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_auth = fake_get_auth  # type: ignore[method-assign]
    return service


def test_validate_commit_sha_accepts_hashes_and_rejects_refs():
    assert validate_commit_sha("abc1234") == "abc1234"
    assert validate_commit_sha("A" * 40) == "A" * 40
    for bad in ["", "HEAD", "main", "abc", "../etc/passwd", "abc123; rm -rf /", "g" * 8]:
        with pytest.raises(RepositoryPathError):
            validate_commit_sha(bad)


def test_parse_log_splits_records_and_fields():
    fs, rs = "\x1f", "\x1e"
    output = (
        f"h1{fs}s1{fs}Alice{fs}alice@x.com{fs}2026-01-01T00:00:00+00:00{fs}first commit{rs}\n"
        f"h2{fs}s2{fs}Bob{fs}bob@x.com{fs}2026-01-02T00:00:00+00:00{fs}second commit{rs}"
    )
    commits = RepositoryService._parse_log(output)
    assert commits == [
        GitCommit(
            hash="h1",
            short_hash="s1",
            author_name="Alice",
            author_email="alice@x.com",
            date="2026-01-01T00:00:00+00:00",
            subject="first commit",
        ),
        GitCommit(
            hash="h2",
            short_hash="s2",
            author_name="Bob",
            author_email="bob@x.com",
            date="2026-01-02T00:00:00+00:00",
            subject="second commit",
        ),
    ]


def test_parse_log_empty_output_is_empty_list():
    assert RepositoryService._parse_log("") == []


async def test_log_runs_git_log_scoped_to_worktree():
    workspace = _demo_workspace()
    fs, rs = "\x1f", "\x1e"
    log_out = f"deadbeef{fs}deadbee{fs}Alice{fs}a@x.com{fs}2026-01-01T00:00:00+00:00{fs}Init{rs}"
    calls: list[tuple[str, str | None]] = []

    async def exec_impl(command, cwd):
        calls.append((command, cwd))
        if command.startswith("git log"):
            return SimpleNamespace(exit_code=0, stdout=log_out, stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    commits = await service.log(
        workspace.id,
        workspace.owner_id,
        worktree_path="/workspace/worktrees/feat",
        limit=10,
    )

    assert [c.hash for c in commits] == ["deadbeef"]
    command, cwd = calls[-1]
    assert command.startswith("git log")
    assert "--pretty=format:" in command
    assert cwd == "/workspace/worktrees/feat"


async def test_log_empty_repository_returns_empty_list():
    workspace = _demo_workspace()

    async def exec_impl(command, cwd):
        return SimpleNamespace(
            exit_code=128,
            stdout="",
            stderr="fatal: your current branch 'main' does not have any commits yet",
        )

    service = _repo_service_with_exec(workspace, exec_impl)
    assert await service.log(workspace.id, workspace.owner_id) == []


async def test_commit_detail_returns_metadata_and_patch():
    workspace = _demo_workspace()
    fs = "\x1f"
    meta = (
        f"deadbeef{fs}deadbee{fs}Alice{fs}a@x.com{fs}2026-01-01T00:00:00+00:00"
        f"{fs}Add feature{fs}Body line"
    )
    patch = "\ndiff --git a/x b/x\n+new\n"

    async def exec_impl(command, cwd):
        if command.startswith("git show -s"):
            return SimpleNamespace(exit_code=0, stdout=meta, stderr="")
        if command.startswith("git show"):
            return SimpleNamespace(exit_code=0, stdout=patch, stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    detail = await service.commit_detail(workspace.id, workspace.owner_id, "deadbeef")

    assert isinstance(detail, GitCommitDetail)
    assert detail.hash == "deadbeef"
    assert detail.subject == "Add feature"
    assert detail.body == "Body line"
    # Leading blank line from the empty --pretty=format is trimmed so the diff renders cleanly.
    assert detail.diff == "diff --git a/x b/x\n+new\n"


async def test_commit_detail_rejects_non_sha():
    workspace = _demo_workspace()

    async def exec_impl(command, cwd):
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    with pytest.raises(RepositoryPathError):
        await service.commit_detail(workspace.id, workspace.owner_id, "HEAD")


async def test_fetch_uses_authenticated_git_in_worktree():
    import base64

    workspace = _demo_workspace()
    worktree_path = "/workspace/worktrees/feat-login"
    calls: list[tuple[str, str | None]] = []

    async def exec_impl(command, cwd):
        calls.append((command, cwd))
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    await service.fetch(workspace.id, workspace.owner_id, worktree_path=worktree_path)

    command, cwd = calls[-1]
    assert "fetch origin" in command
    assert cwd == worktree_path
    expected = base64.b64encode(b"x-access-token:raw-secret-token").decode()
    assert f"http.extraHeader=AUTHORIZATION: basic {expected}" in command
    assert "raw-secret-token" not in command


async def test_discard_all_resets_hard_and_cleans():
    workspace = _demo_workspace()
    calls: list[str] = []

    async def exec_impl(command, cwd):
        calls.append(command)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    await service.discard(workspace.id, workspace.owner_id)

    assert "git reset --hard HEAD" in calls
    assert "git clean -fd" in calls


async def test_discard_untracked_file_uses_clean():
    workspace = _demo_workspace()
    calls: list[str] = []

    async def exec_impl(command, cwd):
        calls.append(command)
        if command.startswith("git status --porcelain"):
            return SimpleNamespace(exit_code=0, stdout="?? new.py\n", stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    await service.discard(workspace.id, workspace.owner_id, path="new.py")

    assert any(c.startswith("git clean -fd -- new.py") for c in calls)
    assert not any("checkout" in c for c in calls)


async def test_discard_tracked_file_restores_from_head():
    workspace = _demo_workspace()
    calls: list[str] = []

    async def exec_impl(command, cwd):
        calls.append(command)
        if command.startswith("git status --porcelain"):
            return SimpleNamespace(exit_code=0, stdout=" M README.md\n", stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)
    await service.discard(workspace.id, workspace.owner_id, path="README.md")

    assert any(c.startswith("git checkout HEAD -- README.md") for c in calls)
    assert not any(c.startswith("git clean") for c in calls)


def test_worktree_sync_errors_map_to_expected_http_statuses():
    from app.api.workspaces import repository_error
    from app.services.worktrees import CannotSyncBaseWorktreeError, MergeConflictError

    conflict = MergeConflictError("CONFLICT (content): merge conflict")
    assert repository_error(conflict).status_code == 409
    assert repository_error(CannotSyncBaseWorktreeError()).status_code == 409


def _ssh_service(workspace: Workspace, exec_impl, fs_log: list[tuple[str, ...]]):
    private_key = generate_ed25519_keypair("enigma-test").private_key

    class FakeFs:
        async def get_file_info(self, path):
            return SimpleNamespace(name=path)

        async def create_folder(self, path, mode):
            fs_log.append(("create_folder", path, mode))

        async def upload_file(self, content, path):
            fs_log.append(("upload_file", path, content.decode()))

        async def set_file_permissions(self, path, mode=None, owner=None, group=None):
            fs_log.append(("chmod", path, mode or ""))

        async def delete_file(self, path, recursive=False):
            fs_log.append(("delete", path, str(recursive)))

    class FakeRuntime:
        async def fs(self, ws):
            return FakeFs()

        async def exec(self, ws, command, *, cwd=None, env=None, timeout=None):
            return await exec_impl(command, cwd, env)

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=FakeRuntime(),  # type: ignore[arg-type]
        settings=Settings(),
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        return workspace

    async def fake_get_auth(workspace_id: UUID) -> GitAuth:
        return GitAuth(kind="ssh", private_key=private_key)

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    service._get_auth = fake_get_auth  # type: ignore[method-assign]
    return service, private_key


async def test_push_uses_gitlab_oauth2_username_in_basic_header():
    import base64

    workspace = _demo_workspace()
    workspace.repo_url = "https://gitlab.com/example/demo.git"
    calls: list[str] = []

    async def exec_impl(command, cwd):
        calls.append(command)
        return SimpleNamespace(exit_code=0, stdout=workspace.repo_url, stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)

    async def fake_get_auth(workspace_id: UUID) -> GitAuth:
        return GitAuth(kind="token", username="oauth2", token="glpat-secret")

    service._get_auth = fake_get_auth  # type: ignore[method-assign]
    await service.push(workspace.id, workspace.owner_id)

    expected = base64.b64encode(b"oauth2:glpat-secret").decode()
    assert f"http.extraHeader=AUTHORIZATION: basic {expected}" in calls[-1]
    # origin already matched the workspace URL, so no remote add/set-url ran.
    assert not any("remote add" in c or "remote set-url" in c for c in calls)


async def test_push_adds_origin_when_missing_and_sets_url_when_changed():
    workspace = _demo_workspace()
    calls: list[str] = []
    origin: dict[str, str | None] = {"url": None}

    async def exec_impl(command, cwd):
        calls.append(command)
        if command == "git remote get-url origin":
            if origin["url"] is None:
                return SimpleNamespace(exit_code=2, stdout="", stderr="error: No such remote")
            return SimpleNamespace(exit_code=0, stdout=origin["url"] + "\n", stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    service = _repo_service_with_exec(workspace, exec_impl)

    await service.push(workspace.id, workspace.owner_id)
    assert any(c == f"git remote add origin {workspace.repo_url}" for c in calls)

    calls.clear()
    origin["url"] = "https://github.com/old/place.git"
    await service.fetch(workspace.id, workspace.owner_id)
    assert any(c == f"git remote set-url origin {workspace.repo_url}" for c in calls)
    assert "fetch origin" in calls[-1]


async def test_ssh_push_stages_key_passes_git_ssh_command_and_removes_key():
    workspace = _demo_workspace()
    workspace.repo_url = "ssh://git@git.example.com/team/demo.git"
    fs_log: list[tuple[str, ...]] = []
    execs: list[tuple[str, dict | None]] = []

    async def exec_impl(command, cwd, env):
        execs.append((command, env))
        return SimpleNamespace(exit_code=0, stdout=workspace.repo_url, stderr="")

    service, private_key = _ssh_service(workspace, exec_impl, fs_log)
    await service.push(workspace.id, workspace.owner_id)

    command, env = execs[-1]
    assert command.endswith("git push --set-upstream origin HEAD")
    assert command.startswith("mkdir -p /home/daytona/.ssh && ")
    assert env is not None
    ssh_command = env["GIT_SSH_COMMAND"]
    assert "-o IdentitiesOnly=yes" in ssh_command
    assert "-o BatchMode=yes" in ssh_command
    assert "-o StrictHostKeyChecking=accept-new" in ssh_command
    assert "/home/daytona/.ssh/known_hosts" in ssh_command
    assert env["GIT_TERMINAL_PROMPT"] == "0"

    ops = [entry[0] for entry in fs_log]
    assert ops == ["create_folder", "upload_file", "chmod", "delete"]
    key_dir = fs_log[0][1]
    assert key_dir.startswith("/tmp/enigma-ssh/")
    assert fs_log[0][2] == "700"
    assert fs_log[1][1] == f"{key_dir}/id_ed25519"
    assert fs_log[1][2] == private_key
    assert fs_log[2][2] == "600"
    assert fs_log[3][1] == key_dir and fs_log[3][2] == "True"
    assert f"-i {key_dir}/id_ed25519" in ssh_command
    assert private_key not in command


async def test_ssh_push_removes_key_even_when_git_fails_and_redacts_output():
    workspace = _demo_workspace()
    workspace.repo_url = "ssh://git@git.example.com/team/demo.git"
    fs_log: list[tuple[str, ...]] = []

    async def exec_impl(command, cwd, env):
        if "push" in command:
            return SimpleNamespace(
                exit_code=128,
                stdout="git@git.example.com: Permission denied (publickey).",
                stderr="",
            )
        return SimpleNamespace(exit_code=0, stdout=workspace.repo_url, stderr="")

    service, _private_key = _ssh_service(workspace, exec_impl, fs_log)
    with pytest.raises(GitCommandError) as excinfo:
        await service.push(workspace.id, workspace.owner_id)

    assert "Permission denied (publickey)" in excinfo.value.message
    assert [entry[0] for entry in fs_log][-1] == "delete"


async def test_ssh_push_reports_missing_ssh_client():
    workspace = _demo_workspace()
    workspace.repo_url = "ssh://git@git.example.com/team/demo.git"

    async def exec_impl(command, cwd, env):
        if "push" in command:
            return SimpleNamespace(
                exit_code=128, stdout="error: cannot run ssh: No such file or directory", stderr=""
            )
        return SimpleNamespace(exit_code=0, stdout=workspace.repo_url, stderr="")

    service, _ = _ssh_service(workspace, exec_impl, [])
    with pytest.raises(GitCommandError) as excinfo:
        await service.push(workspace.id, workspace.owner_id)
    assert "SSH client is not available" in excinfo.value.message


class FakeCredentialSession:
    """Minimal AsyncSession stand-in holding at most one credential row."""

    def __init__(self) -> None:
        self.credential = None
        self.commits = 0

    async def execute(self, statement):
        credential = self.credential
        return SimpleNamespace(scalar_one_or_none=lambda: credential)

    def add(self, obj) -> None:
        from app.models import RepositoryCredential

        if isinstance(obj, RepositoryCredential):
            self.credential = obj

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        return None


def _service_with_credential_session(workspace: Workspace):
    session = FakeCredentialSession()
    service = RepositoryService(
        session=session,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(Settings().workspace_env_encryption_key),
        runtime=SimpleNamespace(),  # type: ignore[arg-type]
        settings=Settings(),
    )

    async def fake_get_workspace(workspace_id: UUID, owner_id: UUID) -> Workspace:
        return workspace

    service._get_workspace = fake_get_workspace  # type: ignore[method-assign]
    return service, session


async def test_connect_pat_stores_provider_username_and_encrypted_token():
    workspace = _demo_workspace()
    service, session = _service_with_credential_session(workspace)

    await service.connect(
        workspace.id,
        workspace.owner_id,
        "https://bitbucket.org/team/demo",
        token="app-password",
        username="alice",
    )

    credential = session.credential
    assert credential.provider == "bitbucket"
    assert credential.auth_type == "personal_access_token"
    assert credential.username == "alice"
    assert credential.encrypted_token != "app-password"
    assert service.cipher.decrypt(credential.encrypted_token) == "app-password"
    assert credential.encrypted_private_key is None and credential.public_key is None
    assert workspace.repo_url == "https://bitbucket.org/team/demo.git"

    auth = await service._get_auth(workspace.id)
    assert auth == GitAuth(kind="token", username="alice", token="app-password")


async def test_connect_ssh_generates_keypair_once_and_reuses_it():
    workspace = _demo_workspace()
    service, session = _service_with_credential_session(workspace)

    first = await service.ensure_ssh_key(workspace.id, workspace.owner_id)
    public_key = first.public_key
    assert public_key.startswith("ssh-ed25519 ")
    assert first.encrypted_private_key != public_key
    private_key = service.cipher.decrypt(first.encrypted_private_key)
    assert private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")

    await service.connect(
        workspace.id, workspace.owner_id, "git@gitlab.com:team/demo.git", auth_type="ssh_key"
    )
    assert session.credential.public_key == public_key
    assert session.credential.provider == "gitlab"
    assert session.credential.encrypted_token is None
    assert workspace.repo_url == "ssh://git@gitlab.com/team/demo.git"

    auth = await service._get_auth(workspace.id)
    assert auth.kind == "ssh" and auth.private_key == private_key

    rotated = await service.ensure_ssh_key(workspace.id, workspace.owner_id, rotate=True)
    assert rotated.public_key != public_key


async def test_connect_ssh_with_https_url_raises_invalid_url():
    workspace = _demo_workspace()
    service, _session = _service_with_credential_session(workspace)
    with pytest.raises(InvalidRepositoryUrlError):
        await service.connect(
            workspace.id, workspace.owner_id, "https://gitlab.com/team/demo", auth_type="ssh_key"
        )
