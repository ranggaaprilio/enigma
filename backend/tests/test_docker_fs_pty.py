"""Docker provider file access and terminal: argv safety, tar/ownership details, tmux.

The provider-neutral contract (round trip, unusual names, PTY reconnect) is in
``tests/sandbox/test_conformance.py``; a real container is in
``tests/test_docker_integration.py``.
"""

from __future__ import annotations

import asyncio

import pytest
import requests

from app.services.agent_tools import WorkspaceTools
from app.services.sandbox import PtySize, RuntimeUnavailableError, SandboxRuntimeError, docker_fs
from app.services.sandbox.docker_fs import parse_find_records, parse_grep_matches
from app.services.sandbox.docker_pty import tmux_session_name
from tests.sandbox.fakes import FakeDockerClient, FakeStdinSocket
from tests.test_docker_runtime import provisioned

HOME = "/home/daytona"
ROOT = f"{HOME}/workspace"


async def docker_fs_case():
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    container = client.containers.get(workspace.sandbox_id)
    return runtime, workspace, container, await runtime.fs(workspace)


def path_argv(container) -> list[list[str]]:
    return [argv for argv in container.exec_runs if argv[0] != "mkdir" or "-m" in argv]


# --- argv safety -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["; rm -rf ~", "$(id)", "`id`", "a\nb", "-rf", "--help"])
async def test_hostile_paths_are_single_argv_elements_never_shell_text(name: str) -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    path = f"{ROOT}/{name}"
    await fs.upload_file(b"x", path)
    await fs.get_file_info(path)
    await fs.list_files(ROOT)
    await fs.set_file_permissions(path, mode="600")
    await fs.find_files(ROOT, name)
    await fs.move_files(path, f"{ROOT}/renamed")
    await fs.delete_file(f"{ROOT}/renamed")

    for argv in container.exec_runs[1:]:  # [0] is the provision-time mkdir
        for position, value in enumerate(argv):
            if name in value:
                # A user path is a whole argument: after ``--``, ``$1`` of a constant
                # script, or ``find``'s start point (always absolute, see below).
                assert value in (path, name), argv
                assert argv[0] != "/bin/sh" or position == 4, argv


async def test_relative_paths_resolve_under_home_so_find_never_sees_an_option() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    with pytest.raises(FileNotFoundError):
        await fs.get_file_info("-delete")
    assert container.exec_runs[-1][:2] == ["find", f"{HOME}/-delete"]


async def test_invalid_modes_are_refused_before_any_exec() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    before = len(container.exec_runs)
    for call in (
        fs.set_file_permissions(f"{ROOT}/a", mode="u+s; id"),
        fs.create_folder(f"{ROOT}/d", "0777 -x"),
    ):
        with pytest.raises(ValueError, match="invalid file mode"):
            await call
    assert len(container.exec_runs) == before


# --- semantics ---------------------------------------------------------------------------


async def test_upload_streams_over_stdin_as_the_sandbox_user_and_keeps_the_mode() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    container = client.containers.get(workspace.sandbox_id)
    fs = await runtime.fs(workspace)
    script = f"{ROOT}/bin/run.sh"
    await fs.upload_file(b"#!/bin/sh\n", script)  # parent created on demand
    assert container.fs.modes[script] == "644"

    await fs.set_file_permissions(script, mode="755")
    await fs.upload_file(b"#!/bin/sh\necho edited\n", script)
    assert container.fs.modes[script] == "755"  # truncated in place, not replaced
    assert container.fs.files[script] == b"#!/bin/sh\necho edited\n"

    (upload, *_) = [r for r in client.api.execs.values() if 'head -c "$3" >' in r["cmd"][2]]
    # The bytes travel on stdin, never in the argv; and no ``user=`` override: the exec
    # runs as the container's non-root user, so the kernel checks every write.
    assert upload["cmd"][3:] == ["sh", script, f"{ROOT}/bin", "10"]
    assert not hasattr(container, "put_archive") and not hasattr(container, "get_archive")


