"""Screenshot a page with the sandbox's own Chromium over the DevTools protocol.

This file is not imported by the backend: ``screenshot.capture_command`` sends its source
to the tenant's sandbox and runs it there with the image's ``/usr/bin/python3 -I -S``.
It must therefore use the standard library only and nothing from ``app``.

Chromium's ``--screenshot`` flag captures as soon as ``load`` fires, which is before a
client-rendered page has hydrated and run its entrance animations: the evidence showed an
empty hero. This driver navigates, waits for ``load`` (bounded, because a third-party asset
can hold it for a long time), then gives the page ``settle`` seconds of real time before
capturing the 1280x800 viewport.

Usage (in the sandbox): ``cdp_capture.py URL OUT_PNG PROFILE_DIR``
"""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import subprocess
import sys
import time

WIDTH, HEIGHT = 1280, 800
SETTLE_SECONDS = 2.0
LOAD_TIMEOUT_SECONDS = 20.0
DEADLINE_SECONDS = 40.0


class _Pipe:
    """The browser end of ``--remote-debugging-pipe``: NUL-delimited JSON on fds 3 and 4."""

    def __init__(self, browser: str, profile_dir: str) -> None:
        to_r, self._to_w = os.pipe()  # we write commands, the browser reads fd 3
        self._from_r, from_w = os.pipe()  # the browser writes fd 4, we read

        def child_fds() -> None:
            # os.pipe() fds are close-on-exec; dup away first so dup2 onto 3/4 clears it
            # even when a pipe end already has that number.
            read_end, write_end = os.dup(to_r), os.dup(from_w)
            os.dup2(read_end, 3)
            os.dup2(write_end, 4)

        self.process = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--no-sandbox",  # the container is the sandbox; it drops every capability
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                f"--window-size={WIDTH},{HEIGHT}",
                f"--user-data-dir={profile_dir}",
                "--remote-debugging-pipe",
                "about:blank",
            ],
            preexec_fn=child_fds,
            close_fds=False,
            start_new_session=True,  # its renderers and helpers die with the group
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(to_r)
        os.close(from_w)
        self._buffer = b""
        self._next_id = 0
        self._events: list[dict] = []
        self.deadline = time.monotonic() + DEADLINE_SECONDS

    def _read(self, until: float) -> dict | None:
        while b"\0" not in self._buffer:
            left = until - time.monotonic()
            if left <= 0 or not select.select([self._from_r], [], [], left)[0]:
                return None
            chunk = os.read(self._from_r, 1 << 20)
            if not chunk:
                raise EOFError("the browser closed its pipe")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(raw)

    def call(self, method: str, params: dict | None = None, session: str | None = None):
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self._to_w, json.dumps(message).encode() + b"\0")
        while True:
            reply = self._read(self.deadline)
            if reply is None:
                raise TimeoutError(method)
            if reply.get("id") == self._next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method} failed")
                return reply.get("result", {})
            self._events.append(reply)

    def wait_for(self, event: str, timeout: float) -> bool:
        if any(seen.get("method") == event for seen in self._events):
            return True
        until = min(self.deadline, time.monotonic() + timeout)
        while (message := self._read(until)) is not None:
            if message.get("method") == event:
                return True
        return False

    def close(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.process.wait()


def capture(
    url: str,
    out: str,
    profile_dir: str,
    *,
    browser: str = "/usr/bin/chromium",
    settle: float = SETTLE_SECONDS,
    load_timeout: float = LOAD_TIMEOUT_SECONDS,
) -> None:
    """Write a PNG of ``url``'s viewport to ``out``, or raise."""
    pipe = _Pipe(browser, profile_dir)
    try:
        targets = pipe.call("Target.getTargets")["targetInfos"]
        target = next(info["targetId"] for info in targets if info.get("type") == "page")
        session = pipe.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]
        pipe.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1, "mobile": False},
            session,
        )
        pipe.call("Page.enable", session=session)
        pipe.call("Page.navigate", {"url": url}, session)
        pipe.wait_for("Page.loadEventFired", load_timeout)
        time.sleep(settle)  # real time: hydration, fetches and entrance animations run
        data = pipe.call("Page.captureScreenshot", {"format": "png"}, session)["data"]
        with open(out, "wb") as handle:
            handle.write(base64.b64decode(data))
    finally:
        pipe.close()


def _terminate(_signum, _frame) -> None:
    raise SystemExit(1)  # `timeout` sends SIGTERM; unwind so the browser is killed too


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate)
    capture(sys.argv[1], sys.argv[2], sys.argv[3])
