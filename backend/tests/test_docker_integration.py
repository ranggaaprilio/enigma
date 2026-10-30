"""Docker provider against a real daemon (Docker or Podman) and the built sandbox image.

Run with a daemon up and ``./sandbox/build-local-image.sh`` done:

    DOCKER_INTEGRATION_TESTS=1 uv run pytest tests/test_docker_integration.py

Covers the local-docker-runtime acceptance criteria that need a real container.
Epic 02: lifecycle, exec/stream parity, timeout and cancellation kill, auto-start, a
command run through ``CommandService`` and an agent ``run_command``, no leaked
containers. Epic 03: files (unusual names, modes, ownership, symlinks), the agent file
tools, blank/template init, worktrees, ``specify init``, ``.env`` and SSH key ``0600``,
the git token kept off every command line, and tmux terminals that survive a reconnect.
Epic 04: previews over the sandbox network (a second sandbox stands in for the preview
proxy and a third for a sibling tenant), the proxy-only loopback relay with a real Vite
dev server and its HMR WebSocket, code-server on loopback, two registry services with
their env, logs and previews, and no host-published ports.
Epic 05: sibling isolation, ``block_all`` egress (with the agent tools still working offline),
pids/OOM limits, and the idle reaper against real rows: an attached terminal spares a sandbox
until the busy cap, and a terminal wakes it again.
Follow-ups: the egress allow-list through ``app/egress_proxy.py`` and the per-sandbox disk
quota (skipped where the storage driver has none).
The template, ``specify init``, Vite and allow-list tests need outbound network from the
sandbox.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.models import User, Workspace
from app.services.agent_tools import WorkspaceTools
from app.services.commands import CommandRuntime, CommandService
from app.services.repositories import GitAuth, RepositoryService
from app.services.sandbox import CommandTimeoutError, PtySize
from app.services.sandbox.docker import MANAGED_LABEL, WORKSPACE_LABEL, DockerRuntime
from app.services.sandbox_maintenance import stop_idle_sandboxes
from app.services.speckit import SPECIFY_INIT_COMMAND
from app.services.workspace_env import materialize_env_file
from app.services.workspace_init import WorkspaceInitializer
from app.services.workspaces import WorkspaceSecretCipher
from app.services.worktrees import WorktreeService
from tests.test_commands import real_db_session  # noqa: F401  (fixture)
from tests.test_sandbox_maintenance import _sandboxed, maker  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.getenv("DOCKER_INTEGRATION_TESTS") != "1",
    reason="Set DOCKER_INTEGRATION_TESTS=1 to run against a Docker/Podman daemon",
)

PS_SLEEPERS = "ps -eo args | grep -E '^sleep 3[0-9]{2}$' || echo none"


def _workspace(owner_id=None) -> Workspace:
    return Workspace(
        id=uuid4(),
        owner_id=owner_id or uuid4(),
        name="Docker integration",
        slug=f"docker-it-{uuid4().hex[:8]}",
        repo_url=None,
        default_branch="main",
        runtime="docker",
        workspace_path="workspace",
        status="creating",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
async def runtime():
    runtime = DockerRuntime(Settings(docker_sandbox_memory_mb=1024, docker_sandbox_cpus=1))
    if await runtime.health() != "ok":
        pytest.fail("Docker daemon is not reachable")
    yield runtime
    await runtime.close()


@pytest.fixture
async def sandbox(runtime):
    workspace = _workspace()
    workspace.sandbox_id = await runtime.provision(workspace)
    yield workspace
    await runtime.delete(workspace)
    client, _ = await runtime._clients()
    leaked = await asyncio.to_thread(
        client.containers.list,
        all=True,
        filters={"label": [f"{MANAGED_LABEL}=1", f"{WORKSPACE_LABEL}={workspace.id}"]},
    )
    assert leaked == []


async def test_lifecycle_exec_stream_and_auto_start(runtime, sandbox) -> None:
    assert await runtime.provision(sandbox) == sandbox.sandbox_id  # idempotent by name
    assert await runtime.status(sandbox) == "ready"

    result = await runtime.exec(sandbox, "echo hello; echo oops >&2; pwd; exit 4")
    assert result.exit_code == 4
    # Combined like Daytona's: everything in stdout, stderr empty. stdout and stderr are
    # separate pipes, so their relative interleaving is not guaranteed; each stays ordered.
    assert result.stderr == ""
    assert sorted(result.stdout.splitlines()) == ["/home/daytona/workspace", "hello", "oops"]
    assert result.stdout.index("hello") < result.stdout.index("/home/daytona/workspace")

    tools = await runtime.exec(
        sandbox, "for b in mise code-server specify tmux git ssh; do command -v $b; done"
    )
    assert tools.exit_code == 0, tools.stdout

    out: list[str] = []
    err: list[str] = []

    async def on_out(text: str) -> None:
        out.append(text)

    async def on_err(text: str) -> None:
        err.append(text)

    code = await runtime.stream_command(
        sandbox,
        "it-stream",
        "for i in 1 2 3; do echo line$i; sleep 0.2; done; "
        "printf '\\342\\202'; sleep 0.2; printf '\\254\\n'; echo bad >&2; exit 2",
        cwd=None,
        env={"GREETING": "hi there"},
        timeout=30,
        on_stdout=on_out,
        on_stderr=on_err,
    )
    assert code == 2
    assert "".join(out) == "line1\nline2\nline3\n€\n"
    assert "".join(err) == "bad\n"

    client, _ = await runtime._clients()
    container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)
    await asyncio.to_thread(container.stop, timeout=1)
    assert await runtime.status(sandbox) == "stopped"
    assert (await runtime.exec(sandbox, "echo back")).stdout == "back\n"  # auto-started
    assert await runtime.status(sandbox) == "ready"


async def test_timeout_and_cancellation_leave_no_process_behind(runtime, sandbox) -> None:
    with pytest.raises(CommandTimeoutError):
        await runtime.exec(sandbox, "sleep 301 & sleep 302", timeout=2)
    assert (await runtime.exec(sandbox, PS_SLEEPERS)).stdout == "none\n"

    async def sink(text: str) -> None:
        return None

    with pytest.raises(CommandTimeoutError):
        await runtime.stream_command(
            sandbox,
            "it-timeout",
            "echo start; sleep 303",
            cwd=None,
            env=None,
            timeout=2,
            on_stdout=sink,
            on_stderr=sink,
        )
    assert (await runtime.exec(sandbox, PS_SLEEPERS)).stdout == "none\n"

    task = asyncio.create_task(runtime.exec(sandbox, "sleep 304 & sleep 305"))
    await asyncio.sleep(1.5)
    loop = asyncio.get_running_loop()
    cancelled_at = loop.time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The killer exits as soon as the group is gone instead of always waiting the grace.
    assert loop.time() - cancelled_at < 3
    assert (await runtime.exec(sandbox, PS_SLEEPERS)).stdout == "none\n"


async def test_sandbox_floor_is_enforced_on_the_real_container(runtime, sandbox) -> None:
    whoami = await runtime.exec(
        sandbox, "id -un; sudo -n true 2>/dev/null && echo ROOT || echo no-root"
    )
    assert whoami.stdout == "daytona\nno-root\n"

    client, _ = await runtime._clients()
    container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)
    host_config = container.attrs["HostConfig"]
    assert container.attrs["Mounts"] == []
    assert not host_config.get("Privileged")
    assert host_config.get("PidsLimit") == 4096
    effective = await runtime.exec(sandbox, "grep -E '^Cap(Eff|Bnd):' /proc/self/status")
    assert effective.stdout.split() == ["CapEff:", "0" * 16, "CapBnd:", "0" * 16]
    assert list(container.attrs["NetworkSettings"]["Networks"]) == [
        f"enigma-sbx-{sandbox.id.hex}"
    ]


async def test_agent_run_command_executes_in_the_container(runtime, sandbox) -> None:
    tools = WorkspaceTools(
        sandbox, runtime=runtime, command_timeout_seconds=30, max_command_output_chars=20_000
    )

    result = await tools.run_command("echo hello")

    assert (result.exit_code, result.stdout) == (0, "hello\n")


async def test_command_run_streams_and_completes_on_docker(runtime, real_db_session) -> None:  # noqa: F811
    session, session_maker = real_db_session
    user = User(
        id=uuid4(),
        email=f"docker-it-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = _workspace(user.id)
    workspace.sandbox_id = await runtime.provision(workspace)
    workspace.status = "ready"
    session.add(user)
    session.add(workspace)
    await session.flush()
    try:
        service = CommandService(
            session=session,
            session_maker=session_maker,
            settings=get_settings(),
            runtime=CommandRuntime(),
            sandbox_runtime=runtime,
        )
        command = await service.create_command(
            workspace_id=workspace.id,
            user_id=user.id,
            name="Multi-line",
            command='echo "one"; echo "two" >&2; echo "$ENIGMA_WORKSPACE_ID"; exit 0',
            description=None,
            icon=None,
            hotkey=None,
            requires_confirmation=False,
        )
        run = await service.create_run(workspace.id, command.id, user.id, confirmed=True)
        task = service.runtime.get_task(run.id)
        assert task is not None
        await task

        await session.refresh(run)
        assert run.status == "completed" and run.exit_code == 0
        assert "one\n" in run.output and "two\n" in run.output
        assert str(workspace.id) in run.output
    finally:
        await runtime.delete(workspace)


# --- Epic 03: files, git, terminal --------------------------------------------------------

ROOT = "/home/daytona/workspace"


async def _sh(runtime, workspace, command: str) -> str:
    result = await runtime.exec(workspace, command, cwd="/home/daytona")
    assert result.exit_code == 0, result.stdout
    return result.stdout


async def test_files_round_trip_with_modes_ownership_and_symlinks(runtime, sandbox) -> None:
    fs = await runtime.fs(sandbox)
    odd = f"{ROOT}/d ir/-x; rm `id` ✓\nz.txt"
    await fs.upload_file(b"hello\nneedle here\n", odd)  # parent created on demand

    assert [entry.name for entry in await fs.list_files(f"{ROOT}/d ir")] == [odd.rsplit("/")[-1]]
    info = await fs.get_file_info(odd)
    assert (info.is_dir, info.size, info.owner, info.permissions) == (
        False,
        len(b"hello\nneedle here\n"),
        "daytona",
        "0644",
    )
    assert await fs.download_file(odd) == b"hello\nneedle here\n"
    assert [(m.file, m.line) for m in await fs.find_files(ROOT, "needle")] == [(odd, 2)]

    script = f"{ROOT}/run.sh"
    await fs.upload_file(b"#!/bin/sh\necho one\n", script)
    await fs.set_file_permissions(script, mode="755")
    await fs.upload_file(b"#!/bin/sh\necho two\n", script)
    assert await _sh(runtime, sandbox, "stat -c '%a %U' workspace/run.sh; workspace/run.sh") == (
        "755 daytona\ntwo\n"
    )

    await _sh(runtime, sandbox, "ln -s /etc/hostname workspace/escape")
    assert (await fs.get_file_info(f"{ROOT}/escape")).is_symlink is True  # files.py refuses
    with pytest.raises(IsADirectoryError):
        await fs.download_file("/usr")  # refused up front, never streamed into memory

    blob = os.urandom(5 * 1024 * 1024) + bytes(range(256))  # binary, larger than a frame
    await fs.upload_file(blob, f"{ROOT}/blob.bin")
    assert await fs.download_file(f"{ROOT}/blob.bin") == blob
    await fs.upload_file(b"", f"{ROOT}/blob.bin")
    assert await fs.download_file(f"{ROOT}/blob.bin") == b""

    await fs.move_files(odd, f"{ROOT}/moved.txt")
    with pytest.raises(FileNotFoundError):
        await fs.get_file_info(odd)
    await fs.delete_file(f"{ROOT}/d ir")  # now empty
    await fs.delete_file(ROOT, recursive=True)
    with pytest.raises(FileNotFoundError):
        await fs.get_file_info(ROOT)


async def test_symlinks_never_lend_the_daemon_root_privileges(runtime, sandbox) -> None:
    """A repository can commit ``docs -> /etc``. Reads and writes run as the sandbox user,
    so the kernel refuses what that user may not do — even through a parent symlink."""
    fs = await runtime.fs(sandbox)
    await _sh(runtime, sandbox, "mkdir -p workspace && ln -s /etc workspace/docs")
    await _sh(runtime, sandbox, "ln -s /etc/shadow workspace/shadow")
    before = await _sh(runtime, sandbox, "ls -la /etc | md5sum; stat -c %U /etc/hostname")

    for target in (f"{ROOT}/docs/pwned", f"{ROOT}/docs/hostname"):
        with pytest.raises(PermissionError):
            await fs.upload_file(b"pwned", target)
    for target in (f"{ROOT}/shadow", f"{ROOT}/docs/shadow"):
        with pytest.raises(PermissionError):
            await fs.download_file(target)

    assert await _sh(runtime, sandbox, "ls -la /etc | md5sum; stat -c %U /etc/hostname") == before


async def test_agent_file_and_git_tools_on_a_blank_workspace(runtime, sandbox) -> None:
    await WorkspaceInitializer(runtime).initialize_blank(sandbox)
    assert "Initial commit" in await _sh(runtime, sandbox, "git -C workspace log --oneline")

    tools = WorkspaceTools(
        sandbox, runtime=runtime, command_timeout_seconds=30, max_command_output_chars=20_000
    )
    await tools.write_file("src/app.py", "print('hello')\n")
    patch = "<<<<<<< SEARCH\nprint('hello')\n=======\nprint('bye')\n>>>>>>> REPLACE\n"
    assert (await tools.apply_patch("src/app.py", patch)).blocks_applied == 1
    assert await tools.read_file("src/app.py") == "print('bye')\n"
    assert sorted(await tools.list_files()) == ["README.md", "src/app.py"]
    found = await tools.search_code("bye")
    assert [(m.path, m.line_number) for m in found.matches] == [("src/app.py", 1)]
    status = await tools.git_status()
    assert status.is_clean is False and status.branch == "main"
    await tools.write_file("README.md", "# changed\n")
    assert "+# changed" in await tools.git_diff()


async def test_template_init_and_specify_init(runtime, sandbox) -> None:
    template = SimpleNamespace(
        slug="react",
        source_repo_url="https://github.com/github/codespaces-react.git",
        source_ref=None,
        mise_config='[tools]\nnode = "22"\n',
    )
    await WorkspaceInitializer(runtime).initialize_from_template(sandbox, template)
    log = await _sh(runtime, sandbox, "git -C workspace log --oneline")
    assert log.count("\n") == 1 and "from template react" in log
    assert await _sh(runtime, sandbox, "cat workspace/mise.toml") == '[tools]\nnode = "22"\n'

    result = await runtime.exec(sandbox, SPECIFY_INIT_COMMAND, timeout=180)
    assert result.exit_code == 0, result.stdout[-2000:]
    await _sh(runtime, sandbox, "test -d workspace/.specify")


async def test_env_file_and_ssh_key_land_private(runtime, sandbox) -> None:
    await WorkspaceInitializer(runtime).initialize_blank(sandbox)
    sandbox.env_file_path = ".env"
    written = await materialize_env_file(
        runtime, sandbox, checkout="workspace", env={"API_KEY": "s3cr3t"}, timeout=30
    )
    stat = await _sh(runtime, sandbox, f"stat -c '%a %U' {written}; cat {written}")
    assert stat.startswith("600 daytona\n") and "s3cr3t" in stat

    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(get_settings().workspace_env_encryption_key),
        runtime=runtime,
    )
    fs = await runtime.fs(sandbox)
    async with service._ssh_identity(fs, "-----BEGIN KEY-----\nabc\n") as key_path:
        modes = await _sh(runtime, sandbox, f"stat -c '%a' {key_path} $(dirname {key_path})")
        assert modes == "600\n700\n"
    with pytest.raises(FileNotFoundError):
        await fs.get_file_info(key_path)


async def test_git_token_never_reaches_a_command_line(runtime, sandbox) -> None:
    await WorkspaceInitializer(runtime).initialize_blank(sandbox)
    service = RepositoryService(
        session=None,  # type: ignore[arg-type]
        cipher=WorkspaceSecretCipher(get_settings().workspace_env_encryption_key),
        runtime=runtime,
    )
    auth = GitAuth(kind="token", username="x-access-token", token="tok-NEVER-IN-PS")
    seen = await service._run_authenticated_git(
        sandbox, ["config", "--get", "http.extraHeader"], auth, cwd="workspace"
    )
    assert seen.stdout.startswith("AUTHORIZATION: basic ")  # git does receive it
    encoded = seen.stdout.split()[-1]

    slow = asyncio.create_task(
        service._run_authenticated_git(
            sandbox, ["-c", "alias.slow=!sleep 3", "slow"], auth, cwd="workspace"
        )
    )
    await asyncio.sleep(1.5)
    snapshot = await _sh(
        runtime, sandbox, "ps -eo args; for f in /proc/[0-9]*/cmdline; do tr '\\0' ' ' < $f; done"
    )
    assert (await slow).exit_code == 0
    assert "sleep 3" in snapshot  # the sample really saw the running git
    assert "tok-NEVER-IN-PS" not in snapshot and encoded not in snapshot


async def test_worktrees_on_docker(runtime, real_db_session) -> None:  # noqa: F811
    session, _ = real_db_session
    user = User(
        id=uuid4(),
        email=f"docker-it-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = _workspace(user.id)
    workspace.sandbox_id = await runtime.provision(workspace)
    workspace.status = "ready"
    session.add(user)
    session.add(workspace)
    await session.flush()
    try:
        await WorkspaceInitializer(runtime).initialize_blank(workspace)
        service = WorktreeService(session, runtime, get_settings())
        created = await service.create_worktree(workspace.id, user.id, "feature/it")
        listed = await service.list_worktrees(workspace.id, user.id)
        assert "feature/it" in [item.branch for item in listed]
        fs = await runtime.fs(workspace)
        path = await service.resolve_path(workspace, created.id)
        assert (await fs.get_file_info(f"{path}/README.md")).is_dir is False
        await service.remove_worktree(workspace.id, user.id, created.id)
        assert "feature/it" not in [
            i.branch for i in await service.list_worktrees(workspace.id, user.id)
        ]
    finally:
        await runtime.delete(workspace)


async def test_terminal_survives_a_reconnect_and_mirrors_tabs(runtime, sandbox) -> None:
    first: list[bytes] = []
    second: list[bytes] = []

    def sink(store: list[bytes]):
        async def on_data(data: bytes) -> None:
            store.append(data)

        return on_data

    async def until(store: list[bytes], text: bytes) -> None:
        for _ in range(100):
            if text in b"".join(store):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"{text!r} never arrived: {b''.join(store)[-300:]!r}")

    session_id = f"execute-{sandbox.id.hex}:{uuid4().hex}"  # a worktree terminal's id
    tab = await runtime.create_or_connect_pty(
        sandbox, session_id, sink(first), env={"GREETING": "hi there"}, cols=100, rows=30
    )
    await tab.send_input(b"echo marker-$GREETING; pwd; echo $TERM\n")
    await until(first, b"marker-hi there")
    await tab.resize(PtySize(cols=140, rows=40))
    await tab.send_input("tput cols\n")
    await until(first, b"140")

    mirror = await runtime.create_or_connect_pty(
        sandbox, session_id, sink(second), env=None, cols=140, rows=40
    )
    await tab.send_input(b"echo mirrored\n")
    await until(second, b"mirrored")  # two tabs on one session see the same shell
    await mirror.disconnect()

    await tab.disconnect()
    name = session_id.replace(":", "_")
    assert await _sh(runtime, sandbox, "tmux list-clients") == ""  # detached, not killed
    assert "marker-hi there" in await _sh(runtime, sandbox, f"tmux capture-pane -p -t ={name}:")

    first.clear()
    again = await runtime.create_or_connect_pty(
        sandbox, session_id, sink(first), env=None, cols=100, rows=30
    )
    await until(first, b"marker-hi there")  # reattached: tmux redraws the old screen
    await again.disconnect()

    await runtime.kill_pty(sandbox, session_id)
    await runtime.kill_pty(sandbox, session_id)
    gone = await runtime.exec(sandbox, f"tmux has-session -t ={name}")
    assert gone.exit_code != 0


# --- Epic 04: preview, editor and services over the sandbox network -----------------------

APP = "/home/daytona/app"


@pytest.fixture
async def peer(runtime):
    """A second sandbox on the same bridge, playing the preview proxy: the relay admits it."""
    from app.services.sandbox.docker import container_name

    workspace = _workspace()
    workspace.sandbox_id = await runtime.provision(workspace)
    # Play the proxy: the backend attaches containers carrying the proxy label to every
    # sandbox network under the proxy's name, and the relay admits that name only.
    runtime.settings.docker_preview_proxy_host = container_name(workspace)
    runtime.settings.docker_preview_proxy_label = f"{WORKSPACE_LABEL}={workspace.id}"
    await runtime.reconcile_networks()
    yield workspace
    await runtime.delete(workspace)


@pytest.fixture
async def stranger(runtime):
    """A third sandbox: another tenant on the same bridge, which the relay must refuse."""
    workspace = _workspace()
    workspace.sandbox_id = await runtime.provision(workspace)
    yield workspace
    await runtime.delete(workspace)


def _resolve_from(runtime, peer):
    """The sandbox network's DNS, asked from inside it (the test host is not on it)."""

    async def resolve(name: str) -> list[str]:
        result = await runtime.exec(peer, f"getent ahostsv4 {name} || true")
        return sorted({line.split()[0] for line in result.stdout.splitlines() if line.strip()})

    return resolve