async def test_unreadable_targets_fail_as_permission_errors() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    container.fs.files["/etc/shadow"] = b"root:x"
    container.fs.unreadable.add("/etc/shadow")
    container.fs.links[f"{ROOT}/sneaky"] = "/etc/shadow"
    with pytest.raises(PermissionError):
        await fs.download_file(f"{ROOT}/sneaky")
    with pytest.raises(PermissionError):
        await fs.upload_file(b"pwned", f"{ROOT}/sneaky")
    assert container.fs.files["/etc/shadow"] == b"root:x"


async def test_symlinks_are_reported_and_followed_like_daytona() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    await fs.upload_file(b"target", f"{ROOT}/real.txt")
    container.fs.links[f"{ROOT}/link.txt"] = f"{ROOT}/real.txt"
    await fs.create_folder(f"{ROOT}/dir", "755")
    container.fs.links[f"{ROOT}/dirlink"] = f"{ROOT}/dir"

    info = await fs.get_file_info(f"{ROOT}/link.txt")
    assert (info.is_symlink, info.is_dir) == (True, False)
    assert (await fs.get_file_info(f"{ROOT}/dirlink")).is_dir is True
    assert await fs.download_file(f"{ROOT}/link.txt") == b"target"

    await fs.upload_file(b"through", f"{ROOT}/link.txt")
    assert container.fs.files[f"{ROOT}/real.txt"] == b"through"
    assert container.fs.links[f"{ROOT}/link.txt"] == f"{ROOT}/real.txt"


async def test_download_refuses_directories_missing_files_and_oversized_files(
    monkeypatch,
) -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    with pytest.raises(IsADirectoryError):
        await fs.download_file(ROOT)
    with pytest.raises(FileNotFoundError) as missing:
        await fs.download_file(f"{ROOT}/nope")
    # A per-file problem, not "the sandbox is unavailable".
    assert not isinstance(missing.value, SandboxRuntimeError)

    await fs.upload_file(b"0123456789", f"{ROOT}/big.bin")
    monkeypatch.setattr(docker_fs, "MAX_DOWNLOAD_BYTES", 5)
    with pytest.raises(OSError, match="too large"):
        await fs.download_file(f"{ROOT}/big.bin")


async def test_non_recursive_delete_only_removes_files_and_empty_folders() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    await fs.upload_file(b"x", f"{ROOT}/full/a.txt")
    with pytest.raises(OSError, match="not empty"):
        await fs.delete_file(f"{ROOT}/full")
    await fs.delete_file(f"{ROOT}/full/a.txt")
    await fs.delete_file(f"{ROOT}/full")
    assert f"{ROOT}/full" not in container.fs.dirs


async def test_find_files_is_bounded_and_fails_only_for_a_missing_root(monkeypatch) -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    with pytest.raises(FileNotFoundError):
        await fs.find_files(f"{ROOT}/nope", "needle")

    before = len(container.exec_runs)
    assert await fs.find_files(ROOT, "two\nlines") == []
    assert len(container.exec_runs) == before  # answered without running grep

    record = b"/w/a.txt\x0012:needle\n"
    monkeypatch.setattr(docker_fs, "MAX_SEARCH_OUTPUT_BYTES", len(record) + 5)
    container.fs.run = lambda argv: (0, (record + b"/w/b.txt\x003:need")[: int(argv[-1])], b"")
    assert [(m.file, m.line) for m in await fs.find_files(ROOT, "need")] == [("/w/a.txt", 12)]


async def test_daemon_failures_still_surface_as_sandbox_runtime_errors() -> None:
    runtime, workspace, container, fs = await docker_fs_case()

    def down(*args, **kwargs):
        raise requests.exceptions.ConnectionError("http+docker://localhost/secret.sock")

    container.exec_run = down
    with pytest.raises(RuntimeUnavailableError, match="Docker is unavailable"):
        await fs.list_files(ROOT)


