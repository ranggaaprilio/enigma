"""Shared TCP port probing against a sandbox.

Both the dev-server compatibility facade and the multi-service preview scan need to know
which of a set of candidate ports are actually bound inside the sandbox. This used to be a
duplicated ``python3 -c`` one-liner in ``dev_server.py`` and ``preview.py``; consolidated here
so a multi-port scan costs one sandbox round-trip instead of one per port.
"""

from collections.abc import Sequence

from app.models import Workspace
from app.services.sandbox import SandboxRuntime, SandboxRuntimeError

_PORT_SCAN_TIMEOUT_SECONDS = 5


def _build_port_scan_script(ports: Sequence[int]) -> str:
    ports_list = ", ".join(str(port) for port in ports)
    return (
        "python3 - <<'ENIGMA_PORTS_EOF'\n"
        "import socket\n"
        f"ports = [{ports_list}]\n"
        "open_ports = []\n"
        "for p in ports:\n"
        "    s = socket.socket()\n"
        "    s.settimeout(0.3)\n"
        "    if s.connect_ex(('127.0.0.1', p)) == 0:\n"
        "        open_ports.append(p)\n"
        "    s.close()\n"
        "print(' '.join(str(p) for p in open_ports))\n"
        "ENIGMA_PORTS_EOF"
    )


async def probe_ports(
    runtime: SandboxRuntime, workspace: Workspace, ports: Sequence[int]
) -> set[int]:
    """Return the subset of ``ports`` that are open (bound) inside the sandbox.

    One sandbox exec regardless of how many ports are checked, so scanning every candidate
    port for every running service does not cost one round-trip per port.
    """
    if not ports:
        return set()
    try:
        result = await runtime.exec(
            workspace,
            _build_port_scan_script(ports),
            cwd=workspace.workspace_path,
            timeout=_PORT_SCAN_TIMEOUT_SECONDS,
        )
    except SandboxRuntimeError:
        return set()
    tokens = (result.stdout or "").split()
    open_ports: set[int] = set()
    for token in tokens:
        try:
            open_ports.add(int(token))
        except ValueError:
            continue
    return open_ports


async def port_is_open(runtime: SandboxRuntime, workspace: Workspace, port: int) -> bool:
    return port in await probe_ports(runtime, workspace, [port])