async def _fetch(runtime, peer, url: str) -> tuple[str, str]:
    result = await runtime.exec(
        peer,
        f"curl -s -m 5 -o /tmp/body -w '%{{http_code}}' {url} 2>/dev/null; echo; "
        "cat /tmp/body 2>/dev/null",
    )
    code, _, body = result.stdout.partition("\n")
    return code.strip(), body


async def _wait_for(runtime, workspace, condition: str, seconds: int = 90) -> None:
    result = await runtime.exec(
        workspace,
        f"for i in $(seq 1 {seconds * 2}); do ({condition}) && exit 0; sleep 0.5; done; exit 1",
        timeout=seconds + 10,
    )
    assert result.exit_code == 0, f"timed out waiting for: {condition}"


def _listening(port: int) -> str:
    return f"grep -qi ':{port:04X} [0-9A-F:]* 0A' /proc/net/tcp /proc/net/tcp6"


NO_RELAY = "! ls /tmp/enigma-port-relay-*.pid >/dev/null 2>&1"


async def _relays(runtime, workspace) -> int:
    result = await runtime.exec(workspace, "ls /tmp/enigma-port-relay-*.pid 2>/dev/null | wc -l")
    return int(result.stdout.strip() or 0)


async def test_sibling_sandboxes_cannot_reach_each_other_but_the_proxy_can(
    runtime, sandbox, peer, stranger
) -> None:
    """Epic 05: each sandbox is on its own network. A server bound to 0.0.0.0 (no relay
    involved) is reachable from the proxy, which the backend attached, and from no other
    tenant's sandbox, by name or by address."""
    runtime._resolve_host = _resolve_from(runtime, peer)
    await _sh(
        runtime,
        sandbox,
        "(setsid nohup python3 -m http.server 8000 --bind 0.0.0.0 >/tmp/http.log 2>&1 &)",
    )
    await _wait_for(runtime, sandbox, _listening(8000))
    link = await runtime.preview_link(sandbox, 8000)

    assert (await _fetch(runtime, peer, link.url))[0] == "200"
    assert (await _fetch(runtime, stranger, link.url))[0] == "000"
    name_url = f"http://execute-{sandbox.id.hex}:8000/"
    assert (await _fetch(runtime, stranger, name_url))[0] == "000"
    lookup = await runtime.exec(stranger, f"getent hosts execute-{sandbox.id.hex}.")
    assert lookup.exit_code != 0  # not even resolvable from another tenant's network


