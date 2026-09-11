from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models import Workspace, WorkspaceTemplate
from app.services.workspace_init import WorkspaceInitializationError, WorkspaceInitializer


def _workspace(name: str = "Demo", branch: str = "main") -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name=name,
        slug="demo",
        repo_url=None,
        source="blank",
        default_branch=branch,
        runtime="daytona",
        daytona_sandbox_id="sandbox-1",
        workspace_path="/workspace",
        status="creating",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


DEFAULT_MISE = "[tasks.dev]\nrun = 'npm run dev'\n"


def _template(*, mise_config: str | None = DEFAULT_MISE) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id=uuid4(),
        slug="react-vite",
        name="React",
        description="",
        source_repo_url="https://github.com/github/codespaces-react.git",
        source_ref=None,
        default_branch="main",
        mise_config=mise_config,
        is_active=True,
    )


class FakeFs:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or set()
        self.uploads: list[tuple[str, str]] = []
        self.folders: list[str] = []

    async def get_file_info(self, path):
        if path in self.existing:
            return SimpleNamespace(name=path)
        raise FileNotFoundError(path)

    async def create_folder(self, path, mode):
        self.folders.append(path)
        self.existing.add(path)

    async def upload_file(self, content, remote_path):
        self.uploads.append((remote_path, content.decode()))
        self.existing.add(remote_path)


class FakeRuntime:
    def __init__(self, fs: FakeFs, fail_on: str | None = None) -> None:
        self.fs = fs
        self.commands: list[tuple[str, str | None, dict | None]] = []
        self.fail_on = fail_on

    async def get_sandbox(self, workspace):
        return SimpleNamespace(fs=self.fs)

    async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None):
        self.commands.append((command, cwd, env))
        if self.fail_on and self.fail_on in command:
            return SimpleNamespace(
                exit_code=128,
                stdout="",
                stderr="fatal: could not read from 'https://x:tok@github.com/x/y.git'",
            )
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


async def test_initialize_from_template_clones_strips_git_writes_mise_and_commits():
    fs = FakeFs(existing={"/workspace"})
    runtime = FakeRuntime(fs)
    workspace = _workspace(branch="develop")

    await WorkspaceInitializer(runtime).initialize_from_template(workspace, _template())  # type: ignore[arg-type]

    commands = [c for c, _cwd, _env in runtime.commands]
    assert commands == [
        "git clone --depth 1 -- https://github.com/github/codespaces-react.git .",
        "rm -rf .git",
        "git init -b develop",
        "git add -A",
        "git -c 'user.name=Enigma app' -c user.email=author@enigma.ai commit -m "
        "'Initial commit from template react-vite'",
    ]
    assert all(cwd == "/workspace" for _c, cwd, _e in runtime.commands)
    assert all(env == {"GIT_TERMINAL_PROMPT": "0"} for _c, _cwd, env in runtime.commands)
    assert fs.uploads == [("/workspace/mise.toml", "[tasks.dev]\nrun = 'npm run dev'\n")]


async def test_initialize_from_template_skips_mise_when_repo_has_one_and_uses_source_ref():
    fs = FakeFs(existing={"/workspace", "/workspace/.mise.toml"})
    runtime = FakeRuntime(fs)
    template = _template()
    template.source_ref = "v2"

    await WorkspaceInitializer(runtime).initialize_from_template(_workspace(), template)  # type: ignore[arg-type]

    assert runtime.commands[0][0] == (
        "git clone --depth 1 --branch v2 -- https://github.com/github/codespaces-react.git ."
    )
    assert fs.uploads == []


async def test_initialize_blank_creates_readme_and_initial_commit():
    fs = FakeFs()
    runtime = FakeRuntime(fs)
    workspace = _workspace(name="My Scratchpad")

    await WorkspaceInitializer(runtime).initialize_blank(workspace)  # type: ignore[arg-type]

    assert fs.folders == ["/workspace"]
    assert fs.uploads == [("/workspace/README.md", "# My Scratchpad\n\nCreated with Enigma.\n")]
    commands = [c for c, _cwd, _env in runtime.commands]
    assert commands[0] == "git init -b main"
    assert commands[-1].endswith("commit -m 'Initial commit'")


async def test_initialize_is_noop_when_git_already_exists():
    fs = FakeFs(existing={"/workspace", "/workspace/.git"})
    runtime = FakeRuntime(fs)

    await WorkspaceInitializer(runtime).initialize_blank(_workspace())  # type: ignore[arg-type]

    assert runtime.commands == []
    assert fs.uploads == []


async def test_initialize_raises_on_clone_failure_with_redacted_output():
    runtime = FakeRuntime(FakeFs(existing={"/workspace"}), fail_on="clone")

    with pytest.raises(WorkspaceInitializationError) as excinfo:
        await WorkspaceInitializer(runtime).initialize_from_template(_workspace(), _template())  # type: ignore[arg-type]

    assert "tok" not in excinfo.value.message
    assert "https://[redacted]@github.com" in excinfo.value.message
