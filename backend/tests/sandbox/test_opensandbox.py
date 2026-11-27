"""Provider-specific tests for the OpenSandbox runtime.

The conformance suite proves the contract; these cover what is unique to the adapter — the
``find_files`` content search (``grep``, not the SDK's glob-based ``files.search``), the
``SandboxException`` → ``SandboxRuntimeError`` mapping, and the execd-backed PTY behavior.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from opensandbox.exceptions import (
    SandboxApiException,
    SandboxConnectionException,
    SandboxException,
    SandboxTimeoutException,
)

from app.core.config import Settings
from app.services.sandbox.base import (
    RuntimeCommandResult,
    RuntimeUnavailableError,
    SandboxNotFoundError,
    SandboxRuntimeError,
)
from app.services.sandbox.opensandbox import (
    SEARCH_TIMEOUT_SECONDS,
    OpenSandboxMatch,
    OpenSandboxRuntime,
    map_opensandbox_error,
    parse_grep_matches,
)
from tests.sandbox.fakes import make_workspace, opensandbox_runtime_with


def test_configured_requires_api_url() -> None:
    assert OpenSandboxRuntime(Settings()).configured is False
    assert (
        OpenSandboxRuntime(Settings(opensandbox_api_url="http://127.0.0.1:8080")).configured
        is True
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SandboxApiException(status_code=404), SandboxNotFoundError),
        (SandboxApiException(status_code=500), SandboxRuntimeError),
        (SandboxConnectionException(), RuntimeUnavailableError),
        (SandboxTimeoutException(), RuntimeUnavailableError),
        (SandboxException(), SandboxRuntimeError),
        (ValueError("boom"), SandboxRuntimeError),
    ],
)
def test_map_opensandbox_error(error: Exception, expected: type[SandboxRuntimeError]) -> None:
    assert isinstance(map_opensandbox_error(error), expected)


def test_map_opensandbox_error_passthrough() -> None:
    original = SandboxNotFoundError("already mapped")
    assert map_opensandbox_error(original) is original


async def test_find_files_is_content_search_not_glob() -> None:
    runtime = opensandbox_runtime_with(exec_handler=None)
    fs = await runtime.fs(make_workspace("sandbox-1", runtime="opensandbox"))
    await fs.create_folder("/workspace/dir", "755")
    await fs.upload_file(b"hello world", "/workspace/dir/a.txt")

    matches = await fs.find_files("/workspace", "world")
    assert [(m.file, m.line, m.content) for m in matches] == [
        ("/workspace/dir/a.txt", 1, "hello world")
    ]
    # A glob pattern matches nothing by filename: this is grep, not ``files.search``.
    assert await fs.find_files("/workspace", "*.txt") == []


async def test_find_files_is_bounded_and_timeout_passed_through(monkeypatch) -> None:
    runtime = opensandbox_runtime_with(exec_handler=None)
    fs = await runtime.fs(make_workspace("sandbox-1", runtime="opensandbox"))
    await fs.create_folder("/workspace/dir", "755")
    await fs.upload_file(b"needle one\n", "/workspace/dir/a.txt")
    await fs.upload_file(b"needle two\n", "/workspace/dir/b.txt")

    assert [(m.file, m.line) for m in await fs.find_files("/workspace", "needle")] == [
        ("/workspace/dir/a.txt", 1),
        ("/workspace/dir/b.txt", 1),
    ]

    # The search is bounded: a timeout and ``skip_accumulation`` reach the SDK, so the command
    # cannot run forever and its output is never buffered in ``execution.logs`` (unbounded on a
    # large tree otherwise).
    (command, opts, handlers), *_ = fs._sandbox.commands.run_calls
    assert command[0] == "grep"
    assert opts.timeout == timedelta(seconds=SEARCH_TIMEOUT_SECONDS)
    assert handlers.skip_accumulation is True

    # A match set larger than the cap is truncated, and the record the cap cut is dropped.
    record = "/workspace/dir/a.txt\x001:needle\n"
    monkeypatch.setattr(
        "app.services.sandbox.opensandbox.MAX_SEARCH_OUTPUT_BYTES", len(record) + 5
    )
    assert [(m.file, m.line) for m in await fs.find_files("/workspace", "needle")] == [
        ("/workspace/dir/a.txt", 1)
    ]


def test_parse_grep_matches() -> None:
    text = "/workspace/a.txt\x001:hello\n/workspace/b.txt\x002:goodbye\n"
    assert parse_grep_matches(text) == [
        OpenSandboxMatch("/workspace/a.txt", 1, "hello"),
        OpenSandboxMatch("/workspace/b.txt", 2, "goodbye"),
    ]


async def test_preview_link_and_expose_port() -> None:
    runtime = opensandbox_runtime_with(exec_handler=None)
    workspace = make_workspace("sandbox-1", runtime="opensandbox")

    link = await runtime.preview_link(workspace, 5173)
    assert link.url.startswith("http")
    assert link.token == ""

    # expose_port is best-effort and never raises, even repeatedly.
    await runtime.expose_port(workspace, 5173)
    await runtime.expose_port(workspace, 5173)


async def test_pty_reconnect_keeps_one_execd_session() -> None:
    runtime = opensandbox_runtime_with(exec_handler=None)
    workspace = make_workspace("sandbox-1", runtime="opensandbox")

    async def on_data(_data: bytes) -> None:
        return None

    handle = await runtime.create_or_connect_pty(
        workspace, "term-1", on_data, env=None, cols=80, rows=24
    )
    assert handle is not None

    await runtime.kill_pty(workspace, "term-1")  # deletes the execd session
    await runtime.kill_pty(workspace, "term-1")  # idempotent: unknown session is a no-op


async def test_file_modes_reach_execd_as_octal_digits() -> None:
    """execd parses ``mode`` as octal digits (``ParseUint(..., 8)``): ``755`` means rwxr-xr-x.
    Sending the decimal value of 0o755 (493) is rejected live with "invalid syntax"."""
    runtime = opensandbox_runtime_with(exec_handler=None)
    fs = await runtime.fs(make_workspace("sandbox-1", runtime="opensandbox"))
    await fs.create_folder("/workspace/dir", "755")
    await fs.upload_file(b"x", "/workspace/dir/a.txt")
    await fs.set_file_permissions("/workspace/dir/a.txt", mode="600")

    modes = fs._sandbox.files._store.modes
    assert modes["/workspace/dir"] == 755
    assert modes["/workspace/dir/a.txt"] == 600


async def test_relative_paths_resolve_against_the_sandbox_home() -> None:
    """Workspaces store a relative ``workspace_path`` (``"workspace"``). Daytona resolves it
    against the sandbox home and the Docker provider mirrors that; execd resolves it against
    ``/``, where ``mkdir /workspace`` is denied for the sandbox user. So the provider must
    make relative paths absolute itself, for files and for command working directories."""
    from app.core.config import get_settings

    home = get_settings().sandbox_home.rstrip("/")
    runtime = opensandbox_runtime_with(
        exec_handler=lambda _c: RuntimeCommandResult(exit_code=0, stdout="", stderr="")
    )
    workspace = make_workspace("sandbox-1", runtime="opensandbox")
    workspace.workspace_path = "workspace"
    fs = await runtime.fs(workspace)

    await fs.create_folder("workspace", "755")
    await fs.upload_file(b"x", "workspace/a.txt")
    store = fs._sandbox.files._store
    assert f"{home}/workspace" in store.dirs
    assert f"{home}/workspace/a.txt" in store.files
    assert await fs.download_file("workspace/a.txt") == b"x"
    # Absolute paths are left alone.
    await fs.upload_file(b"y", "/tmp/b.txt")
    assert "/tmp/b.txt" in store.files

    await runtime.exec(workspace, "true")  # no cwd: the workspace, like Daytona
    await runtime.exec(workspace, "true", cwd="workspace/sub")
    await runtime.exec(workspace, "true", cwd="/srv")
    cwds = [opts.working_directory for _cmd, opts, _h in fs._sandbox.commands.run_calls]
    assert cwds == [f"{home}/workspace", f"{home}/workspace/sub", "/srv"]


async def test_file_info_finds_a_relative_path() -> None:
    """The SDK answers ``get_file_info`` keyed by the absolute path it was asked about; looking
    the result up by the caller's relative path reported every relative path as missing --
    found live when Spec Kit's commit could not see ``workspace/.git``."""
    runtime = opensandbox_runtime_with(exec_handler=None)
    workspace = make_workspace("sandbox-1", runtime="opensandbox")
    workspace.workspace_path = "workspace"
    fs = await runtime.fs(workspace)
    await fs.create_folder("workspace/.git", "755")

    info = await fs.get_file_info("workspace/.git")
    assert info is not None
    with pytest.raises(FileNotFoundError):
        await fs.get_file_info("workspace/missing")


