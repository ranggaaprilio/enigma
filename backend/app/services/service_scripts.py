"""Sandbox launch/kill/probe scripts, generalised to run per-service instead of the single
global `/tmp/enigma-dev.*` slot.

These were originally private methods on ``DevServerService`` (`_setup_command`, `_run_command`,
`_custom_command_script`, `_KILL_SCRIPT`, `_shell_quote`). Moved here, pure and parameterised by
``ServicePaths``, so both the compatibility facade (which still drives the *primary* service
through the legacy `/tmp/enigma-dev.*` paths, for byte-for-byte behaviour) and
`WorkspaceServiceManager` (which drives every other service through its own
`/tmp/enigma-svc-<id>.*` paths) share one implementation.

Every script keeps the generation-token guard: killtree kills children first, and the wrapper's
own trailing status write can still race a fresh launch between the child's death and the
parent's SIGTERM, so a stale write must never win.
"""

from dataclasses import dataclass
from uuid import UUID

# The single dev server this app has ever supported used these fixed paths; the *primary*
# service (the one `/dev/*` and `DevServerService` still expose) keeps using them so a workspace
# that had a dev server running before this migration is upgraded in place with no behaviour
# change, and existing log/status tooling keeps working unchanged.
LEGACY_STATUS_PATH = "/tmp/enigma-dev.status"
LEGACY_LOG_PATH = "/tmp/enigma-dev.log"
LEGACY_MISE_INSTALL_LOG_PATH = "/tmp/enigma-mise-install.log"
LEGACY_GEN_PATH = "/tmp/enigma-dev.gen"
LEGACY_RUN_PID_PATH = "/tmp/enigma-dev.pid"
LEGACY_SETUP_PID_PATH = "/tmp/enigma-dev-setup.pid"


@dataclass(frozen=True)
class ServicePaths:
    status: str
    log: str
    gen: str
    pid: str
    # Substring that appears in the wrapper shell's own command line (e.g. because the script
    # body mentions its own log/status paths), used to confirm a PID belongs to one of our
    # wrappers rather than an unrelated process that reused the same, since-recycled PID.
    grep_token: str
    # Only the primary/legacy path set has a distinct "environment setup" phase exposed via
    # `/dev/setup`, tracked by its own pid file so `run` and `setup` can be killed independently.
    setup_pid: str | None = None
    mise_install_log: str | None = None


def builtin_paths() -> ServicePaths:
    """The primary service's paths — unchanged from the pre-Epic-11 single-slot behaviour."""
    return ServicePaths(
        status=LEGACY_STATUS_PATH,
        log=LEGACY_LOG_PATH,
        gen=LEGACY_GEN_PATH,
        pid=LEGACY_RUN_PID_PATH,
        grep_token="enigma-dev",
        setup_pid=LEGACY_SETUP_PID_PATH,
        mise_install_log=LEGACY_MISE_INSTALL_LOG_PATH,
    )


def service_paths(service_id: UUID) -> ServicePaths:
    """Paths for any service that is not the primary — one private file set per service id."""
    token = f"enigma-svc-{service_id.hex}"
    base = f"/tmp/{token}"
    return ServicePaths(
        status=f"{base}.status",
        log=f"{base}.log",
        gen=f"{base}.gen",
        pid=f"{base}.pid",
        grep_token=token,
    )


def shell_quote(value: str) -> str:
    # Single-quote for the outer shell; the script body contains no single quotes.
    return "'" + value.replace("'", "'\\''") + "'"


def fresh_log_line(log_path: str) -> str:
    """Redirect the script into a *new* log file rather than truncating the old one in place.

    The live log stream follows the file with `tail -F`, which notices an in-place truncation
    only if it looks before the file has grown back past its old size; otherwise it carries on
    from the old offset and silently drops the start of the restarted service's output. A new
    inode is always noticed ("has been replaced; following new file") and read from the top.
    """
    return f"rm -f {log_path}\nexec >{log_path} 2>&1\n"


