from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import auth as auth_module
from app.api import files as files_module
from app.api.rate_limits import enforce_sandbox_rate_limit
from app.core.config import Settings
from app.core.security import hash_password
from app.main import app
from app.models import User, Workspace, WorkspaceWorktree
from app.services.files import FileService, FileTooLargeError
from app.services.users import DuplicateUserEmailError


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


class FakeSession:
    def __init__(
        self, workspace: Workspace, worktrees: list[WorkspaceWorktree] | None = None
    ) -> None:
        self.workspace = workspace
        self.worktrees = {worktree.id: worktree for worktree in (worktrees or [])}

    async def get(self, model, item_id):
        if model is Workspace and self.workspace.id == item_id:
            return self.workspace
        if model is WorkspaceWorktree:
            return self.worktrees.get(item_id)
        return None

    def add(self, value) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None


class LocalFileSystem:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, remote: str) -> Path:
        relative = remote.removeprefix("/workspace").lstrip("/")
        if ".." in Path(relative).parts:
            raise ValueError("outside workspace")
        return self.root / relative

    def info(self, path: Path):
        stat = path.stat()
        return SimpleNamespace(
            name=path.name or self.root.name,
            path=f"/workspace/{path.relative_to(self.root)}",
            is_dir=path.is_dir(),
            is_symlink=path.is_symlink(),
            size=stat.st_size,
            mod_time=datetime.fromtimestamp(stat.st_mtime, UTC),
        )

    async def get_file_info(self, remote: str):
        path = self.path(remote)
        if not path.exists():
            raise FileNotFoundError
        return self.info(path)

    async def list_files(self, remote: str):
        return [self.info(path) for path in self.path(remote).iterdir()]

    async def find_files(self, remote: str, pattern: str):
        query = pattern.strip("*").lower()
        return [
            SimpleNamespace(file=f"/workspace/{path.relative_to(self.root).as_posix()}")
            for path in self.root.rglob("*")
            if query in path.name.lower()
        ]

    async def download_file(self, remote: str):
        path = self.path(remote)
        if path.is_symlink():
            raise FileNotFoundError
        return path.read_bytes()

    async def upload_file(self, content: bytes, remote: str):
        path = self.path(remote)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    async def create_folder(self, remote: str, mode: str):
        self.path(remote).mkdir(parents=True, exist_ok=True)

    async def move_files(self, source: str, destination: str):
        target = self.path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path(source).rename(target)

    async def delete_file(self, remote: str, recursive: bool = False):
        path = self.path(remote)
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()


class FakeRuntime:
    def __init__(self, root: Path) -> None:
        self.sandbox = SimpleNamespace(fs=LocalFileSystem(root))

    async def fs(self, workspace):
        return self.sandbox.fs


@pytest.fixture
def file_context(tmp_path):
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
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = FileService(
        session=FakeSession(workspace),
        settings=Settings(
            file_max_read_bytes=32,
            file_max_upload_bytes=64,
            file_tree_max_entries=20,
        ),
        runtime=FakeRuntime(workspace_path),  # type: ignore[arg-type]
    )

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[enforce_sandbox_rate_limit] = lambda: None
    app.dependency_overrides[files_module.get_file_service] = lambda: service
    yield user, workspace, workspace_path
    app.dependency_overrides.clear()


async def login(client: AsyncClient) -> None:
    await client.post("/auth/login", json={"email": "owner@example.com", "password": "password"})


async def test_file_endpoints_require_auth(file_context):
    _user, workspace, _workspace_path = file_context
    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/workspaces/{workspace.id}/files/tree")

    assert response.status_code == 401


async def test_file_ownership_is_enforced(file_context):
    _user, workspace, _workspace_path = file_context
    workspace.owner_id = uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(f"/workspaces/{workspace.id}/files/tree")

    assert response.status_code == 404


