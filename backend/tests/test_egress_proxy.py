"""The Docker runtime's egress proxy (``app/egress_proxy.py``): SANDBOX_NETWORK_ALLOW_LIST
for sandboxes on ``internal`` networks."""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from app import egress_proxy
from app.egress_proxy import EgressProxy, destination_allowed, parse_networks, settings_from_env

POOL = parse_networks("10.213.0.0/16")


@pytest.mark.parametrize(
    ("address", "allow", "expected"),
    [
        ("140.82.112.4", "140.82.112.0/20", True),
        ("140.82.112.4", "", False),  # an empty allow list refuses everything
        ("8.8.8.8", "140.82.112.0/20", False),
        ("127.0.0.1", "0.0.0.0/0", False),  # the proxy's own loopback, whatever is listed
        ("169.254.169.254", "0.0.0.0/0", False),  # cloud metadata
        ("10.213.0.5", "10.0.0.0/8", False),  # another sandbox: the pool is always refused
        ("10.214.0.5", "10.0.0.0/8", True),
        ("::ffff:127.0.0.1", "0.0.0.0/0", False),  # IPv4-mapped loopback
        ("::ffff:140.82.112.4", "140.82.112.0/20", True),
        ("2606:50c0::1", "2606:50c0::/32", True),
        ("2606:50c0::1", "140.82.112.0/20", False),  # versions never cross-match
        ("::1", "::/0", False),
        ("0.0.0.0", "0.0.0.0/0", False),
        ("not-an-ip", "0.0.0.0/0", False),
    ],
)
def test_destination_allowed(address: str, allow: str, expected: bool) -> None:
    assert destination_allowed(address, parse_networks(allow), POOL) is expected


def test_settings_from_env_reads_the_allow_list_and_fails_on_a_bad_entry() -> None:
    allow, deny, port = settings_from_env({"DAYTONA_NETWORK_ALLOW_LIST": "1.2.3.0/24"})
    assert allow == [ipaddress.ip_network("1.2.3.0/24")]
    assert deny == POOL and port == 3128
    allow, _, port = settings_from_env(
        {"SANDBOX_NETWORK_ALLOW_LIST": "5.6.7.8/32", "EGRESS_PROXY_PORT": "8888"}
    )
    assert allow == [ipaddress.ip_network("5.6.7.8/32")] and port == 8888
    with pytest.raises(ValueError):
        settings_from_env({"SANDBOX_NETWORK_ALLOW_LIST": "1.2.3.0/24,example.com"})


# --- end to end over loopback ------------------------------------------------------------


@pytest.fixture
def loopback_is_a_destination(monkeypatch):
    """Tests dial an upstream on 127.0.0.1, which the real policy always refuses; let only
    the address the resolver maps "allowed.test" to through."""

    def allowed(address, allow, deny):
        return address == "127.0.0.1"

    monkeypatch.setattr(egress_proxy, "destination_allowed", allowed)


async def _upstream(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _proxy(resolved: dict[str, list[str]]):
    contacted: list[str] = []

    async def resolver(host: str, port: int) -> list[str]:
        contacted.append(host)
        if host not in resolved:
            raise OSError("no such host")
        return resolved[host]

    proxy = EgressProxy(parse_networks("0.0.0.0/0"), POOL, resolver=resolver)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], contacted


async def _status(reader: asyncio.StreamReader) -> bytes:
    return (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).split(b"\r\n", 1)[0]


async def test_connect_tunnels_both_ways_to_an_allowed_destination(
    loopback_is_a_destination,
) -> None:
    async def echo(reader, writer):
        writer.write(b"hello " + await reader.readexactly(5))
        await writer.drain()
        writer.close()

    upstream, upstream_port = await _upstream(echo)
    server, port, _ = await _proxy({"allowed.test": ["127.0.0.1"]})
    async with upstream, server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT allowed.test:{upstream_port} HTTP/1.1\r\n\r\n".encode())
        assert await _status(reader) == b"HTTP/1.1 200 Connection established"
        writer.write(b"world")
        assert await asyncio.wait_for(reader.read(), 5) == b"hello world"
        writer.close()