async def test_sandbox_ports_are_never_published_to_the_host(runtime, sandbox) -> None:
    client, _ = await runtime._clients()
    container = await asyncio.to_thread(client.containers.get, sandbox.sandbox_id)
    host_config = container.attrs["HostConfig"]
    assert not host_config.get("PortBindings")
    assert not host_config.get("PublishAllPorts")
    assert not any((container.attrs["NetworkSettings"].get("Ports") or {}).values())


async def test_vite_preview_and_hmr_through_the_loopback_relay(
    runtime, sandbox, peer, stranger
) -> None:
    runtime._resolve_host = _resolve_from(runtime, peer)
    await _sh(
        runtime,
        sandbox,
        f"mkdir -p {APP} && cd {APP} && npm init -y >/dev/null && "
        "npm install --no-audit --no-fund vite >/tmp/npm.log 2>&1 && "
        "echo '<h1>enigma-vite</h1>' > index.html",
    )
    # Vite's default: loopback only, unreachable over the bridge until relayed.
    await _sh(
        runtime,
        sandbox,
        f"cd {APP} && (setsid nohup npx vite --port 5173 >/tmp/vite.log 2>&1 & "
        "echo $! > /tmp/vite.pid)",
    )
    await _wait_for(runtime, sandbox, _listening(5173))
    link = await runtime.preview_link(sandbox, 5173)
    assert link.token == ""
    assert (await _fetch(runtime, peer, link.url))[0] == "000"

    await runtime.expose_port(sandbox, 5173)
    await runtime.expose_port(sandbox, 5173)  # idempotent: still one relay
    assert await _relays(runtime, sandbox) == 1
    code, body = await _fetch(runtime, peer, link.url)
    assert code == "200" and "enigma-vite" in body
    # A sibling tenant on the same bridge gets nothing: the relay admits the proxy only.
    assert (await _fetch(runtime, stranger, link.url))[0] == "000"
    hmr = await runtime.exec(
        peer,
        "node -e \"const ws = new WebSocket(process.argv[1], 'vite-hmr'); "
        "ws.onmessage = (e) => { console.log(e.data); process.exit(0) }; "
        "setTimeout(() => process.exit(2), 8000)\" " + link.url.replace("http://", "ws://") + "/",
        timeout=20,
    )
    assert hmr.exit_code == 0 and '"connected"' in hmr.stdout, hmr.stdout

    # The relay leaves with the app, so a relaunch on 0.0.0.0 finds the port free.
    await _sh(runtime, sandbox, "kill -- -$(cat /tmp/vite.pid) || kill $(cat /tmp/vite.pid)")
    await _wait_for(runtime, sandbox, NO_RELAY, seconds=10)
    await _sh(
        runtime,
        sandbox,
        f"cd {APP} && (setsid nohup npx vite --host --port 5173 >/tmp/vite2.log 2>&1 &)",
    )
    await _wait_for(runtime, sandbox, _listening(5173))
    await runtime.expose_port(sandbox, 5173)
    assert await _relays(runtime, sandbox) == 0  # already reachable: nothing to relay
    code, body = await _fetch(runtime, peer, (await runtime.preview_link(sandbox, 5173)).url)
    assert code == "200" and "enigma-vite" in body