# --- execd delivers stdout one line per message (found by the Plans 4-6 live E2E) ----------

async def test_exec_keeps_the_lines_of_its_output() -> None:
    """A screenshot capture printed its temp dir and the PNG's size on two lines; joined with
    "" they arrived as one ``/tmp/tmp.X25957`` and the screenshot was lost."""
    runtime = opensandbox_runtime_with(
        lambda _cmd: RuntimeCommandResult(
            exit_code=0, stdout="/tmp/tmp.X\n25957\n\nend\n", stderr="e1\ne2\n"
        )
    )
    result = await runtime.exec(make_workspace(runtime="opensandbox"), "capture")
    assert result.stdout.splitlines() == ["/tmp/tmp.X", "25957", "", "end"]
    assert result.stderr.splitlines() == ["e1", "e2"]


async def test_streamed_output_keeps_its_lines() -> None:
    runtime = opensandbox_runtime_with(
        lambda _cmd: RuntimeCommandResult(exit_code=0, stdout="one\ntwo\n\nthree\n", stderr="")
    )
    chunks: list[str] = []

    async def out(text: str) -> None:
        chunks.append(text)

    async def err(_text: str) -> None:
        return None

    await runtime.stream_command(
        make_workspace(runtime="opensandbox"),
        "run-1",
        "x",
        cwd=None,
        env=None,
        timeout=5,
        on_stdout=out,
        on_stderr=err,
    )
    assert "".join(chunks) == "one\ntwo\n\nthree\n"