async def test_a_denied_destination_is_refused_and_never_dialled() -> None:
    dialled = []

    async def record(reader, writer):
        dialled.append(1)
        writer.close()

    upstream, upstream_port = await _upstream(record)
    # The real policy: loopback is refused even though 0.0.0.0/0 is "allowed".
    server, port, _ = await _proxy({"sneaky.test": ["127.0.0.1"]})
    async with upstream, server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT sneaky.test:{upstream_port} HTTP/1.1\r\n\r\n".encode())
        assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
        assert b"SANDBOX_NETWORK_ALLOW_LIST" in await asyncio.wait_for(reader.read(), 5)
        writer.close()
    assert dialled == []


async def test_only_an_allowed_address_of_a_name_is_dialled(loopback_is_a_destination) -> None:
    """A name resolving to a refused address first must not get that address dialled (and
    one resolving only to refused addresses gets a 403)."""

    async def ok(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 204 No Content\r\n\r\n")
        await writer.drain()
        writer.close()

    upstream, upstream_port = await _upstream(ok)
    server, port, _ = await _proxy(
        {"mixed.test": ["169.254.169.254", "127.0.0.1"], "bad.test": ["169.254.169.254"]}
    )
    async with upstream, server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET http://mixed.test:{upstream_port}/ HTTP/1.1\r\n\r\n".encode())
        assert await _status(reader) == b"HTTP/1.1 204 No Content"
        writer.close()

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT bad.test:{upstream_port} HTTP/1.1\r\n\r\n".encode())
        assert await _status(reader) == b"HTTP/1.1 403 Forbidden"
        writer.close()


async def test_plain_http_is_forwarded_in_origin_form_without_proxy_headers(
    loopback_is_a_destination,
) -> None:
    received: list[bytes] = []

    async def origin(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        received.append(await reader.readexactly(4))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    upstream, upstream_port = await _upstream(origin)
    server, port, _ = await _proxy({"allowed.test": ["127.0.0.1"]})
    async with upstream, server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST http://allowed.test:{upstream_port}/a/b?c=d HTTP/1.1\r\n"
            f"Host: allowed.test:{upstream_port}\r\n"
            "Proxy-Authorization: Basic c2VjcmV0\r\nProxy-Connection: keep-alive\r\n"
            "Content-Length: 4\r\n\r\nbody".encode()
        )
        assert await asyncio.wait_for(reader.read(), 5) == (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        )
        writer.close()

    head = received[0].decode()
    assert head.startswith("POST /a/b?c=d HTTP/1.1\r\n")
    assert "Proxy-" not in head and "Connection: close\r\n" in head
    assert f"Host: allowed.test:{upstream_port}" in head
    assert received[1] == b"body"


@pytest.mark.parametrize(
    "request_line",
    [
        b"GET https://allowed.test/ HTTP/1.1",  # https goes through CONNECT
        b"GET /relative HTTP/1.1",
        b"CONNECT allowed.test HTTP/1.1",  # no port
        b"garbage",
    ],
)
async def test_malformed_requests_get_a_400(request_line: bytes) -> None:
    server, port, contacted = await _proxy({"allowed.test": ["127.0.0.1"]})
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request_line + b"\r\n\r\n")
        assert await _status(reader) == b"HTTP/1.1 400 Bad Request"
        writer.close()
    assert contacted == []


async def test_an_oversized_header_is_refused() -> None:
    proxy = EgressProxy([], POOL)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0, limit=1024)
    port = server.sockets[0].getsockname()[1]
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://x/ HTTP/1.1\r\nX: " + b"a" * 4096 + b"\r\n\r\n")
        assert await _status(reader) == b"HTTP/1.1 400 Bad Request"
        writer.close()
