"""Plan 6 Task 3: a screenshot is taken inside the tenant's sandbox and validated."""

from __future__ import annotations

import pytest

from app.models import Workspace
from app.services.sandbox import SandboxRuntimeError
from app.services.sandbox.base import RuntimeCommandResult
from app.services.verification.screenshot import (
    MAX_SCREENSHOT_BYTES,
    PNG_SIGNATURE,
    ScreenshotError,
    capture_screenshot,
)
from tests.sandbox.fakes import FakeFileSystem, FakeSandboxRuntime

PNG = PNG_SIGNATURE + b"rest-of-image"
TMP = "/tmp/tmp.AbC123"


def _runtime(*, files=None, stdout=None, exit_code=0, error=None):
    if stdout is None:  # the dir, then the size the sandbox measured
        size = len((files or {}).get(f"{TMP}/shot.png", b""))
        stdout = f"{TMP}\n{size}\n"
    def handler(command: str) -> RuntimeCommandResult:
        if error is not None and "chromium" in command:
            raise error
        if "chromium" in command:
            return RuntimeCommandResult(exit_code=exit_code, stdout=stdout, stderr="")
        return RuntimeCommandResult(exit_code=0, stdout="", stderr="")

    return FakeSandboxRuntime(
        filesystem=FakeFileSystem(files=files or {}), exec_handler=handler
    )


def _workspace() -> Workspace:
    return Workspace(sandbox_id="sbx", workspace_path="workspace", runtime="docker")


async def test_captures_the_app_inside_the_sandbox_and_cleans_up() -> None:
    runtime = _runtime(files={f"{TMP}/shot.png": PNG})

    data = await capture_screenshot(runtime, _workspace(), 5173)

    assert data == PNG
    capture, cleanup = (call["command"] for call in runtime.exec_calls)
    # Absolute binaries only: ~/.local/bin leads PATH and belongs to the tenant.
    assert "/usr/bin/chromium --headless=new" in capture
    assert "/usr/bin/mktemp -d" in capture
    assert "http://127.0.0.1:5173/" in capture
    assert cleanup.startswith("/bin/rm -rf -- ")
    assert TMP in cleanup


@pytest.mark.parametrize(
    ("files", "stdout", "exit_code"),
    [
        ({f"{TMP}/shot.png": b"<html>not a png"}, None, 0),  # not a PNG
        ({f"{TMP}/shot.png": PNG + b"x" * MAX_SCREENSHOT_BYTES}, f"{TMP}\n100\n", 0),  # grew
        ({}, f"{TMP}\n0\n", 0),  # the browser produced nothing
        ({}, "\n", 0),  # mktemp failed
        ({}, "relative/dir\n10\n", 0),  # never trust a non-absolute dir
        ({f"{TMP}/shot.png": PNG}, f"{TMP}\n14\n", 1),  # the shell itself failed
        ({}, f"{TMP}\nnot-a-size\n", 0),  # garbage instead of a size
    ],
)
async def test_bad_captures_raise_screenshot_error(files, stdout, exit_code) -> None:
    runtime = _runtime(files=files, stdout=stdout, exit_code=exit_code)
    with pytest.raises(ScreenshotError):
        await capture_screenshot(runtime, _workspace(), 3000)


async def test_a_provider_error_is_a_screenshot_error() -> None:
    runtime = _runtime(error=SandboxRuntimeError("sandbox gone"))
    with pytest.raises(ScreenshotError):
        await capture_screenshot(runtime, _workspace(), 3000)


async def test_an_oversize_screenshot_is_refused_before_it_is_downloaded() -> None:
    """Security review: the size is checked in the sandbox, so a huge file never reaches the
    backend's memory (the download itself may be up to 64 MiB)."""
    runtime = _runtime(files={}, stdout=f"{TMP}\n{MAX_SCREENSHOT_BYTES + 1}\n")

    with pytest.raises(ScreenshotError, match="2 MB"):
        await capture_screenshot(runtime, _workspace(), 3000)
    # Cleaned up even though nothing was downloaded.
    assert runtime.exec_calls[-1]["command"].startswith("/bin/rm -rf -- ")
