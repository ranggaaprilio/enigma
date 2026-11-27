"""Capture a screenshot of a sandboxed app, inside that sandbox (Plan 6).

Tenant pages are untrusted, so they are rendered only by the Chromium the sandbox image
ships (``/usr/bin/chromium``), never in the control plane, and the app is reached on its own
loopback (``127.0.0.1:<port>``). Binaries are called by absolute path because the tenant
owns the front of ``PATH`` (``~/.local/bin``); the image goes to a fresh ``mktemp -d``
directory, is read back through the runtime's filesystem, validated as PNG and capped.
``--no-sandbox`` is required: the container already drops every capability, so Chromium's
own namespace sandbox cannot start -- the container is the sandbox.

Chromium is driven over the DevTools protocol by ``cdp_capture.py`` (sent as source and run
by the image's ``/usr/bin/python3``), not by its ``--screenshot`` flag: that flag captures
on ``load``, before a client-rendered page has hydrated and revealed its content.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import structlog

from app.models import Workspace
from app.services.sandbox import SandboxRuntime

logger = structlog.get_logger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 2_000_000
CAPTURE_TIMEOUT_SECONDS = 45
# The in-sandbox driver, sent as source: it is never imported by the backend.
_DRIVER_SOURCE = Path(__file__).with_name("cdp_capture.py").read_text()
_ABSOLUTE_DIR = re.compile(r"^/[A-Za-z0-9._/-]+$")


class ScreenshotError(Exception):
    """No usable screenshot could be taken (the reason is safe to show the reviewer)."""


def capture_command(port: int) -> str:
    # The image's own Python runs the driver isolated (-I -S): no PYTHON* env, no user or
    # site packages and no cwd on sys.path, so nothing the tenant wrote can be imported.
    return (
        'd=$(/usr/bin/mktemp -d) && '
        f"/usr/bin/timeout -k 5 {CAPTURE_TIMEOUT_SECONDS} /usr/bin/python3 -I -S -c "
        f"{shlex.quote(_DRIVER_SOURCE)} "
        f'http://127.0.0.1:{int(port)}/ "$d/shot.png" "$d/profile" >/dev/null 2>&1; echo "$d"; '
        # The size, measured in the sandbox, so an oversize file is never downloaded.
        '/usr/bin/stat -L -c %s -- "$d/shot.png" 2>/dev/null || echo 0'
    )


async def capture_screenshot(runtime: SandboxRuntime, workspace: Workspace, port: int) -> bytes:
    """PNG bytes of ``http://127.0.0.1:<port>/`` rendered in the sandbox, or ``ScreenshotError``."""
    try:
        result = await runtime.exec(
            workspace, capture_command(port), timeout=CAPTURE_TIMEOUT_SECONDS + 15
        )
    except Exception as error:
        logger.warning("screenshot_capture_failed", error_type=type(error).__name__)
        raise ScreenshotError("the sandbox could not run the browser") from None
    lines = [line.strip() for line in (result.stdout or "").strip().splitlines()]
    directory = lines[0] if lines else ""
    if not _ABSOLUTE_DIR.match(directory) or ".." in directory:
        raise ScreenshotError("the browser could not be started in the sandbox")
    try:
        size = int(lines[1]) if len(lines) > 1 else 0
        if result.exit_code != 0 or size <= 0:
            raise ScreenshotError("the browser produced no screenshot")
        if size > MAX_SCREENSHOT_BYTES:
            raise ScreenshotError("the screenshot is larger than the 2 MB limit")
        try:
            fs = await runtime.fs(workspace)
            data = await fs.download_file(f"{directory}/shot.png")
        except Exception:
            raise ScreenshotError("the browser produced no screenshot") from None
    except ValueError:
        raise ScreenshotError("the browser produced no screenshot") from None
    finally:
        try:
            await runtime.exec(workspace, f"/bin/rm -rf -- {shlex.quote(directory)}", timeout=30)
        except Exception:
            logger.warning("screenshot_cleanup_failed")

    if not data.startswith(PNG_SIGNATURE):
        raise ScreenshotError("the browser output is not a PNG image")
    if len(data) > MAX_SCREENSHOT_BYTES:  # the file may have changed since it was measured
        raise ScreenshotError("the screenshot is larger than the 2 MB limit")
    return data