async def test_file_operations_never_use_the_control_pool() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    pools: list[str] = []
    original = runtime._run

    async def recording(kind, func, *args, **kwargs):
        pools.append(kind)
        return await original(kind, func, *args, **kwargs)

    runtime._run = recording
    await fs.upload_file(b"x", f"{ROOT}/a.txt")
    await fs.download_file(f"{ROOT}/a.txt")
    await fs.list_files(ROOT)
    assert pools and set(pools) == {"exec"}


def test_parsers_round_trip_any_file_name() -> None:
    record = b"f\0f\x0042\x001700000000.25\x00644\x00-rw-r--r--\x00daytona\x00daytona\x00/w/a\nb\0"
    (info,) = parse_find_records(record)
    assert (info.name, info.size, info.permissions) == ("a\nb", 42, "0644")
    assert info.mod_time == "2023-11-14T22:13:20.250000Z"
    matches = parse_grep_matches(b"/w/x:y\x003:a: b\n/w/z\x0010:\n")
    assert [(m.file, m.line, m.content) for m in matches] == [
        ("/w/x:y", 3, "a: b"),
        ("/w/z", 10, ""),
    ]


async def test_agent_tool_sequence_over_the_docker_fs() -> None:
    runtime, workspace, container, fs = await docker_fs_case()
    workspace.workspace_path = ROOT
    tools = WorkspaceTools(
        workspace, runtime=runtime, command_timeout_seconds=30, max_command_output_chars=20_000
    )
    await tools.write_file("src/app.py", "print('hello')\n")
    await tools.apply_patch(
        "src/app.py", "<<<<<<< SEARCH\nprint('hello')\n=======\nprint('bye')\n>>>>>>> REPLACE\n"
    )
    assert await tools.read_file("src/app.py") == "print('bye')\n"
    assert await tools.list_files() == ["src/app.py"]


# --- terminal ----------------------------------------------------------------------------


async def open_pty(session_id: str = "execute-ab:cd", env: dict[str, str] | None = None):
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    container = client.containers.get(workspace.sandbox_id)
    received: list[bytes] = []
    arrived = asyncio.Event()

    async def on_data(data: bytes) -> None:
        received.append(data)
        arrived.set()

    handle = await runtime.create_or_connect_pty(
        workspace, session_id, on_data, env=env, cols=100, rows=30, cwd="repo"
    )
    return client, runtime, workspace, container, handle, received, arrived


def test_session_names_are_tmux_safe() -> None:
    assert tmux_session_name("execute-ab12:cd34") == "execute-ab12_cd34"
    assert tmux_session_name("a.b c;d") == "a_b_c_d"


async def test_pty_attaches_a_named_tmux_session_with_env_and_cwd() -> None:
    client, runtime, workspace, container, handle, received, arrived = await open_pty(
        env={"FOO": "bar baz"}
    )
    (record,) = [r for r in client.api.execs.values() if r["tty"]]
    cmd = record["cmd"]
    assert cmd[:2] == ["/bin/sh", "-c"] and cmd[3].startswith("/tmp/.enigma-pty/")
    assert cmd[4:9] == ["tmux", "new-session", "-A", "-s", "execute-ab_cd"]
    tmux = cmd[4:]
    assert tmux[tmux.index("-c") + 1] == f"{HOME}/repo"
    assert tmux[tmux.index("-e") + 1] == "FOO=bar baz"
    assert tmux[-2:] == ["/bin/bash", "-l"]
    assert record["environment"] == ["TERM=xterm-256color"]
    assert [size for _, *size in client.api.resizes] == [[100, 30]]
    await handle.disconnect()