async def test_code_server_reaches_the_proxy_and_never_a_sibling(
    runtime, sandbox, peer, stranger
) -> None:
    """code-server runs `--auth none`; on the shared bridge it must stay off every address
    but the proxy's, or another tenant could drive this workspace's editor and terminal."""
    from app.services.editor import EditorService

    runtime._resolve_host = _resolve_from(runtime, peer)
    settings = get_settings()
    editor = EditorService(None, settings, runtime)  # type: ignore[arg-type]  # launch only
    launched = await runtime.exec(
        sandbox,
        editor._ensure_command(ROOT, loopback_only=True),
        timeout=settings.editor_install_timeout_seconds,
    )
    assert launched.exit_code == 0, launched.stdout
    await runtime.expose_port(sandbox, settings.editor_port)
    assert await _relays(runtime, sandbox) == 1

    link = await runtime.preview_link(sandbox, settings.editor_port)
    assert (await _fetch(runtime, peer, f"{link.url}/healthz"))[0] == "200"
    assert (await _fetch(runtime, stranger, f"{link.url}/healthz"))[0] == "000"


async def test_two_services_start_preview_log_and_stop_on_docker(
    runtime,
    peer,
    real_db_session,  # noqa: F811
) -> None:
    from sqlalchemy import select

    from app.models import PreviewSession
    from app.services.workspace_services import WorkspaceServiceManager
    from tests.test_workspace_services import _row_for_command, make_service_command

    runtime._resolve_host = _resolve_from(runtime, peer)
    session, _ = real_db_session
    user = User(
        id=uuid4(),
        email=f"docker-it-{uuid4().hex[:8]}@example.com",
        password_hash=hash_password("password"),
        role="owner",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    workspace = _workspace(user.id)
    workspace.sandbox_id = await runtime.provision(workspace)
    workspace.status = "ready"
    session.add_all([user, workspace])
    await session.flush()
    try:
        await _sh(runtime, workspace, f"mkdir -p {ROOT} && cd {ROOT} && git init -q")
        api = await make_service_command(
            session,
            workspace,
            name="API",
            command="python3 -m http.server 8000 --bind 127.0.0.1",
            port=8000,
        )
        web = await make_service_command(
            session,
            workspace,
            name="Web",
            command="env | grep ^ENIGMA_ > env.txt && exec python3 -m http.server 5173",
            port=5173,
        )
        manager = WorkspaceServiceManager(session, get_settings(), runtime)
        await manager.list(workspace.id, user.id)
        rows = [await _row_for_command(session, workspace.id, c.id) for c in (api, web)]
        for row in rows:
            await manager.start(workspace.id, row.id, user.id)

        for _ in range(120):
            listed, _primary = await manager.list(workspace.id, user.id)
            if all(r.status == "running" for r in listed if r.id in {row.id for row in rows}):
                break
            await asyncio.sleep(0.5)
        else:
            raise AssertionError([(r.status, r.last_error) for r in listed])

        previews = (
            await session.execute(
                select(PreviewSession).where(
                    PreviewSession.workspace_id == workspace.id, PreviewSession.status == "active"
                )
            )
        ).scalars().all()
        assert sorted(p.port for p in previews) == [5173, 8000]
        # The loopback-only API is relayed; the web service binds 0.0.0.0 on its own.
        assert await _relays(runtime, workspace) == 1

        api_code, _ = await _fetch(runtime, peer, (await runtime.preview_link(workspace, 8000)).url)
        assert api_code == "200"
        web_url = (await runtime.preview_link(workspace, 5173)).url
        code, env_txt = await _fetch(runtime, peer, f"{web_url}/env.txt")
        assert code == "200" and f"ENIGMA_WORKSPACE_ID={workspace.id}" in env_txt
        logs = await manager.logs_tail(workspace.id, rows[1].id, user.id, 4000)
        assert "GET /env.txt" in logs

        for row in rows:
            stopped = await manager.stop(workspace.id, row.id, user.id)
            assert stopped.status == "stopped"
        await _wait_for(runtime, workspace, NO_RELAY, seconds=10)
    finally:
        await runtime.delete(workspace)


# --- Epic 05: lifecycle, limits and egress on a real daemon --------------------------------


async def _private_runtime(**settings) -> DockerRuntime:
    runtime = DockerRuntime(
        Settings(**{"docker_sandbox_memory_mb": 1024, "docker_sandbox_cpus": 1, **settings})
    )
    runtime.activity.touch = _no_activity  # type: ignore[method-assign]  # no DB rows here
    return runtime


async def _no_activity(_workspace_id) -> None:
    return None


async def test_block_all_cuts_egress_but_the_proxy_still_reaches_the_sandbox(runtime, peer) -> None:
    blocked = await _private_runtime(sandbox_network_block_all=True)
    blocked.settings.docker_preview_proxy_label = runtime.settings.docker_preview_proxy_label
    blocked.settings.docker_preview_proxy_host = runtime.settings.docker_preview_proxy_host
    blocked._resolve_host = _resolve_from(runtime, peer)
    workspace = _workspace()
    try:
        workspace.sandbox_id = await blocked.provision(workspace)
        out = await blocked.exec(
            workspace, "curl -sS -m 5 -o /dev/null https://example.com; echo rc=$?"
        )
        assert "rc=0" not in out.stdout
        lookup = await blocked.exec(workspace, "getent hosts example.com; echo rc=$?")
        assert "rc=0" not in lookup.stdout  # no DNS to the outside either
        await _sh(
            blocked, workspace, "(setsid nohup python3 -m http.server 8000 >/dev/null 2>&1 &)"
        )
        await _wait_for(blocked, workspace, _listening(8000))
        link = await blocked.preview_link(workspace, 8000)
        assert (await _fetch(runtime, peer, link.url))[0] == "200"
    finally:
        await blocked.delete(workspace)
        await blocked.close()


@pytest.fixture
async def egress_proxy(runtime):
    """``app/egress_proxy.py`` in a container with a way out, labelled for one test only;
    the sandbox image has the Python it needs (the module is stdlib only)."""
    from pathlib import Path

    import app.egress_proxy as module

    label = f"enigma.egress-test={uuid4().hex}"
    key, value = label.split("=")
    client, _ = await runtime._clients()
    container = await asyncio.to_thread(
        client.containers.run,
        runtime.settings.docker_sandbox_image,
        entrypoint=["python3", "-c", Path(module.__file__).read_text()],
        command=[],
        detach=True,
        labels={key: value},
        environment={"SANDBOX_NETWORK_ALLOW_LIST": "1.1.1.1/32"},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
    )
    try:
        for _ in range(50):
            logs = await asyncio.to_thread(container.logs)
            if b"egress_proxy_listening" in logs:
                break
            await asyncio.sleep(0.2)
        else:
            pytest.fail(f"egress proxy did not start: {logs!r}")
        yield label
    finally:
        await asyncio.to_thread(container.remove, force=True)


async def test_the_allow_list_lets_only_listed_destinations_out_through_the_proxy(
    egress_proxy,
) -> None:
    """SANDBOX_NETWORK_ALLOW_LIST on Docker (needs outbound network to 1.1.1.1): the sandbox
    network is internal; an allow-listed address is reached through the egress proxy, any
    other one is refused by it, and nothing gets out without it."""
    allowed = await _private_runtime(
        sandbox_network_allow_list="1.1.1.1/32", docker_egress_proxy_label=egress_proxy
    )
    workspace = _workspace()
    curl = "curl -sS -m 10 -o /dev/null -w '%{http_code}'"
    try:
        workspace.sandbox_id = await allowed.provision(workspace)
        through = await allowed.exec(workspace, f"{curl} http://1.1.1.1/; echo; echo rc=$?")
        assert through.stdout.splitlines()[0] in {"200", "301", "302"}, through.stdout
        tls = await allowed.exec(workspace, f"{curl} https://1.1.1.1/; echo; echo rc=$?")
        assert "rc=0" in tls.stdout, tls.stdout  # a CONNECT tunnel
        refused = await allowed.exec(workspace, f"{curl} http://8.8.8.8/; echo")
        assert refused.stdout.strip() == "403", refused.stdout
        direct = await allowed.exec(
            workspace, f"{curl} --noproxy '*' http://1.1.1.1/; echo rc=$?"
        )
        assert "rc=0" not in direct.stdout, direct.stdout  # internal: no route without it
    finally:
        await allowed.delete(workspace)
        await allowed.close()


async def test_a_disk_quota_caps_the_sandbox_writable_layer() -> None:
    """DOCKER_SANDBOX_DISK_MB, where the storage driver supports it (Podman's overlay here;
    Docker needs overlay2 on xfs with pquota) -- elsewhere provisioning says so."""
    from app.services.sandbox import ProvisioningError

    quota = await _private_runtime(docker_sandbox_disk_mb=256)
    workspace = _workspace()
    try:
        try:
            workspace.sandbox_id = await quota.provision(workspace)
        except ProvisioningError as error:
            assert "DOCKER_SANDBOX_DISK_MB" in str(error)
            pytest.skip("this daemon's storage driver has no per-container quota")
        size = await quota.exec(workspace, "df -m / | tail -1")
        assert size.stdout.split()[1] == "256", size.stdout
        fill = await quota.exec(
            workspace, "head -c 300M /dev/zero > ~/big; echo rc=$?; rm -f ~/big", timeout=120
        )
        assert "rc=0" not in fill.stdout and "No space" in fill.stdout, fill.stdout
        assert (await quota.exec(workspace, "echo alive")).stdout.strip() == "alive"
    finally:
        await quota.delete(workspace)
        await quota.close()


async def test_a_fork_bomb_hits_the_pids_limit_and_the_sandbox_survives() -> None:
    limited = await _private_runtime(docker_sandbox_pids_limit=64)
    workspace = _workspace()
    try:
        workspace.sandbox_id = await limited.provision(workspace)
        # The bomb runs short sleepers; while they hold every pid even the exec that
        # started them cannot report back, so read the outcome once they have exited.
        await limited.exec(
            workspace,
            "for i in $(seq 1 200); do sleep 4 & done 2>/tmp/fork.err; true",
            timeout=60,
        )
        await asyncio.sleep(6)
        failed = await limited.exec(workspace, "grep -c -i 'fork\\|resource' /tmp/fork.err")
        assert int(failed.stdout.strip() or 0) > 0, failed.stdout
        assert (await limited.exec(workspace, "echo alive")).stdout == "alive\n"
    finally:
        await limited.delete(workspace)
        await limited.close()


async def test_an_oom_kill_surfaces_as_a_status_detail() -> None:
    small = await _private_runtime(docker_sandbox_memory_mb=256)
    workspace = _workspace()
    try:
        workspace.sandbox_id = await small.provision(workspace)
        assert await small.status_detail(workspace) is None
        await small.exec(workspace, "python3 -c 'b = bytearray(600 * 1024 * 1024)'", timeout=60)
        detail = await small.status_detail(workspace)
        assert detail is not None and "256 MB" in detail
        assert await small.status(workspace) == "ready"  # the sandbox itself lives on
    finally:
        await small.delete(workspace)
        await small.close()


async def test_idle_stop_keeps_the_sandbox_and_the_next_use_wakes_it(runtime, sandbox) -> None:
    await _sh(runtime, sandbox, f"echo kept > {ROOT}/note.txt")
    assert "tmpfs" in await _sh(runtime, sandbox, "grep ' /tmp ' /proc/mounts")

    assert await runtime.stop_if_running(sandbox) is True
    assert await runtime.status(sandbox) == "stopped"
    assert await runtime.stop_if_running(sandbox) is False

    assert (await runtime.exec(sandbox, f"cat {ROOT}/note.txt")).stdout == "kept\n"
    assert await runtime.status(sandbox) == "ready"


async def test_upkeep_only_ever_sees_containers_enigma_created(runtime, sandbox) -> None:
    client, _ = await runtime._clients()
    decoy = await asyncio.to_thread(
        client.containers.run,
        "enigma-sandbox:latest",
        entrypoint=["sleep", "300"],
        detach=True,
        labels={"app": "not-enigma"},
    )
    try:
        listed = {container_id for container_id, _ in await runtime.managed_sandboxes()}
        assert sandbox.sandbox_id in listed
        assert decoy.id not in listed
    finally:
        await asyncio.to_thread(decoy.remove, force=True)


async def test_agent_tools_work_offline_under_block_all() -> None:
    """With egress blocked the agent still edits, searches, runs and diffs, and the terminal
    works; a command that needs the network fails as an ordinary non-zero exit, never a hang
    or a tool error."""
    blocked = await _private_runtime(sandbox_network_block_all=True)
    workspace = _workspace()
    try:
        workspace.sandbox_id = await blocked.provision(workspace)
        await WorkspaceInitializer(blocked).initialize_blank(workspace)
        tools = WorkspaceTools(
            workspace, runtime=blocked, command_timeout_seconds=30, max_command_output_chars=20_000
        )
        await tools.write_file("src/app.py", "print('offline')\n")
        assert await tools.read_file("src/app.py") == "print('offline')\n"
        found = await tools.search_code("offline")
        assert [m.path for m in found.matches] == ["src/app.py"]
        ran = await tools.run_command("python3 src/app.py")
        assert (ran.exit_code, ran.stdout) == (0, "offline\n")
        await tools.write_file("README.md", "# offline\n")
        assert "+# offline" in await tools.git_diff()
        assert (await tools.git_status()).is_clean is False

        fetch = "__import__('urllib.request').request.urlopen('https://example.com', timeout=5)"
        online = await tools.run_command(f'python3 -c "{fetch}"')
        assert online.exit_code != 0 and "URLError" in online.stdout + online.stderr

        output: list[bytes] = []
        tab = await blocked.create_or_connect_pty(
            workspace, f"execute-{workspace.id.hex}", _collect(output), env=None, cols=80, rows=24
        )
        try:
            await tab.send_input(b"echo offline-$((6*7))\n")
            await _until_output(output, b"offline-42")  # the terminal needs no network
        finally:
            await tab.disconnect()
    finally:
        await blocked.delete(workspace)
        await blocked.close()


async def _until_output(store: list[bytes], text: bytes) -> None:
    for _ in range(100):
        if text in b"".join(store):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{text!r} never arrived: {b''.join(store)[-300:]!r}")


def _collect(store: list[bytes]):
    async def on_data(data: bytes) -> None:
        store.append(data)

    return on_data


async def test_an_attached_terminal_spares_an_idle_sandbox_until_the_busy_cap(maker) -> None:  # noqa: F811
    """The reaper against a real container and real rows: an attached tmux client keeps a
    sandbox idle past the window running, a detached one does not, a terminal wakes it, and
    past SANDBOX_BUSY_MAX_MINUTES even an attached client does not save it."""
    from sqlalchemy import text

    runtime = await _private_runtime(sandbox_auto_stop_minutes=30, sandbox_busy_max_minutes=120)
    workspace = await _sandboxed(maker, None, runtime, idle_minutes=45)
    only = {workspace.id}
    session_id = f"execute-{workspace.id.hex}"
    tab = None
    try:
        output: list[bytes] = []
        tab = await runtime.create_or_connect_pty(
            workspace, session_id, _collect(output), env=None, cols=80, rows=24
        )
        await tab.send_input(b"echo attached-$((6*7))\n")
        await _until_output(output, b"attached-42")
        assert await stop_idle_sandboxes(maker, runtime.settings, runtime, only=only) == 0
        assert await runtime.status(workspace) == "ready"

        await tab.disconnect()  # the tmux session lives on, detached: that is not "busy"
        assert await stop_idle_sandboxes(maker, runtime.settings, runtime, only=only) == 1
        assert await runtime.status(workspace) == "stopped"

        output.clear()
        tab = await runtime.create_or_connect_pty(
            workspace, session_id, _collect(output), env=None, cols=80, rows=24
        )
        await tab.send_input(b"echo woke-$((6*7))\n")
        await _until_output(output, b"woke-42")  # the terminal started the stopped sandbox
        assert await runtime.status(workspace) == "ready"

        async with maker() as session:
            await session.execute(
                text(
                    "UPDATE sandbox_activity SET last_activity_at = "
                    "clock_timestamp() - make_interval(mins => 180) WHERE workspace_id = :id"
                ),
                {"id": workspace.id},
            )
            await session.commit()
        assert await stop_idle_sandboxes(maker, runtime.settings, runtime, only=only) == 1
        assert await runtime.status(workspace) == "stopped"
    finally:
        if tab is not None:
            await tab.disconnect()
        await runtime.delete(workspace)
        await runtime.close()


async def test_the_service_probe_does_not_mistake_its_own_exec_for_the_service(
    runtime, sandbox
) -> None:
    """Docker runs every exec as ``sh -c <script>``, so the probe script -- which names the
    service's marker -- is in its own shells' command lines: a ``pgrep -f <marker>`` fallback
    found those and reported a dead service alive, and a stopped dev server was never
    relaunched. Only a process other than the probe and its launchers counts."""
    from app.services.service_scripts import ProbeRow, build_probe_script, parse_probe_output
    from app.services.service_scripts import service_paths as paths_for

    paths = paths_for(uuid4())
    row = ProbeRow(id="svc", paths=paths, port=None, health_path=None)

    result = await runtime.exec(sandbox, build_probe_script([row]))
    assert parse_probe_output(result.stdout)["svc"].pid_alive is False, result.stdout

    marker = paths.grep_token
    await _sh(
        runtime,
        sandbox,
        f"(setsid nohup python3 -c 'import time; time.sleep(300)' {marker} "
        ">/dev/null 2>&1 &)",
    )
    result = await runtime.exec(sandbox, build_probe_script([row]))
    assert parse_probe_output(result.stdout)["svc"].pid_alive is True, result.stdout


async def test_a_concurrent_probe_is_not_mistaken_for_the_service(runtime, sandbox) -> None:
    """Two probes of one workspace can overlap (two requests, or a request and upkeep); each
    names every marker in its own command line, so neither may count the other."""
    from app.services.service_scripts import ProbeRow, build_probe_script, parse_probe_output
    from app.services.service_scripts import service_paths as paths_for

    row = ProbeRow(id="svc", paths=paths_for(uuid4()), port=None, health_path=None)
    # A probe that is still running (stalled on its health check) while the next one looks.
    stall = "import time\ntime.sleep(20)\nimport http.client"
    slow = build_probe_script([row]).replace("import http.client", stall, 1)
    stalled = asyncio.create_task(runtime.exec(sandbox, slow, timeout=60))
    await asyncio.sleep(2)
    try:
        result = await runtime.exec(sandbox, build_probe_script([row]))
        assert parse_probe_output(result.stdout)["svc"].pid_alive is False, result.stdout
    finally:
        stalled.cancel()
        await asyncio.gather(stalled, return_exceptions=True)
