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
The template, ``specify init`` and Vite tests need outbound network from the sandbox.
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
from app.services.speckit import SPECIFY_INIT_COMMAND
from app.services.workspace_env import materialize_env_file
from app.services.workspace_init import WorkspaceInitializer
from app.services.workspaces import WorkspaceSecretCipher
from app.services.worktrees import WorktreeService
from tests.test_commands import real_db_session  # noqa: F401  (fixture)

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
    assert list(container.attrs["NetworkSettings"]["Networks"]) == ["enigma-sandboxes"]


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
    runtime.settings.docker_preview_proxy_host = container_name(workspace)
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
