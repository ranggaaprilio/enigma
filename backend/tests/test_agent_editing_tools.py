# ruff: noqa: E501

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models import Workspace
from app.services.agent_tools import (
    DangerousCommandError,
    GitStatusResult,
    PatchError,
    PatchResult,
    SearchResult,
    WorkspaceToolError,
    WorkspaceTools,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeExecResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class FakeFs:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.uploads: list[tuple[str, bytes]] = []
        self.created_folders: list[tuple[str, str]] = []

    async def download_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def upload_file(self, content: bytes, remote: str) -> None:
        self.uploads.append((remote, content))
        self.files[remote] = content

    async def create_folder(self, path: str, mode: str) -> None:
        self.created_folders.append((path, mode))


class FakeRuntime:
    def __init__(
        self,
        fs: FakeFs | None = None,
        exec_handler: Callable[[str], FakeExecResult] | None = None,
    ) -> None:
        self.fs = fs or FakeFs()
        self.exec_handler = exec_handler
        self.exec_calls: list[dict] = []

    async def get_sandbox(self, workspace: Workspace):
        return type("Sandbox", (), {"fs": self.fs})()

    async def exec(self, workspace, command, *, cwd=None, timeout=None) -> FakeExecResult:
        self.exec_calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        if self.exec_handler is not None:
            return self.exec_handler(command)
        return FakeExecResult()


def make_workspace() -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=uuid4(),
        name="Demo",
        slug="demo-edit",
        repo_url="https://github.com/example/demo",
        default_branch="main",
        runtime="daytona",
        daytona_sandbox_id="sandbox-demo",
        workspace_path="workspace",
        status="ready",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def make_tools(
    *,
    fs: FakeFs | None = None,
    exec_handler: Callable[[str], FakeExecResult] | None = None,
    allowlist_enabled: bool = False,
    allowed_commands: frozenset[str] | None = None,
    deny_dangerous: bool = True,
) -> tuple[WorkspaceTools, FakeRuntime]:
    runtime = FakeRuntime(fs=fs, exec_handler=exec_handler)
    tools = WorkspaceTools(
        make_workspace(),
        runtime=runtime,  # type: ignore[arg-type]
        command_timeout_seconds=20,
        max_command_output_chars=100_000,
        allowlist_enabled=allowlist_enabled,
        allowed_commands=allowed_commands,
        deny_dangerous=deny_dangerous,
    )
    return tools, runtime


SINGLE_BLOCK_PATCH = """<<<<<<< SEARCH
    return a - b
=======
    return a + b
>>>>>>> REPLACE"""


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------


async def test_apply_patch_single_block_writes_once():
    fs = FakeFs({"workspace/app.py": b"def add(a, b):\n    return a - b\n"})
    tools, _ = make_tools(fs=fs)

    result = await tools.apply_patch("app.py", SINGLE_BLOCK_PATCH)

    assert isinstance(result, PatchResult)
    assert result.blocks_applied == 1
    assert result.path == "app.py"
    assert "-    return a - b" in result.diff
    assert "+    return a + b" in result.diff
    assert len(fs.uploads) == 1
    remote, content = fs.uploads[0]
    assert remote == "workspace/app.py"
    assert content.decode("utf-8") == "def add(a, b):\n    return a + b\n"


async def test_apply_patch_multiple_blocks():
    fs = FakeFs({"workspace/m.py": b"alpha\nbeta\ngamma\n"})
    tools, _ = make_tools(fs=fs)
    patch = (
        "<<<<<<< SEARCH\nalpha\n=======\nALPHA\n>>>>>>> REPLACE\n"
        "<<<<<<< SEARCH\ngamma\n=======\nGAMMA\n>>>>>>> REPLACE\n"
    )

    result = await tools.apply_patch("m.py", patch)

    assert result.blocks_applied == 2
    assert fs.uploads[0][1].decode("utf-8") == "ALPHA\nbeta\nGAMMA\n"


async def test_apply_patch_no_match_does_not_write():
    fs = FakeFs({"workspace/app.py": b"def add(a, b):\n    return a + b\n"})
    tools, _ = make_tools(fs=fs)
    patch = "<<<<<<< SEARCH\n    return NOPE\n=======\n    return x\n>>>>>>> REPLACE"

    with pytest.raises(PatchError, match="not found"):
        await tools.apply_patch("app.py", patch)
    assert fs.uploads == []


async def test_apply_patch_ambiguous_match_does_not_write():
    fs = FakeFs({"workspace/dup.py": b"x = 1\nx = 1\n"})
    tools, _ = make_tools(fs=fs)
    patch = "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE"

    with pytest.raises(PatchError, match="ambiguous"):
        await tools.apply_patch("dup.py", patch)
    assert fs.uploads == []


async def test_apply_patch_file_missing():
    fs = FakeFs()
    tools, _ = make_tools(fs=fs)

    with pytest.raises(WorkspaceToolError, match="File does not exist"):
        await tools.apply_patch("nope.py", SINGLE_BLOCK_PATCH)
    assert fs.uploads == []


async def test_apply_patch_empty_and_malformed():
    fs = FakeFs({"workspace/app.py": b"hello\n"})
    tools, _ = make_tools(fs=fs)

    with pytest.raises(PatchError):
        await tools.apply_patch("app.py", "   ")
    with pytest.raises(PatchError, match="REPLACE"):
        await tools.apply_patch("app.py", "<<<<<<< SEARCH\nhello\n=======\nbye\n")
    assert fs.uploads == []


async def test_apply_patch_traversal_blocked():
    fs = FakeFs({"workspace/app.py": b"hello\n"})
    tools, _ = make_tools(fs=fs)

    with pytest.raises(WorkspaceToolError):
        await tools.apply_patch("../escape.py", SINGLE_BLOCK_PATCH)


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------


async def test_search_code_parses_rg_output_and_strips_prefix():
    def handler(command: str) -> FakeExecResult:
        return FakeExecResult(
            exit_code=0,
            stdout=(
                "app.py:3:    return a - b\n"
                "./util.py:10:x = 1\n"
                "workspace/sub/deep.py:7:found\n"
            ),
        )

    tools, runtime = make_tools(exec_handler=handler)
    result = await tools.search_code("return")

    assert isinstance(result, SearchResult)
    assert result.truncated is False
    assert [(m.path, m.line_number) for m in result.matches] == [
        ("app.py", 3),
        ("util.py", 10),
        ("sub/deep.py", 7),
    ]
    assert runtime.exec_calls[0]["command"].startswith("rg ")
    assert runtime.exec_calls[0]["cwd"] == "workspace"


async def test_search_code_passes_explicit_path_to_rg():
    # Regression: without a path argument ripgrep reads from stdin (empty)
    # under a non-interactive exec and matches nothing, so searches always
    # come back empty. The command must end with an explicit "." search path.
    def handler(command: str) -> FakeExecResult:
        return FakeExecResult(exit_code=0, stdout="app.py:1:hit\n")

    tools, runtime = make_tools(exec_handler=handler)
    await tools.search_code("hit")

    command = runtime.exec_calls[0]["command"]
    assert command.startswith("rg ")
    assert command.split()[-1] == "."


async def test_search_code_falls_back_to_grep_when_rg_missing():
    calls: list[str] = []

    def handler(command: str) -> FakeExecResult:
        calls.append(command)
        if command.startswith("rg "):
            return FakeExecResult(exit_code=127, stdout="rg: command not found")
        return FakeExecResult(exit_code=0, stdout="app.py:1:hit\n")

    tools, runtime = make_tools(exec_handler=handler)
    result = await tools.search_code("hit")

    assert len(result.matches) == 1
    assert result.matches[0].path == "app.py"
    assert runtime.exec_calls[1]["command"].startswith("grep ")


async def test_search_code_no_matches_is_not_an_error():
    tools, _ = make_tools(exec_handler=lambda command: FakeExecResult(exit_code=1, stdout=""))

    result = await tools.search_code("nothing")

    assert result.matches == []
    assert result.truncated is False


async def test_search_code_glob_and_global_cap():
    def handler(command: str) -> FakeExecResult:
        return FakeExecResult(
            exit_code=0,
            stdout="a.py:1:one\nb.py:2:two\nc.py:3:three\n",
        )

    tools, runtime = make_tools(exec_handler=handler)
    result = await tools.search_code("x", path_glob="*.py", max_results=2)

    assert len(result.matches) == 2
    assert result.truncated is True
    assert "*.py" in runtime.exec_calls[0]["command"]


async def test_search_code_empty_query_rejected():
    tools, _ = make_tools()
    with pytest.raises(WorkspaceToolError):
        await tools.search_code("  ")


# ---------------------------------------------------------------------------
# git_status / git_diff
# ---------------------------------------------------------------------------


async def test_git_status_parses_porcelain_and_branch():
    def handler(command: str) -> FakeExecResult:
        if "status" in command:
            return FakeExecResult(exit_code=0, stdout=" M README.md\n?? new.py\n")
        if "branch" in command:
            return FakeExecResult(exit_code=0, stdout="feat\n")
        return FakeExecResult()

    tools, _ = make_tools(exec_handler=handler)
    result = await tools.git_status()

    assert isinstance(result, GitStatusResult)
    assert result.branch == "feat"
    assert result.is_clean is False
    assert [(f.path, f.status) for f in result.files] == [
        ("README.md", "M"),
        ("new.py", "??"),
    ]


async def test_git_status_clean_tree():
    def handler(command: str) -> FakeExecResult:
        if "branch" in command:
            return FakeExecResult(exit_code=0, stdout="main\n")
        return FakeExecResult(exit_code=0, stdout="")

    tools, _ = make_tools(exec_handler=handler)
    result = await tools.git_status()

    assert result.is_clean is True
    assert result.files == []
    assert result.branch == "main"


async def test_git_status_error_raises():
    tools, _ = make_tools(
        exec_handler=lambda command: FakeExecResult(exit_code=128, stderr="fatal: not a git repo")
    )
    with pytest.raises(WorkspaceToolError, match="not a git repo"):
        await tools.git_status()


async def test_git_diff_returns_output_and_scopes_path():
    def handler(command: str) -> FakeExecResult:
        return FakeExecResult(exit_code=0, stdout="diff --git a/x b/x\n+line\n")

    tools, runtime = make_tools(exec_handler=handler)

    full = await tools.git_diff()
    assert "diff --git" in full
    assert runtime.exec_calls[0]["command"] == "git diff --"

    scoped = await tools.git_diff("README.md")
    assert "diff --git" in scoped
    assert "README.md" in runtime.exec_calls[1]["command"]


async def test_git_diff_invalid_path_rejected():
    tools, _ = make_tools(exec_handler=lambda command: FakeExecResult())
    with pytest.raises(WorkspaceToolError):
        await tools.git_diff("../outside")


async def test_git_diff_error_raises():
    tools, _ = make_tools(
        exec_handler=lambda command: FakeExecResult(exit_code=1, stderr="boom")
    )
    with pytest.raises(WorkspaceToolError, match="boom"):
        await tools.git_diff()


# ---------------------------------------------------------------------------
# run_command allowlist mode
# ---------------------------------------------------------------------------


def test_validate_command_denylist_default_blocks_git():
    tools, _ = make_tools()
    assert tools._validate_command("pytest -q") == ["pytest", "-q"]
    with pytest.raises(DangerousCommandError):
        tools._validate_command("git status")


def test_validate_command_allowlist_supersedes_denylist():
    tools, _ = make_tools(
        allowlist_enabled=True,
        allowed_commands=frozenset({"pytest", "git"}),
    )

    assert tools._validate_command("pytest -q") == ["pytest", "-q"]
    # git is denylisted, but the allowlist supersedes it.
    assert tools._validate_command("git status") == ["git", "status"]
    # Not in the allowlist -> rejected.
    with pytest.raises(DangerousCommandError):
        tools._validate_command("ls -la")
    # Shell operators and absolute paths are always rejected.
    with pytest.raises(DangerousCommandError):
        tools._validate_command("pytest && ls")
    with pytest.raises(DangerousCommandError):
        tools._validate_command("/bin/pytest")


def test_validate_command_deny_dangerous_false_relaxes_denylist():
    tools, _ = make_tools(deny_dangerous=False)

    # npm is denylisted by default, but allowed when the denylist is relaxed.
    assert tools._validate_command("npm install") == ["npm", "install"]
    assert tools._validate_command("pnpm test") == ["pnpm", "test"]
    # Shell operators and absolute paths are always rejected regardless.
    with pytest.raises(DangerousCommandError):
        tools._validate_command("npm install && npm test")
    with pytest.raises(DangerousCommandError):
        tools._validate_command("/usr/bin/npm install")