def build_mise_setup_script(
    paths: ServicePaths,
    gen: str,
    *,
    mise_auto_install: bool,
    mise_install_url: str,
) -> str:
    """Background script that installs the mise toolchain + project dependencies, then stops
    at `ready`. Deliberately does not run the `dev` task — that is `build_mise_run_script`."""
    install_line = ""
    if mise_auto_install:
        install_line = (
            "if ! command -v mise >/dev/null 2>&1; then "
            "echo '[setup] installing mise'; "
            f"curl -fsSL {mise_install_url} | sh; fi\n"
        )
    pid_path = paths.setup_pid or paths.pid
    script = (
        f"{fresh_log_line(paths.log)}"
        "set -x\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        'export MISE_INSTALL_PATH="$HOME/.local/bin/mise"\n'
        f"echo {gen} > {paths.gen}\n"
        f"echo $$ > {pid_path}\n"
        f'setst() {{ [ "$(cat {paths.gen} 2>/dev/null)" = {gen} ] && '
        f'echo "$1" > {paths.status}; }}\n'
        "setst installing\n"
        f"{install_line}"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        "command -v mise || "
        "{ echo '[setup] mise not found on PATH'; setst error; exit 127; }\n"
        "mise trust || echo '[setup] mise trust failed (continuing)'\n"
        "mise install || "
        "{ echo '[setup] mise install failed'; setst error; exit 1; }\n"
        "if mise task ls 2>/dev/null | grep -qw install; then "
        "echo '[setup] installing project dependencies'; "
        "mise run install || "
        "{ echo '[setup] dependency install failed'; setst error; exit 1; }; "
        "fi\n"
        "echo '[setup] environment ready'; setst ready\n"
    )
    return f"nohup sh -c {shell_quote(script)} >/dev/null 2>&1 &"


def build_mise_run_script(paths: ServicePaths, gen: str, *, install_deps: bool) -> str:
    """Background script that runs the app's `dev` mise task. Writes `exited:<code>` (instead
    of a bare `stopped`) so a service that exits on its own is distinguishable from one an
    explicit Stop killed."""
    deps_block = ""
    if install_deps:
        deps_block = (
            "if mise task ls 2>/dev/null | grep -qw install; then "
            "echo '[run] installing project dependencies'; "
            "mise run install || "
            "{ echo '[run] dependency install failed'; setst error; exit 1; }; "
            "fi\n"
        )
    script = (
        f"{fresh_log_line(paths.log)}"
        "set -x\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        'export MISE_INSTALL_PATH="$HOME/.local/bin/mise"\n'
        f"echo {gen} > {paths.gen}\n"
        f"echo $$ > {paths.pid}\n"
        f'setst() {{ [ "$(cat {paths.gen} 2>/dev/null)" = {gen} ] && '
        f'echo "$1" > {paths.status}; }}\n'
        "setst launching\n"
        "command -v mise || "
        "{ echo '[run] mise not found on PATH'; setst error; exit 127; }\n"
        "mise trust || echo '[run] mise trust failed (continuing)'\n"
        "mise install || { echo '[run] mise install failed'; setst error; exit 1; }\n"
        f"{deps_block}"
        f'[ "$(cat {paths.gen} 2>/dev/null)" = {gen} ] || '
        "{ echo '[run] superseded before start'; exit 0; }\n"
        "if mise task ls 2>/dev/null | grep -qw dev; then "
        "setst running; "
        "mise run dev; "
        'rc=$?; setst "exited:$rc"; '
        "else echo '[run] no dev task found'; setst no-dev-task; fi\n"
    )
    return f"nohup sh -c {shell_quote(script)} >/dev/null 2>&1 &"


