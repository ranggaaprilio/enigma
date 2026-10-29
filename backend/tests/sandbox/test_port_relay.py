"""The in-sandbox relay that puts a loopback-only dev server on the sandbox network.

The script itself runs inside a container (covered for real by the Docker integration
suite); here its pure parts run on synthetic ``/proc/net/tcp`` tables.
"""

from __future__ import annotations

import asyncio
import os
import shlex

import pytest

from app.services.sandbox.port_relay import RELAY_SCRIPT, port_relay_command

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"


@pytest.fixture
def relay():
    namespace: dict = {"__name__": "relay_under_test"}
    exec(compile(RELAY_SCRIPT, "relay", "exec"), namespace)  # noqa: S102 - our own script
    return namespace


def _table(tmp_path, name: str, rows: list[tuple[str, int, str]]) -> str:
    lines = [
        f"   {i}: {address}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 0 1000\n"
        for i, (address, port, state) in enumerate(rows)
    ]
    path = tmp_path / name
    path.write_text(HEADER + "".join(lines))
    return str(path)


def test_loopback_only_ipv4_listener_is_relayed_to_127_0_0_1(relay, tmp_path):
    tcp = _table(tmp_path, "tcp", [("0100007F", 5173, "0A"), ("0100007F", 9999, "0A")])
    addresses = relay["listeners"](5173, tables=(tcp,))
    assert relay["relay_target"](addresses) == "127.0.0.1"


def test_loopback_only_ipv6_listener_is_relayed_to_the_v6_loopback(relay, tmp_path):
    tcp6 = _table(tmp_path, "tcp6", [("00000000000000000000000001000000", 5173, "0A")])
    addresses = relay["listeners"](5173, tables=(tcp6,))
    assert relay["relay_target"](addresses) == "::1"


@pytest.mark.parametrize(
    ("table", "address"),
    [
        ("tcp", "00000000"),  # 0.0.0.0
        ("tcp6", "00000000000000000000000000000000"),  # ::
        ("tcp", "0300590A"),  # a specific non-loopback address
    ],
)
def test_a_listener_reachable_off_loopback_needs_no_relay(relay, tmp_path, table, address):
    path = _table(tmp_path, table, [(address, 8000, "0A"), ("0100007F", 8000, "0A")])
    addresses = relay["listeners"](8000, tables=(path,))
    assert relay["relay_target"](addresses) is None


def test_a_closed_port_or_a_non_listening_socket_needs_no_relay(relay, tmp_path):
    # 01 = ESTABLISHED: a client connection to the port is not a listener.
    tcp = _table(tmp_path, "tcp", [("0100007F", 3000, "01")])
    assert relay["listeners"](3000, tables=(tcp,)) == set()
    assert relay["relay_target"](set()) is None


def test_missing_tables_read_as_no_listeners(relay, tmp_path):
    assert relay["listeners"](3000, tables=(str(tmp_path / "absent"),)) == set()


def test_claim_is_exclusive_while_the_owner_lives_and_recovers_a_stale_pidfile(relay, tmp_path):
    pidfile = str(tmp_path / "relay.pid")
    # Stand in for /proc/<pid>/cmdline: this test process plays the running relay.
    relay["relay_alive"] = lambda pid: pid == os.getpid()
    assert relay["claim"](pidfile) is True
    assert relay["read_pid"](pidfile) == os.getpid()
    # A second claimant sees a live owner (this process) and backs off.
    assert relay["claim"](pidfile) is False

    with open(pidfile, "w") as handle:
        handle.write("999999999")  # no such process
    assert relay["claim"](pidfile) is True
    assert relay["read_pid"](pidfile) == os.getpid()


def test_a_reused_pid_that_is_not_a_relay_counts_as_dead(relay, tmp_path):
    """After a container restart the pidfile in /tmp survives and its pid may now belong to
    another process; only a process running this script counts as the port's relay."""
    pidfile = str(tmp_path / "relay.pid")
    with open(pidfile, "w") as handle:
        handle.write("1")  # alive, but not a relay

    assert relay["relay_alive"](relay["read_pid"](pidfile)) is False
    assert relay["claim"](pidfile) is True
    assert relay["read_pid"](pidfile) == os.getpid()
    assert sorted(os.listdir(tmp_path)) == ["relay.pid"]  # staging files cleaned up


def test_relay_command_quotes_the_script_and_passes_the_port_and_the_proxy_name():
    command = port_relay_command(5173, "preview-proxy")
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    assert argv[2] == RELAY_SCRIPT
    assert argv[3:] == ["5173", "preview-proxy"]


def test_relay_command_refuses_a_proxy_name_that_is_not_a_hostname():
    with pytest.raises(ValueError):
        port_relay_command(5173, "proxy; rm -rf ~")


async def _echo_server():
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _relay_on(relay, upstream_port: int, allowed: set[str]):
    """The relay's server, listening on a free local port, forwarding to the upstream."""
    peers = set(allowed)
    server = await relay["start_relay"]("127.0.0.1", 0, "127.0.0.1", upstream_port, peers)
    return server, server.sockets[0].getsockname()[1]


async def test_the_relay_forwards_bytes_both_ways_for_the_proxy(relay):
    upstream, upstream_port = await _echo_server()
    server, port = await _relay_on(relay, upstream_port, {"127.0.0.1"})
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(16), 2) == b"GET / HTTP/1.1\r\n"
        writer.write(b"upgrade-frame")  # a long-lived stream keeps flowing (WebSocket/HMR)
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(13), 2) == b"upgrade-frame"
        writer.close()
    finally:
        server.close()
        upstream.close()


async def test_the_relay_drops_a_connection_from_anything_but_the_proxy(relay):
    """Security: sandboxes share the bridge network, so a sibling tenant must never reach a
    loopback-only service (or the editor) through the relay."""
    upstream, upstream_port = await _echo_server()
    server, port = await _relay_on(relay, upstream_port, {"10.89.0.250"})  # the proxy elsewhere
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(b"hello")
            await writer.drain()
            received = await asyncio.wait_for(reader.read(100), 2)
        except ConnectionResetError:
            received = b""
        assert received == b""  # closed (EOF or reset), nothing relayed
        writer.close()
    finally:
        server.close()
        upstream.close()


def test_hex_v4_matches_the_proc_net_tcp_encoding(relay):
    assert relay["hex_v4"]("127.0.0.1") == "0100007F"
    assert relay["hex_v4"]("10.89.6.2") == "0206590A"
