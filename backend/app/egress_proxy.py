"""Sandbox egress proxy: SANDBOX_NETWORK_ALLOW_LIST on the Docker runtime.

Docker cannot filter a container's traffic by destination without host access (iptables),
and sandboxes hold no capability to route through a gateway. So with an allow list every
sandbox network is ``internal`` -- no route out at all -- and the backend attaches this
proxy to each one under DOCKER_EGRESS_PROXY_HOST, the same way it attaches the preview
proxy. Sandbox commands and terminals get ``HTTP(S)_PROXY`` pointing here; the proxy
forwards a ``CONNECT`` tunnel or a plain-HTTP request only when the destination address is
inside an allowed CIDR. Anything that ignores the proxy settings (SSH, raw TCP, UDP) has no
way out.

The destination is resolved here, checked, and dialled by that exact address, so a name that
resolves differently a second time (DNS rebinding) cannot slip past the check. Loopback,
link-local (cloud metadata), multicast, unspecified and reserved addresses and the sandbox
subnet pool are refused whatever the allow list says: the proxy sits on every sandbox network
and must never become a path between them or into itself.

Stdlib only, and given no secrets (no DATABASE_URL, no AUTH_SECRET_KEY): it parses untrusted
traffic from every tenant. Run with ``python -m app.egress_proxy``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger("enigma.egress_proxy")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

DEFAULT_PORT = 3128
MAX_HEADER_BYTES = 64 * 1024
HEADER_TIMEOUT_SECONDS = 30
CONNECT_TIMEOUT_SECONDS = 10
MAX_CONNECTIONS = 512
COPY_CHUNK_BYTES = 64 * 1024
# Never forwarded upstream: they describe this hop, not the request.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "upgrade",
}
DENIED_MESSAGE = b"Egress to this destination is not allowed by SANDBOX_NETWORK_ALLOW_LIST\n"


def parse_networks(raw: str) -> list[Network]:
    """Comma-separated CIDRs. An invalid entry is an error: the proxy refuses to start
    rather than run with a policy other than the one configured."""
    return [
        ipaddress.ip_network(item.strip(), strict=False) for item in raw.split(",") if item.strip()
    ]


def _normalise(address: Address) -> Address:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def destination_allowed(address: str, allow: Sequence[Network], deny: Sequence[Network]) -> bool:
    try:
        ip = _normalise(ipaddress.ip_address(address))
    except ValueError:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if any(ip.version == net.version and ip in net for net in deny):
        return False
    return any(ip.version == net.version and ip in net for net in allow)


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class BadRequest(Exception):
    pass


def _split_authority(authority: str) -> tuple[str, int]:
    """``host:port`` / ``[v6]:port`` from a CONNECT target; the port is required."""
    parsed = urlsplit(f"//{authority}")
    try:
        port = parsed.port
    except ValueError as error:
        raise BadRequest("bad port") from error
    if not parsed.hostname or port is None:
        raise BadRequest("CONNECT needs host:port")
    return parsed.hostname, port


class EgressProxy:
    def __init__(
        self,
        allow: Sequence[Network],
        deny: Sequence[Network],
        *,
        resolver: Resolver = resolve,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.allow = list(allow)
        self.deny = list(deny)
        self._resolve = resolver
        self._slots = asyncio.Semaphore(max_connections)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self._slots:
            try:
                await self._serve(reader, writer)
            except BadRequest:
                await _reply(writer, 400, b"Bad request\n")
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_SECONDS)
        except asyncio.LimitOverrunError as error:
            raise BadRequest("header too large") from error
        lines = head.decode("latin-1").split("\r\n")[:-2]
        try:
            method, target, version = lines[0].split(" ")
        except ValueError as error:
            raise BadRequest("bad request line") from error

        if method.upper() == "CONNECT":
            host, port = _split_authority(target)
            request = None
        else:
            parsed = urlsplit(target)
            try:
                port = parsed.port or 80
            except ValueError as error:
                raise BadRequest("bad port") from error
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                raise BadRequest("only absolute http:// URLs or CONNECT")
            host = parsed.hostname
            path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
            kept = [
                line
                for line in lines[1:]
                if line.split(":", 1)[0].strip().lower() not in _HOP_BY_HOP
            ]
            request = "\r\n".join(
                [f"{method} {path} {version}", *kept, "Connection: close", "", ""]
            )

        address = await self._pick(host, port)
        if address is None:
            logger.warning("egress_denied host=%r port=%d", host, port)
            await _reply(writer, 403, DENIED_MESSAGE)
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), CONNECT_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError):
            await _reply(writer, 502, b"Could not connect to the destination\n")
            return
        logger.info("egress_allowed host=%r port=%d address=%s", host, port, address)
        try:
            if request is None:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                upstream_writer.write(request.encode("latin-1"))
            await _splice(reader, writer, upstream_reader, upstream_writer)
        finally:
            upstream_writer.close()

    async def _pick(self, host: str, port: int) -> str | None:
        try:
            addresses = await self._resolve(host, port)
        except OSError:
            return None
        return next((a for a in addresses if destination_allowed(a, self.allow, self.deny)), None)


async def _reply(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[status]
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    try:
        await writer.drain()
    except OSError:
        pass


async def _copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    while chunk := await source.read(COPY_CHUNK_BYTES):
        sink.write(chunk)
        await sink.drain()
    if sink.can_write_eof():
        sink.write_eof()  # half-close: the other direction may still be talking


async def _splice(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
) -> None:
    tasks = [
        asyncio.ensure_future(_copy(reader, upstream_writer)),
        asyncio.ensure_future(_copy(upstream_reader, writer)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def settings_from_env(env: dict[str, str]) -> tuple[list[Network], list[Network], int]:
    allow = parse_networks(
        env.get("SANDBOX_NETWORK_ALLOW_LIST") or env.get("DAYTONA_NETWORK_ALLOW_LIST") or ""
    )
    deny = parse_networks(env.get("DOCKER_SANDBOX_SUBNET_POOL") or "10.213.0.0/16")
    port = int(env.get("EGRESS_PROXY_PORT") or DEFAULT_PORT)
    return allow, deny, port


async def serve(env: dict[str, str]) -> None:
    allow, deny, port = settings_from_env(env)
    proxy = EgressProxy(allow, deny)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", port, limit=MAX_HEADER_BYTES)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # PID 1 ignores SIGTERM without a handler
        loop.add_signal_handler(sig, stop.set)
    logger.info(
        "egress_proxy_listening port=%d allow=%s deny=%s",
        port,
        ",".join(map(str, allow)) or "(none: every destination refused)",
        ",".join(map(str, deny)),
    )
    async with server:
        await stop.wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(dict(os.environ)))


if __name__ == "__main__":
    main()