def build_registry_script(
    paths: ServicePaths,
    gen: str,
    *,
    command: str,
    install_command: str | None,
    install_cwd: str | None = None,
    serve: bool,
) -> str:
    """Background script that runs a **registry** command instead of a mise task.

    Structurally identical to `build_mise_run_script` — same log file, same generation-token
    guard, same status vocabulary — so status polling and `ensure_running` work unchanged. mise
    is best-effort here: a registry-driven service need not have a mise config at all.
    """
    bootstrap = (
        "if command -v mise >/dev/null 2>&1; then "
        "mise trust >/dev/null 2>&1 || true; "
        "mise install >/dev/null 2>&1 || true; fi\n"
    )
    install_block = ""
    if install_command:
        install_line = f"sh -lc {shell_quote(install_command)}"
        if install_cwd:
            install_line = f"( cd {shell_quote(install_cwd)} && {install_line} )"
        install_block = (
            "echo '[run] installing project dependencies'\n"
            f"{install_line} || "
            "{ echo '[run] dependency install failed'; setst error; exit 1; }\n"
        )
    if serve:
        tail = (
            "setst running\n"
            f"sh -lc {shell_quote(command)}\n"
            'rc=$?; setst "exited:$rc"\n'
        )
    else:
        tail = "echo '[setup] environment ready'; setst ready\n"
    pid_path = paths.pid if serve else (paths.setup_pid or paths.pid)
    script = (
        f"{fresh_log_line(paths.log)}"
        "set -x\n"
        'export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"\n'
        'export MISE_INSTALL_PATH="$HOME/.local/bin/mise"\n'
        f"echo {gen} > {paths.gen}\n"
        f"echo $$ > {pid_path}\n"
        f'setst() {{ [ "$(cat {paths.gen} 2>/dev/null)" = {gen} ] && '
        f'echo "$1" > {paths.status}; }}\n'
        f"setst {'launching' if serve else 'installing'}\n"
        f"{bootstrap}"
        f"{install_block}"
        f'[ "$(cat {paths.gen} 2>/dev/null)" = {gen} ] || '
        "{ echo '[run] superseded before start'; exit 0; }\n"
        f"{tail}"
    )
    return f"nohup sh -c {shell_quote(script)} >/dev/null 2>&1 &"


def kill_script(paths: ServicePaths, *, legacy_pkill: bool = False) -> str:
    """Kill the wrapper shell(s) recorded in ``paths`` together with everything they spawned
    (children first, so a killed `sh -c` cannot leave its server orphaned).

    ``legacy_pkill=True`` (the primary/builtin service only) additionally falls back to the
    historical `mise run dev` / `mise install` patterns, for a server started before PID files
    existed. A non-primary service never pkills by pattern — a broad kill would also hit an
    unrelated, in-flight launch (e.g. stopping "API" must never kill a concurrent `/dev/setup`).
    """
    pid_files = [paths.pid, *([paths.setup_pid] if paths.setup_pid else [])]
    script = (
        'killtree() { for child in $(pgrep -P "$1" 2>/dev/null); do killtree "$child"; done; '
        'kill "$1" >/dev/null 2>&1 || true; }; '
        + "; ".join(
            f'pid=$(cat {pidfile} 2>/dev/null); [ -n "$pid" ] && killtree "$pid"; rm -f {pidfile}'
            for pidfile in pid_files
        )
    )
    if legacy_pkill:
        script += (
            "; pkill -f '[m]ise run dev' >/dev/null 2>&1 || true"
            "; pkill -f '[m]ise install' >/dev/null 2>&1 || true"
            "; pkill -f '[m]ise run install' >/dev/null 2>&1 || true"
        )
    return script


@dataclass(frozen=True)
class ProbeRow:
    id: str
    paths: ServicePaths
    port: int | None
    # Epic 13: root-relative HTTP path to probe for readiness (``CustomCommand.health_path``).
    # ``None`` means only the TCP connect check applies -- the health field then stays
    # ``"unknown"`` forever, which is the correct, honest answer for a service nobody configured
    # a health check for.
    health_path: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    status: str
    pid_alive: bool
    port_open: bool
    # Defaulted so every pre-Epic-13 construction site (including a 4-field probe line from a
    # skewed process, though script and parser always run in the same process here) still works.
    health: str = "unknown"


