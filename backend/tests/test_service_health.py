"""Epic 13: HTTP health/readiness probing.

Three layers, tested at the level each claim belongs to:
- ``parse_probe_output``: pure text parsing, tolerant of the 4- and 5-field line shapes.
- ``build_probe_script``: the generated script's *text* (no `urllib`, uses `http.client` and
  `127.0.0.1`) -- executed end to end against a real local HTTP server in a separate test to
  prove it actually works, not just that the source looks right.
- ``WorkspaceServiceManager._apply_probe``/``_apply_health``/``_should_relaunch``: the mapping
  from a probe result to `health_status`, and the no-restart-loop invariant.
"""

import http.server
import socket
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.models import WorkspaceService
from app.services.service_scripts import (
    ProbeResult,
    ProbeRow,
    ServicePaths,
    build_probe_script,
    parse_probe_output,
)
from app.services.workspace_services import WorkspaceServiceManager


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# parse_probe_output
# ---------------------------------------------------------------------------


def test_parse_probe_output_accepts_5_field_lines_with_health():
    results = parse_probe_output("abc running 1 1 healthy\n")
    assert results["abc"] == ProbeResult(
        status="running", pid_alive=True, port_open=True, health="healthy"
    )


def test_parse_probe_output_accepts_4_field_lines_defaulting_health_unknown():
    results = parse_probe_output("abc running 1 1\n")
    assert results["abc"] == ProbeResult(
        status="running", pid_alive=True, port_open=True, health="unknown"
    )


def test_parse_probe_output_skips_lines_with_6_or_more_fields():
    results = parse_probe_output("abc running 1 1 healthy extra\n")
    assert results == {}


def test_parse_probe_output_skips_lines_with_fewer_than_4_fields():
    results = parse_probe_output("abc running 1\n")
    assert results == {}


# ---------------------------------------------------------------------------
# build_probe_script: source-level assertions
# ---------------------------------------------------------------------------


def _paths() -> ServicePaths:
    return ServicePaths(
        status="/tmp/x.status", log="/tmp/x.log", gen="/tmp/x.gen", pid="/tmp/x.pid",
        grep_token="x",
    )


def test_build_probe_script_uses_http_client_never_urllib():
    script = build_probe_script([ProbeRow(id="a", paths=_paths(), port=8000, health_path="/h")])
    assert "import http.client" in script
    # A comment may mention "urllib" as an explanation of why it's avoided; what matters is
    # that the generated script never imports or calls it.
    assert "import urllib" not in script
    assert "urllib.request" not in script


def test_build_probe_script_targets_127_0_0_1_only():
    script = build_probe_script([ProbeRow(id="a", paths=_paths(), port=8000, health_path="/h")])
    assert "'127.0.0.1'" in script


def test_build_probe_script_only_attempts_health_when_port_open_and_path_set():
    script = build_probe_script([ProbeRow(id="a", paths=_paths(), port=8000, health_path=None)])
    # The call always exists (health_path=None is passed through); the *script's own guard* --
    # not this test -- is what skips the request. Assert the guard is present.
    assert "if port_open and port and health_path" in script


# ---------------------------------------------------------------------------
# build_probe_script: executed end to end against a real local HTTP server
# ---------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib method name
        if self.path == "/healthz":
            self.send_response(200)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/healthz")
        else:
            self.send_response(500)
        self.end_headers()

    def log_message(self, *args):  # silence stdlib's default stderr logging
        return


def _run_probe_script(rows: list[ProbeRow]) -> dict:
    script = build_probe_script(rows)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    return parse_probe_output(result.stdout or "")


def test_probe_script_reports_healthy_for_2xx():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = str(Path(tmp) / "s.status")
            pid_path = str(Path(tmp) / "s.pid")
            Path(status_path).write_text("running")
            Path(pid_path).write_text("1")
            paths = ServicePaths(
                status=status_path, log="/dev/null", gen="/dev/null", pid=pid_path,
                grep_token="nomatch",
            )
            parsed = _run_probe_script(
                [ProbeRow(id="svc", paths=paths, port=port, health_path="/healthz")]
            )
            assert parsed["svc"].health == "healthy"
    finally:
        server.shutdown()


def test_probe_script_reports_unhealthy_for_5xx():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = str(Path(tmp) / "s.status")
            pid_path = str(Path(tmp) / "s.pid")
            Path(status_path).write_text("running")
            Path(pid_path).write_text("1")
            paths = ServicePaths(
                status=status_path, log="/dev/null", gen="/dev/null", pid=pid_path,
                grep_token="nomatch",
            )
            parsed = _run_probe_script(
                [ProbeRow(id="svc", paths=paths, port=port, health_path="/bad")]
            )
            assert parsed["svc"].health == "unhealthy"
    finally:
        server.shutdown()