async def test_tree_and_search_ignore_generated_paths(file_context):
    _user, workspace, workspace_path = file_context
    (workspace_path / "src").mkdir()
    (workspace_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (workspace_path / "node_modules").mkdir()
    (workspace_path / "node_modules" / "package.js").write_text("ignored", encoding="utf-8")
    (workspace_path / ".git").mkdir()
    (workspace_path / ".git" / "config").write_text("ignored", encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        tree = await client.get(f"/workspaces/{workspace.id}/files/tree")
        search_hit = await client.get(f"/workspaces/{workspace.id}/files/search?q=app")
        search_ignored = await client.get(f"/workspaces/{workspace.id}/files/search?q=package")

    tree_payload = tree.json()
    assert tree.status_code == 200
    assert [child["name"] for child in tree_payload["children"]] == ["src"]
    assert search_hit.status_code == 200
    assert search_hit.json()[0]["path"] == "src/app.py"
    assert search_ignored.json() == []


async def test_query_paths_accept_slash_prefixed_workspace_paths(file_context):
    _user, workspace, workspace_path = file_context
    (workspace_path / "README.md").write_text("hello", encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        tree = await client.get(f"/workspaces/{workspace.id}/files/tree?path=/")
        read = await client.get(f"/workspaces/{workspace.id}/files/read?path=/README.md")

    assert tree.status_code == 200
    assert tree.json()["path"] == ""
    assert read.status_code == 200
    assert read.json()["path"] == "README.md"


async def test_path_traversal_and_escaped_symlinks_are_rejected(file_context, tmp_path):
    _user, workspace, workspace_path = file_context
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    (workspace_path / "outside-link").symlink_to(outside_file)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        traversal = await client.get(f"/workspaces/{workspace.id}/files/read?path=../outside.txt")
        symlink = await client.get(f"/workspaces/{workspace.id}/files/read?path=outside-link")

    assert traversal.status_code == 400
    assert symlink.status_code == 400


async def test_read_download_and_file_size_limits(file_context):
    _user, workspace, workspace_path = file_context
    (workspace_path / "README.md").write_text("hello", encoding="utf-8")
    (workspace_path / "large.txt").write_text("x" * 40, encoding="utf-8")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        read = await client.get(f"/workspaces/{workspace.id}/files/read?path=README.md")
        download = await client.get(f"/workspaces/{workspace.id}/files/download?path=README.md")
        large = await client.get(f"/workspaces/{workspace.id}/files/read?path=large.txt")

    assert read.status_code == 200
    assert read.json()["content"] == "hello"
    assert download.status_code == 200
    assert download.text == "hello"
    assert large.status_code == 413


async def test_upload_create_rename_and_delete_stay_inside_workspace(file_context):
    _user, workspace, workspace_path = file_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        created_folder = await client.post(
            f"/workspaces/{workspace.id}/files/folders",
            json={"path": "docs"},
        )
        uploaded = await client.post(
            f"/workspaces/{workspace.id}/files/upload?path=docs&filename=note.txt",
            content=b"hello",
            headers={"content-type": "application/octet-stream"},
        )
        renamed = await client.patch(
            f"/workspaces/{workspace.id}/files/rename",
            json={"path": "docs/note.txt", "new_path": "docs/renamed.txt"},
        )
        deleted = await client.delete(f"/workspaces/{workspace.id}/files?path=docs/renamed.txt")
        rejected = await client.post(
            f"/workspaces/{workspace.id}/files/upload?path=..&filename=escape.txt",
            content=b"nope",
            headers={"content-type": "application/octet-stream"},
        )

    assert created_folder.status_code == 201
    assert uploaded.status_code == 200
    assert uploaded.json()["path"] == "docs/note.txt"
    assert renamed.status_code == 200
    assert renamed.json()["path"] == "docs/renamed.txt"
    assert deleted.status_code == 200
    assert not (workspace_path / "docs" / "renamed.txt").exists()
    assert rejected.status_code == 400


async def test_rename_rejects_directory_moved_into_itself(file_context):
    _user, workspace, workspace_path = file_context
    (workspace_path / "src" / "nested").mkdir(parents=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.patch(
            f"/workspaces/{workspace.id}/files/rename",
            json={"path": "src", "new_path": "src/nested/src"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "File path is invalid"
    assert (workspace_path / "src").is_dir()
    assert not (workspace_path / "src" / "nested" / "src").exists()


async def test_upload_rejects_oversized_content_before_service_upload(file_context, monkeypatch):
    _user, workspace, workspace_path = file_context
    service = app.dependency_overrides[files_module.get_file_service]()
    upload_called = False

    async def fail_upload(*args, **kwargs):
        nonlocal upload_called
        upload_called = True
        raise AssertionError("service upload should not be called")

    monkeypatch.setattr(service, "upload", fail_upload)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.post(
            f"/workspaces/{workspace.id}/files/upload?path=big.txt",
            content=b"x" * 65,
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 413
    assert upload_called is False
    assert not (workspace_path / "big.txt").exists()


async def test_limited_body_reader_stops_streaming_after_limit():
    class StreamingRequest:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.chunks_read = 0

        async def stream(self):
            for chunk in (b"1234", b"5678", b"9", b"0"):
                self.chunks_read += 1
                yield chunk

    request = StreamingRequest()

    with pytest.raises(FileTooLargeError):
        await files_module.read_limited_body(request, 8)

    assert request.chunks_read == 3


async def test_search_prunes_ignored_directories(file_context):
    _user, _workspace, workspace_path = file_context
    (workspace_path / "src").mkdir()
    (workspace_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (workspace_path / "node_modules" / "package").mkdir(parents=True)
    (workspace_path / "node_modules" / "package" / "index.js").write_text(
        "ignored",
        encoding="utf-8",
    )
    _user, workspace, _workspace_path = file_context
    service = app.dependency_overrides[files_module.get_file_service]()
    results = await service.search(workspace.id, workspace.owner_id, "index")
    assert results == []


@pytest.fixture
def worktree_context(tmp_path):
    """A workspace whose base clone and a feature worktree hold distinct files, so tests can
    assert file operations target the directory of the active worktree (``worktree_id``)."""
    user = User(
        id=uuid4(),
        email="owner@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    base_path = tmp_path / "workspace"
    base_path.mkdir()
    (base_path / "base.txt").write_text("base", encoding="utf-8")
    worktree_dir = base_path / "wt-feat"
    worktree_dir.mkdir()
    (worktree_dir / "feat.txt").write_text("feat", encoding="utf-8")
    workspace = Workspace(
        id=uuid4(),
        owner_id=user.id,
        name="Demo",
        slug="demo",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    worktree = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=workspace.id,
        branch="feat",
        path="/workspace/wt-feat",
        base_ref="main",
        is_base=False,
        status="active",
    )
    service = FileService(
        session=FakeSession(workspace, worktrees=[worktree]),
        settings=Settings(
            file_max_read_bytes=32,
            file_max_upload_bytes=64,
            file_tree_max_entries=20,
        ),
        runtime=FakeRuntime(base_path),  # type: ignore[arg-type]
    )

    app.dependency_overrides[auth_module.get_user_store] = lambda: FakeUserStore(user)
    app.dependency_overrides[auth_module.get_login_rate_limiter] = lambda: FakeRateLimiter()
    app.dependency_overrides[enforce_sandbox_rate_limit] = lambda: None
    app.dependency_overrides[files_module.get_file_service] = lambda: service
    yield user, workspace, worktree, base_path, worktree_dir
    app.dependency_overrides.clear()


async def test_tree_and_read_follow_active_worktree(worktree_context):
    _user, workspace, worktree, _base_path, _worktree_dir = worktree_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        base_tree = await client.get(f"/workspaces/{workspace.id}/files/tree")
        worktree_tree = await client.get(
            f"/workspaces/{workspace.id}/files/tree?worktree_id={worktree.id}"
        )
        worktree_read = await client.get(
            f"/workspaces/{workspace.id}/files/read?path=feat.txt&worktree_id={worktree.id}"
        )
        base_read_missing = await client.get(
            f"/workspaces/{workspace.id}/files/read?path=feat.txt"
        )

    base_names = {child["name"] for child in base_tree.json()["children"]}
    worktree_names = {child["name"] for child in worktree_tree.json()["children"]}
    assert "base.txt" in base_names
    assert "feat.txt" not in base_names
    assert worktree_names == {"feat.txt"}
    assert worktree_read.status_code == 200
    assert worktree_read.json()["content"] == "feat"
    assert base_read_missing.status_code == 404


async def test_mutations_target_active_worktree_directory(worktree_context):
    _user, workspace, worktree, base_path, worktree_dir = worktree_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        uploaded = await client.post(
            f"/workspaces/{workspace.id}/files/upload"
            f"?path=&filename=note.txt&worktree_id={worktree.id}",
            content=b"scoped",
            headers={"content-type": "application/octet-stream"},
        )

    assert uploaded.status_code == 200
    assert uploaded.json()["path"] == "note.txt"
    # Written into the worktree directory, not the base clone.
    assert (worktree_dir / "note.txt").read_text(encoding="utf-8") == "scoped"
    assert not (base_path / "note.txt").exists()


async def test_unknown_worktree_returns_404(worktree_context):
    _user, workspace, _worktree, _base_path, _worktree_dir = worktree_context

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(
            f"/workspaces/{workspace.id}/files/tree?worktree_id={uuid4()}"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Worktree not found"


async def test_worktree_from_another_workspace_returns_404(worktree_context):
    _user, workspace, _worktree, _base_path, _worktree_dir = worktree_context
    service = app.dependency_overrides[files_module.get_file_service]()
    foreign = WorkspaceWorktree(
        id=uuid4(),
        workspace_id=uuid4(),
        branch="feat",
        path="/workspace/wt-feat",
        base_ref="main",
        is_base=False,
        status="active",
    )
    service.session.worktrees[foreign.id] = foreign

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)
        response = await client.get(
            f"/workspaces/{workspace.id}/files/tree?worktree_id={foreign.id}"
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Worktree not found"
