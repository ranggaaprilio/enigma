"""Relay a loopback-only server to the preview proxy, and to nothing else (Epic 04).

Daytona's preview edge enters a sandbox through its loopback, so a dev server listening on
``127.0.0.1`` (Vite's default) previews fine there. The Docker provider's preview proxy
dials the container's address on the sandbox bridge instead, which a loopback-only
listener never answers. ``DockerRuntime.expose_port`` runs ``RELAY_SCRIPT`` inside the
container: when the port listens on loopback only, a detached relay listens on the
container's network address and forwards to the loopback listener.

**Only the preview proxy may connect.** Every sandbox shares the bridge network, so a
relay open to any peer would hand a sibling tenant the service its owner kept on
loopback (and the editor, which runs ``--auth none``) without the proxy's HMAC check. The
relay accepts a connection only when its source address is one the proxy's name
(``DOCKER_PREVIEW_PROXY_HOST``, resolved on the sandbox network's DNS) currently has, and
re-resolves every second, so a restarted or scaled proxy keeps working. A sandbox cannot
spoof that source: ``cap_drop=ALL`` removes ``NET_RAW``, and only the daemon registers
names on the network.

The relay exits within a second of the app going away, so a relaunched app that binds
``0.0.0.0`` finds the port free. An app already listening off loopback is left alone, as
is a port nobody listens on. The script is standard-library Python 3, run as the sandbox
user via ``python3 -c``; the unit tests import it under a non-``__main__`` name.
"""

from __future__ import annotations

import re
import shlex

PORT_RELAY_TIMEOUT_SECONDS = 10

_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

RELAY_SCRIPT = r'''
import asyncio
import os
import socket
import sys
import time

LISTEN = "0A"
LOOPBACK_V4 = {"0100007F", "0000000000000000FFFF00000100007F"}
LOOPBACK_V6 = "00000000000000000000000001000000"
LOOPBACK = LOOPBACK_V4 | {LOOPBACK_V6}
TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
MARKER = b"enigma-port-relay"
CHUNK = 65536


def listeners(port, tables=TABLES):
    """The hex local addresses with a socket in LISTEN on ``port``."""
    found = set()
    for table in tables:
        try:
            with open(table) as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or fields[3] != LISTEN:
                continue
            address, _, hex_port = fields[1].rpartition(":")
            if int(hex_port, 16) == port:
                found.add(address.upper())
    return found


def hex_v4(address):
    """An IPv4 address the way /proc/net/tcp prints it (host byte order, little-endian)."""
    return "%08X" % int.from_bytes(socket.inet_aton(address), "little")


def relay_target(addresses):
    """The loopback address to forward to when the port listens on loopback only."""
    if not addresses or not addresses <= LOOPBACK:
        return None
    return "127.0.0.1" if addresses & LOOPBACK_V4 else "::1"


def read_pid(pidfile):
    try:
        with open(pidfile) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def relay_alive(pid):
    """A live process that is running this script: /tmp survives a container restart, and
    a pid from before it may now belong to anything (tmux, the dev server itself)."""
    if not alive(pid):
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return MARKER in handle.read()
    except OSError:
        return False


def claim(pidfile):
    """Atomically publish this process as the port's relay; False if one is live."""
    staging = "%s.%d" % (pidfile, os.getpid())
    with open(staging, "w") as handle:
        handle.write(str(os.getpid()))
    try:
        for _ in range(2):
            try:
                os.link(staging, pidfile)
                return True
            except FileExistsError:
                if relay_alive(read_pid(pidfile)):
                    return False
                try:
                    os.unlink(pidfile)
                except FileNotFoundError:
                    pass
        return False
    finally:
        os.unlink(staging)


async def resolve_v4(name):
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            name, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        return set()
    return {info[4][0] for info in infos}


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (OSError, RuntimeError):
        pass


async def start_relay(bind, port, target, target_port, peers):
    """Listen on ``bind:port``; forward a connection from an address in ``peers`` (a set the
    caller keeps current) to ``target:target_port``; drop any other."""

    async def handle(reader, writer):
        peer = (writer.get_extra_info("peername") or ("",))[0]
        if peer not in peers:
            writer.close()
            return
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(target, target_port)
        except OSError:
            writer.close()
            return
        try:
            await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        finally:
            upstream_writer.close()
            writer.close()

    return await asyncio.start_server(handle, bind, port, reuse_address=True)


async def supervise(port, bind, target, proxy_name):
    peers = await resolve_v4(proxy_name)
    server = await start_relay(bind, port, target, port, peers)
    async with server:
        while listeners(port) & LOOPBACK:
            await asyncio.sleep(1)
            current = await resolve_v4(proxy_name)
            peers.clear()
            peers.update(current)


def main(port, proxy_name):
    pidfile = "/tmp/enigma-port-relay-%d.pid" % port
    if relay_alive(read_pid(pidfile)):
        return 0
    target = relay_target(listeners(port))
    if target is None:
        return 0
    bind = socket.gethostbyname(socket.gethostname())
    if bind.startswith("127."):
        print("no sandbox-network address to relay on", file=sys.stderr)
        return 1
    first = os.fork()
    if first:
        os.waitpid(first, 0)
        # Return once the relay listens (bounded), so the proxy's next dial lands.
        wanted = hex_v4(bind)
        for _ in range(30):
            if wanted in listeners(port):
                break
            time.sleep(0.1)
        return 0
    # Detach: a new session survives the exec's process-group kill, and closing the
    # inherited stdio lets the exec's output stream end.
    os.setsid()
    if os.fork():
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    try:
        if claim(pidfile):
            try:
                asyncio.run(supervise(port, bind, target, proxy_name + "."))
            finally:
                if read_pid(pidfile) == os.getpid():
                    os.unlink(pidfile)
    finally:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]), sys.argv[2]))
'''


def port_relay_command(port: int, proxy_host: str) -> str:
    """``proxy_host`` is resolved inside the sandbox (absolute, so no search domain)."""
    if not _HOSTNAME.match(proxy_host):
        raise ValueError("The preview proxy host must be a plain hostname")
    return f"python3 -c {shlex.quote(RELAY_SCRIPT)} {int(port)} {shlex.quote(proxy_host)}"
