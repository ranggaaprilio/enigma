"""Plan 7a Task 7: the OpenSandbox security suite, against a real opensandbox-server.

Opt-in (``OPENSANDBOX_INTEGRATION_TESTS=1``) with a server started from
``deploy/opensandbox/sandbox.toml`` and ``OPENSANDBOX_API_URL`` / ``OPENSANDBOX_API_KEY`` set;
the image is ``OPENSANDBOX_SANDBOX_IMAGE`` (default ``enigma-sandbox:latest``) and the database
must be migrated (the installation id). Passing it against the reference config is the gate for
making OpenSandbox the self-host default (Plan 7b). See docs/self-host-opensandbox.md.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import httpx
import pytest

from app.core.config import Settings
from app.models import Workspace
from app.services.sandbox.base import ProvisioningError, PtySize, SandboxNotFoundError
from app.services.sandbox.installation import INSTALLATION_LABEL
from app.services.sandbox.opensandbox import (
    EXECD_PORT,
    EXECD_TOKEN_HEADER,
    FLOOR_PROBE,
    WORKSPACE_LABEL,
    OpenSandboxRuntime,
    assert_safe_create_kwargs,
    build_create_kwargs,
    execd_headers,
    floor_violations,
)
from app.services.sandbox_maintenance import reconcile_opensandbox_orphans
from tests.test_sandbox_maintenance import maker  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.getenv("OPENSANDBOX_INTEGRATION_TESTS") != "1",
    reason="Set OPENSANDBOX_INTEGRATION_TESTS=1 to run against a real opensandbox-server",
)

API_URL = os.getenv("OPENSANDBOX_API_URL", "http://127.0.0.1:8080")
MEMORY = "2Gi"


def _settings(**overrides) -> Settings:
    base = dict(
        opensandbox_api_url=API_URL,
        opensandbox_api_key=os.getenv("OPENSANDBOX_API_KEY"),
        opensandbox_execd_token_key="integration-" + "k" * 40,
        opensandbox_sandbox_image=os.getenv("OPENSANDBOX_SANDBOX_IMAGE", "enigma-sandbox:latest"),
        opensandbox_sandbox_cpu="1",
        opensandbox_sandbox_memory=MEMORY,
        sandbox_network_block_all=False,
        sandbox_network_allow_list=[],
    )
    return Settings(**{**base, **overrides})


def _workspace() -> Workspace:
    return Workspace(id=uuid4(), sandbox_id=None, workspace_path="workspace", runtime="opensandbox")


@pytest.fixture
async def made():
    """Every (runtime, workspace) a test provisions; all destroyed afterwards."""
    created: list[tuple[OpenSandboxRuntime, Workspace]] = []
    yield created
    for runtime, workspace in created:
        try:
            await runtime.delete(workspace)
        except Exception:  # noqa: BLE001 -- best effort; a sweep test may have removed it
            pass
    for runtime in {id(r): r for r, _ in created}.values():
        await runtime.close()


async def _provision(made, settings: Settings | None = None, *, installation: str | None = None):
    runtime = OpenSandboxRuntime(settings or _settings())
    if installation is not None:
        runtime._installation = installation
    workspace = _workspace()
    workspace.sandbox_id = await runtime.provision(workspace)
    made.append((runtime, workspace))
    return runtime, workspace


async def _sh(runtime, workspace, command: str, timeout: int = 60) -> str:
    # A bare sandbox has no checkout yet (workspace init creates it), so run from /tmp.
    result = await runtime.exec(workspace, command, cwd="/tmp", timeout=timeout)
    return (result.stdout or "").strip()


async def _execd_base(runtime, workspace) -> str:
    sandbox = await runtime._get_sandbox(workspace)
    endpoint = str((await sandbox.get_endpoint(EXECD_PORT)).endpoint)
    return (endpoint if "://" in endpoint else f"http://{endpoint}").rstrip("/")


# 1 -------------------------------------------------------------------------------------------


async def test_a_sandbox_is_created_at_the_floor_with_its_settings(made) -> None:
    runtime, workspace = await _provision(made)

    assert floor_violations(await _sh(runtime, workspace, FLOOR_PROBE)) == []
    assert await _sh(runtime, workspace, "cat /sys/fs/cgroup/memory.max") == str(2 * 1024**3)
    info = await (await runtime._get_manager()).get_sandbox_info(workspace.sandbox_id)
    assert info.metadata[INSTALLATION_LABEL] == await runtime._installation_id()
    assert info.metadata[WORKSPACE_LABEL] == str(workspace.id)


# 2 + 3 ---------------------------------------------------------------------------------------


async def test_a_sibling_sandbox_cannot_reach_another_ones_execd(made) -> None:
    runtime, a = await _provision(made)
    _, b = await _provision(made)
    b_ip = (await _sh(runtime, b, "hostname -i")).split()[0]

    code = await _sh(
        runtime,
        a,
        f"curl -s -m 5 -o /dev/null -w '%{{http_code}}' "
        f"'http://{b_ip}:{EXECD_PORT}/files/info?path=/etc/hostname'; true",
    )

    assert code != "200", f"sandbox A reached sandbox B's execd ({code})"


async def test_unauthenticated_calls_are_refused(made) -> None:
    runtime, workspace = await _provision(made)
    base = await _execd_base(runtime, workspace)

    async with httpx.AsyncClient(timeout=10) as client:
        api = await client.get(f"{API_URL.rstrip('/')}/v1/sandboxes")
        execd_without = await client.get(f"{base}/files/info", params={"path": "/etc/hostname"})
        execd_with = await client.get(
            f"{base}/files/info",
            params={"path": "/etc/hostname"},
            headers=execd_headers(runtime.settings, workspace),
        )

    assert api.status_code == 401
    assert execd_without.status_code == 401
    assert execd_with.status_code == 200


async def test_a_preview_answers_only_with_the_sandbox_token(made) -> None:
    runtime, workspace = await _provision(made)
    await _sh(
        runtime,
        workspace,
        "nohup python3 -m http.server 8000 --bind 0.0.0.0 >/dev/null 2>&1 & sleep 2; echo up",
    )
    link = await runtime.preview_link(workspace, 8000)

    async with httpx.AsyncClient(timeout=10) as client:
        with_token = await client.get(link.url, headers=link.headers)
        without = await client.get(link.url)

    assert EXECD_TOKEN_HEADER in link.headers
    assert with_token.status_code == 200
    assert without.status_code == 401


# 4 -------------------------------------------------------------------------------------------

CURL = "curl -s -m 8 -o /dev/null -w '%{http_code}' "


async def test_egress_follows_the_allow_list_and_never_reaches_private_ranges(made) -> None:
    runtime, open_ws = await _provision(made)
    listed_runtime, listed_ws = await _provision(
        made, _settings(sandbox_network_allow_list=["1.1.1.1/32"])
    )

    assert await _sh(runtime, open_ws, CURL + "https://1.1.1.1; true") not in ("", "000")
    for private in ("http://10.0.0.1", "http://169.254.169.254", "http://192.168.1.1"):
        assert await _sh(runtime, open_ws, CURL + f"{private}; true") == "000", private

    assert await _sh(listed_runtime, listed_ws, CURL + "https://1.1.1.1; true") not in ("", "000")
    assert await _sh(listed_runtime, listed_ws, CURL + "https://8.8.8.8; true") == "000"


# 5 -------------------------------------------------------------------------------------------


async def test_a_terminal_survives_a_reconnect(made) -> None:
    runtime, workspace = await _provision(made)
    seen: list[bytes] = []

    async def on_data(data: bytes) -> None:
        seen.append(data)

    async def wait_for(marker: bytes) -> None:
        for _ in range(100):
            if marker in b"".join(seen):
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"{marker!r} never arrived: {b''.join(seen)[-300:]!r}")

    handle = await runtime.create_or_connect_pty(
        workspace, "it-term", on_data, env=None, cols=80, rows=24
    )
    await handle.send_input("export ENIGMA_MARK=pty-$((40+2)); echo $ENIGMA_MARK\n")
    await wait_for(b"pty-42")
    await handle.resize(PtySize(cols=120, rows=40))
    await handle.disconnect()

    # A reconnect (a page reload) attaches to the same shell: its state is still there. It
    # resumes from the last output offset, so only new output arrives.
    seen.clear()
    handle = await runtime.create_or_connect_pty(
        workspace, "it-term", on_data, env=None, cols=120, rows=40
    )
    await handle.send_input("echo again-$ENIGMA_MARK\n")
    await wait_for(b"again-pty-42")
    await handle.disconnect()
    await runtime.kill_pty(workspace, "it-term")


# 6 -------------------------------------------------------------------------------------------


async def test_orphans_are_swept_only_for_this_installation(made, maker) -> None:  # noqa: F811
    runtime, orphan = await _provision(made)  # no workspace row: an orphan
    other_runtime, foreign = await _provision(
        made, installation=f"other-installation-{uuid4().hex[:8]}"
    )

    removed = await reconcile_opensandbox_orphans(maker, runtime, only={orphan.id, foreign.id})

    assert removed == 1
    with pytest.raises(SandboxNotFoundError):
        await runtime.status(orphan)
    assert await other_runtime.status(foreign) == "ready"  # another installation's: untouched


async def test_an_idle_pause_is_undone_by_the_next_use(made) -> None:
    runtime, workspace = await _provision(made)

    assert await runtime.pause_if_running(workspace) is True
    assert await runtime.status(workspace) == "stopped"
    assert await _sh(runtime, workspace, "echo awake") == "awake"
    assert await runtime.status(workspace) == "ready"


# 7 -------------------------------------------------------------------------------------------


def test_an_unsafe_create_request_is_refused() -> None:
    settings = _settings()
    kwargs = build_create_kwargs(settings, _workspace(), "installation")
    for unsafe in (
        {"extensions": {"bootstrap.execd.isolation": "enable"}},
        {"volumes": [{"name": "any", "pvc": {"claimName": "host-volume"}}]},
    ):
        with pytest.raises(ProvisioningError):
            assert_safe_create_kwargs({**kwargs, **unsafe}, settings)