def build_probe_script(rows: list[ProbeRow], *, health_timeout: float = 1.5) -> str:
    """One sandbox exec that reports every row's status file, pid liveness, port and (Epic 13)
    HTTP health, so N services cost one round-trip instead of N.

    Health is only attempted for a row with both a ``health_path`` and an open port -- a closed
    port cannot possibly answer HTTP, so it costs nothing beyond the TCP check already done.
    """
    calls = "\n".join(
        f"probe({row.id!r}, {row.paths.status!r}, {row.paths.pid!r}, "
        f"{row.paths.grep_token!r}, {row.port!r}, {row.health_path!r})"
        for row in rows
    )
    return (
        "python3 - <<'ENIGMA_PROBE_EOF'\n"
        "import http.client\n"
        "import os\n"
        "import socket\n"
        "import subprocess\n"
        "\n"
        "\n"
        "def check_health(port, path, timeout):\n"
        "    # http.client, never urllib: urlopen installs a redirect handler and would follow\n"
        "    # a 30x, which could turn a loopback probe into an outbound request. This reads\n"
        "    # only the status line, follows no redirect, and always closes the connection.\n"
        "    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)\n"
        "    try:\n"
        "        conn.request(\n"
        "            'GET', path,\n"
        "            headers={\n"
        "                'Host': '127.0.0.1:%d' % port,\n"
        "                'User-Agent': 'enigma-health/1',\n"
        "            },\n"
        "        )\n"
        "        code = conn.getresponse().status\n"
        "    except Exception:\n"
        "        return 'unhealthy'\n"
        "    finally:\n"
        "        conn.close()\n"
        "    # A redirect (e.g. '/' -> '/login') still means the app is serving.\n"
        "    return 'healthy' if 200 <= code < 400 else 'unhealthy'\n"
        "\n"
        "\n"
        "def probe(rid, status_path, pid_path, grep_token, port, health_path):\n"
        "    try:\n"
        "        with open(status_path) as fh:\n"
        "            status = fh.read().strip() or 'none'\n"
        "    except OSError:\n"
        "        status = 'none'\n"
        "    pid_alive = 0\n"
        "    try:\n"
        "        with open(pid_path) as fh:\n"
        "            pid = fh.read().strip()\n"
        "    except OSError:\n"
        "        pid = ''\n"
        "    if pid:\n"
        "        try:\n"
        "            os.kill(int(pid), 0)\n"
        "            args = subprocess.run(\n"
        "                ['ps', '-o', 'args=', '-p', pid], capture_output=True, text=True\n"
        "            ).stdout\n"
        "            if grep_token in args:\n"
        "                pid_alive = 1\n"
        "        except (ValueError, ProcessLookupError, PermissionError, OSError):\n"
        "            pid_alive = 0\n"
        "    if not pid_alive:\n"
        "        # No pid file, a stale one, or a pid that no longer belongs to our wrapper\n"
        "        # (its file was lost across a sandbox restart, or it predates pid tracking\n"
        "        # entirely) — fall back to a system-wide search for the wrapper's own marker\n"
        "        # in any process's command line.\n"
        "        try:\n"
        "            pid_alive = 1 if subprocess.run(\n"
        "                ['pgrep', '-f', grep_token], capture_output=True, text=True\n"
        "            ).returncode == 0 else 0\n"
        "        except OSError:\n"
        "            pid_alive = 0\n"
        "    port_open = 0\n"
        "    if port:\n"
        "        s = socket.socket()\n"
        "        s.settimeout(0.3)\n"
        "        port_open = 1 if s.connect_ex(('127.0.0.1', port)) == 0 else 0\n"
        "        s.close()\n"
        "    health = 'unknown'\n"
        "    if port_open and port and health_path:\n"
        f"        health = check_health(port, health_path, {health_timeout!r})\n"
        "    print(rid, status, pid_alive, port_open, health)\n"
        "\n"
        f"{calls}\n"
        "ENIGMA_PROBE_EOF"
    )


def parse_probe_output(output: str) -> dict[str, ProbeResult]:
    """Tolerant of both the pre- and post-Epic-13 line shape: 5 fields carry a health token, 4
    fields (a stale probe script from a different process, in principle -- in practice this
    codebase always runs the script and the parser from the very same request) default it to
    ``"unknown"``. Anything else is not a probe line and is skipped."""
    results: dict[str, ProbeResult] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 5:
            rid, status, pid_alive, port_open, health = parts
        elif len(parts) == 4:
            rid, status, pid_alive, port_open = parts
            health = "unknown"
        else:
            continue
        results[rid] = ProbeResult(
            status=status,
            pid_alive=pid_alive == "1",
            port_open=port_open == "1",
            health=health,
        )
    return results