async def test_pty_relays_input_output_and_resizes() -> None:
    client, runtime, workspace, container, handle, received, arrived = await open_pty()
    await asyncio.wait_for(arrived.wait(), 5)
    await handle.send_input("ls\n")
    await handle.send_input(b"pwd\n")
    for _ in range(100):
        if b"pwd\n" in b"".join(received):
            break
        await asyncio.sleep(0.01)
    assert b"".join(received) == b"$ ls\npwd\n"
    # Terminal output is activity: an open, busy terminal keeps the sandbox from idling.
    assert workspace.id in runtime.activity.touches
    await handle.resize(PtySize(cols=120, rows=40))
    assert client.api.resizes[-1][1:] == (120, 40)
    await handle.disconnect()


async def test_disconnect_detaches_only_this_client_and_reconnect_reattaches() -> None:
    client, runtime, workspace, container, handle, received, arrived = await open_pty()
    (pidfile,) = container.tmux_clients
    sock = container.tmux_clients[pidfile]

    await handle.disconnect()
    await handle.disconnect()  # idempotent
    detaches = [a for a in container.exec_runs if a[:2] == ["/bin/sh", "-c"] and "HUP" in a[2]]
    assert [argv[3] for argv in detaches] == [pidfile]
    assert sock.closed
    assert container.tmux_sessions == {"execute-ab_cd"}  # the shell lives on

    async def on_data(data: bytes) -> None:
        return None

    again = await runtime.create_or_connect_pty(
        workspace, "execute-ab:cd", on_data, env=None, cols=80, rows=24
    )
    assert container.tmux_sessions == {"execute-ab_cd"}
    await again.disconnect()


async def test_kill_pty_ends_the_session_and_is_idempotent() -> None:
    client, runtime, workspace, container, handle, received, arrived = await open_pty()
    await handle.disconnect()
    await runtime.kill_pty(workspace, "execute-ab:cd")
    await runtime.kill_pty(workspace, "execute-ab:cd")
    assert container.tmux_sessions == set()
    assert ["tmux", "kill-session", "-t", "=execute-ab_cd"] in container.exec_runs


async def test_kill_pty_on_a_stopped_container_does_not_start_it() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    container = client.containers.get(workspace.sandbox_id)
    container.status = "exited"
    await runtime.kill_pty(workspace, "execute-ab:cd")
    assert container.start_calls == 0
    assert not any(argv[0] == "tmux" for argv in container.exec_runs)


async def test_pty_rejects_invalid_environment_names_before_any_exec() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)

    async def on_data(data: bytes) -> None:
        return None

    with pytest.raises(ValueError, match="invalid environment variable name"):
        await runtime.create_or_connect_pty(
            workspace, "t", on_data, env={"BAD;rm": "x"}, cols=80, rows=24
        )
    assert not any(record["tty"] for record in client.api.execs.values())


async def test_an_upload_cut_off_mid_stream_is_an_error_not_a_short_file(monkeypatch) -> None:
    runtime, workspace, container, fs = await docker_fs_case()

    def broken_sendall(self, data: bytes) -> None:
        self.received.extend(data[:3])
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(FakeStdinSocket, "sendall", broken_sendall)
    with pytest.raises(OSError, match="interrupted"):
        await fs.upload_file(b"0123456789", f"{ROOT}/a.txt")


async def test_terminal_readers_use_their_own_pool_and_input_never_the_control_pool() -> None:
    client = FakeDockerClient()
    runtime, workspace = await provisioned(client)
    pools: list[str] = []
    original = runtime._run

    async def recording(kind, func, *args, **kwargs):
        pools.append(kind)
        return await original(kind, func, *args, **kwargs)

    runtime._run = recording

    async def on_data(data: bytes) -> None:
        return None

    handle = await runtime.create_or_connect_pty(
        workspace, "t", on_data, env=None, cols=80, rows=24
    )
    assert pools.count("pty") == 1  # the reader, for the terminal's whole life
    pools.clear()
    await handle.send_input("ls\n")
    assert pools == ["exec"]
    await handle.disconnect()