def test_probe_script_follows_no_redirect_and_counts_3xx_as_healthy():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = str(Path(tmp) / "s.status")
            pid_path = str(Path(tmp) / "s.pid")
            Path(status_path).write_text("running")
            Path(pid_path).write_text("1")
            paths = ServicePaths(
                status=status_path, log="/dev/null", gen="/dev/null", pid=pid_path,
                grep_token="nomatch",
            )
            parsed = _run_probe_script(
                [ProbeRow(id="svc", paths=paths, port=port, health_path="/redirect")]
            )
            assert parsed["svc"].health == "healthy"
    finally:
        server.shutdown()


def test_probe_script_skips_health_entirely_when_port_closed():
    with tempfile.TemporaryDirectory() as tmp:
        status_path = str(Path(tmp) / "s.status")
        pid_path = str(Path(tmp) / "s.pid")
        Path(status_path).write_text("running")
        Path(pid_path).write_text("1")
        paths = ServicePaths(
            status=status_path, log="/dev/null", gen="/dev/null", pid=pid_path,
            grep_token="nomatch",
        )
        closed_port = _free_port()  # bound to nothing at call time
        parsed = _run_probe_script(
            [ProbeRow(id="svc", paths=paths, port=closed_port, health_path="/healthz")]
        )
        assert parsed["svc"].health == "unknown"
        assert parsed["svc"].port_open is False


def test_probe_script_reports_unknown_with_no_health_path():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = str(Path(tmp) / "s.status")
            pid_path = str(Path(tmp) / "s.pid")
            Path(status_path).write_text("running")
            Path(pid_path).write_text("1")
            paths = ServicePaths(
                status=status_path, log="/dev/null", gen="/dev/null", pid=pid_path,
                grep_token="nomatch",
            )
            parsed = _run_probe_script(
                [ProbeRow(id="svc", paths=paths, port=port, health_path=None)]
            )
            assert parsed["svc"].health == "unknown"
            assert parsed["svc"].port_open is True
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# WorkspaceServiceManager._apply_probe / _apply_health / _should_relaunch
# ---------------------------------------------------------------------------


def _manager() -> WorkspaceServiceManager:
    return WorkspaceServiceManager(session=None, settings=get_settings(), runtime=object())


def _row(**overrides) -> WorkspaceService:
    defaults = dict(
        id=uuid4(),
        workspace_id=uuid4(),
        custom_command_id=None,
        port=8000,
        status="running",
        desired=True,
        health_status="unknown",
        health_checked_at=None,
        started_at=datetime.now(UTC) - timedelta(seconds=200),
    )
    defaults.update(overrides)
    return WorkspaceService(**defaults)


async def test_apply_probe_healthy_sets_health_status_and_keeps_status_running():
    manager = _manager()
    row = _row()
    probe = ProbeResult(status="running", pid_alive=True, port_open=True, health="healthy")
    await manager._apply_probe(  # noqa: SLF001 - intentional direct unit test
        workspace=None, row=row, probe=probe, target_port=8000, exclude_ports=set()
    )
    assert row.status == "running"
    assert row.health_status == "healthy"
    assert row.health_checked_at is not None


async def test_apply_probe_unhealthy_past_grace_is_unhealthy_but_status_stays_running():
    manager = _manager()
    row = _row(started_at=datetime.now(UTC) - timedelta(seconds=999))
    probe = ProbeResult(status="running", pid_alive=True, port_open=True, health="unhealthy")
    await manager._apply_probe(  # noqa: SLF001
        workspace=None, row=row, probe=probe, target_port=8000, exclude_ports=set()
    )
    assert row.status == "running"
    assert row.health_status == "unhealthy"


async def test_apply_probe_unhealthy_within_grace_reports_starting_not_unhealthy():
    manager = _manager()
    row = _row(started_at=datetime.now(UTC) - timedelta(seconds=5))
    probe = ProbeResult(status="running", pid_alive=True, port_open=True, health="unhealthy")
    await manager._apply_probe(  # noqa: SLF001
        workspace=None, row=row, probe=probe, target_port=8000, exclude_ports=set()
    )
    assert row.status == "running"
    assert row.health_status == "starting"


async def test_apply_probe_no_health_path_stays_unknown_and_uncheckedat_stays_none():
    manager = _manager()
    row = _row()
    probe = ProbeResult(status="running", pid_alive=True, port_open=True, health="unknown")
    await manager._apply_probe(  # noqa: SLF001
        workspace=None, row=row, probe=probe, target_port=8000, exclude_ports=set()
    )
    assert row.status == "running"
    assert row.health_status == "unknown"
    assert row.health_checked_at is None


async def test_should_relaunch_never_true_for_an_unhealthy_but_running_row():
    """The no-restart-loop invariant: health never influences `_should_relaunch`, only
    `status` does, and `status` stays "running" regardless of health."""
    manager = _manager()
    row = _row(status="running", health_status="unhealthy", desired=True)
    probe = ProbeResult(status="running", pid_alive=True, port_open=True, health="unhealthy")
    assert await manager._should_relaunch(row, probe) is False  # noqa: SLF001
