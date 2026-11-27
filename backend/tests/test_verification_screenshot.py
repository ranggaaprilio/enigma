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
        if error is not None and "/usr/bin/python3" in command:
            raise error
        if "/usr/bin/python3" in command:
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
    # Absolute binaries only: ~/.local/bin leads PATH and belongs to the tenant. The
    # system Python runs isolated (-I -S), so no tenant env, site-packages or cwd module
    # can reach the driver, which drives the image's own Chromium.
    assert "/usr/bin/python3 -I -S -c " in capture
    assert "'/usr/bin/chromium'" in capture or '"/usr/bin/chromium"' in capture
    assert "/usr/bin/mktemp -d" in capture
    assert "http://127.0.0.1:5173/" in capture
    # Not the CLI's --screenshot, which fires on `load`, before scripts reveal the page.
    assert "--screenshot=" not in capture
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


# --- the in-sandbox driver (cdp_capture.py), against a fake browser on the same pipe -------

FAKE_BROWSER = r'''
import base64, json, os, sys, threading, time

LOG = os.environ["FAKE_LOG"]
LOAD = os.environ["FAKE_LOAD"]  # "fires" or "never"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake").decode()


def log(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps({**entry, "t": time.monotonic()}) + "\n")


def send(msg):
    os.write(4, json.dumps(msg).encode() + b"\0")


log({"argv": sys.argv[1:]})
buf = b""
while True:
    chunk = os.read(3, 65536)
    if not chunk:
        break
    buf += chunk
    while b"\0" in buf:
        raw, buf = buf.split(b"\0", 1)
        msg = json.loads(raw)
        log({"method": msg["method"], "params": msg.get("params", {})})
        result = {}
        if msg["method"] == "Target.getTargets":
            result = {"targetInfos": [{"type": "page", "targetId": "T1"}]}
        elif msg["method"] == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif msg["method"] == "Page.captureScreenshot":
            result = {"data": PNG}
        send({"id": msg["id"], "result": result, "sessionId": msg.get("sessionId")})
        if msg["method"] == "Page.navigate" and LOAD == "fires":
            log({"event": "Page.loadEventFired"})
            send({"method": "Page.loadEventFired", "params": {}, "sessionId": "S1"})
'''


def _fake_browser(tmp_path, load: str, monkeypatch):
    import sys

    script = tmp_path / "fake_browser.py"
    script.write_text(f"#!{sys.executable}\n" + FAKE_BROWSER)
    script.chmod(0o755)
    log = tmp_path / "browser.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_LOAD", load)
    return str(script), log


def _entries(log):
    import json

    return [json.loads(line) for line in log.read_text().splitlines()]


def test_driver_captures_only_after_load_and_a_real_time_settle(tmp_path, monkeypatch) -> None:
    from app.services.verification import cdp_capture

    browser, log = _fake_browser(tmp_path, "fires", monkeypatch)
    out = tmp_path / "shot.png"

    cdp_capture.capture(
        "http://127.0.0.1:3000/", str(out), str(tmp_path / "profile"),
        browser=browser, settle=0.3, load_timeout=5,
    )

    assert out.read_bytes().startswith(PNG_SIGNATURE)
    entries = _entries(log)
    argv = entries[0]["argv"]
    assert "--remote-debugging-pipe" in argv and "--headless=new" in argv
    assert f"--user-data-dir={tmp_path / 'profile'}" in argv  # a throwaway profile
    methods = [e.get("method") or e.get("event") for e in entries[1:]]
    metrics = next(e for e in entries if e.get("method") == "Emulation.setDeviceMetricsOverride")
    assert (metrics["params"]["width"], metrics["params"]["height"]) == (1280, 800)
    assert methods.index("Emulation.setDeviceMetricsOverride") < methods.index("Page.navigate")
    loaded = next(e["t"] for e in entries if e.get("event") == "Page.loadEventFired")
    shot = next(e for e in entries if e.get("method") == "Page.captureScreenshot")
    assert shot["t"] - loaded >= 0.3  # scripts and animations get real time to run
    assert shot["params"]["format"] == "png"


def test_driver_still_captures_when_load_never_fires(tmp_path, monkeypatch) -> None:
    """A page whose `load` hangs on some third-party asset is still captured, bounded."""
    import time

    from app.services.verification import cdp_capture

    browser, _log = _fake_browser(tmp_path, "never", monkeypatch)
    out = tmp_path / "shot.png"

    start = time.monotonic()
    cdp_capture.capture(
        "http://127.0.0.1:3000/", str(out), str(tmp_path / "profile"),
        browser=browser, settle=0.1, load_timeout=0.5,
    )

    assert out.read_bytes().startswith(PNG_SIGNATURE)
    assert time.monotonic() - start < 5


def test_driver_fails_cleanly_when_the_browser_dies(tmp_path) -> None:
    from app.services.verification import cdp_capture

    out = tmp_path / "shot.png"
    with pytest.raises(Exception):  # noqa: B017 -- any failure; the shell reports size 0
        cdp_capture.capture(
            "http://127.0.0.1:3000/", str(out), str(tmp_path / "profile"),
            browser="/bin/true", settle=0, load_timeout=1,
        )
    assert not out.exists()
