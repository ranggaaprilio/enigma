"""``RuntimeFileSystem`` for the local Docker provider: every operation is a ``docker exec``
**as the sandbox user**.

Never the daemon's archive API: ``get_archive``/``put_archive`` run as root inside the
container and follow symlinks, so a repository that commits ``docs -> /etc`` would let a
write land in (or a read come from) root-owned files. An exec as the sandbox user leaves
permissions and symlink resolution to the kernel. Downloads are ``head -c <cap+1>`` (memory
bounded, directories refused); uploads stream the bytes over the exec's stdin into
``head -c <n> > path``, which needs no end-of-file and keeps an existing file's mode/owner.

Every helper runs an **argv array** — user paths are separate arguments after ``--`` (or
``$1`` of a constant ``sh -c`` script), never spliced into a shell string. Results mirror
the Daytona SDK: entries carry ``name``/``is_dir``/``size``/``mod_time``/``mode``/
``permissions``/``owner``/``group`` (plus ``is_symlink``, which Daytona never sets, so
``files.py`` refuses to open a symlink here), and ``find_files`` is a fixed-string
*content* search returning ``file``/``line``/``content`` matches.

Failures on a path (missing file, permission denied) raise ``OSError`` subclasses, not
``SandboxRuntimeError``: like the Daytona SDK's own errors they are per-file problems,
and consumers map ``SandboxRuntimeError`` to "the sandbox is unavailable". Daemon
failures still surface as ``SandboxRuntimeError`` through ``DockerRuntime._run``.
"""

from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from posixpath import basename, dirname, join
from typing import TYPE_CHECKING, Any

from app.services.sandbox.docker_pty import close_exec_socket

if TYPE_CHECKING:
    from app.services.sandbox.docker import DockerRuntime

# Upper bound on one download held in backend memory (callers apply their own, smaller
# limits first: ``FILE_MAX_READ_BYTES`` for the Files panel).
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
# Upper bound on ``find_files`` output read back, and on how long the search may run.
MAX_SEARCH_OUTPUT_BYTES = 8 * 1024 * 1024
SEARCH_TIMEOUT_SECONDS = 60
# A stalled upload stream (daemon or container wedged) must not pin a thread forever.
UPLOAD_SOCKET_TIMEOUT_SECONDS = 120
_NOT_REGULAR = "not a regular file"
_MODE_PATTERN = re.compile(r"^[0-7]{3,4}$")
# type, target type, size, mtime, octal perms, symbolic mode, owner, group, path
FIND_FORMAT = "%y\\0%Y\\0%s\\0%T@\\0%m\\0%M\\0%u\\0%g\\0%p\\0"
_FIND_FIELDS = 9
# $1 = path, $2 = byte cap. ``-f`` follows a final symlink, as Daytona does; the kernel
# decides whether the sandbox user may read the target.
READ_FILE = (
    f'[ -f "$1" ] || {{ [ -e "$1" ] && echo "{_NOT_REGULAR}" >&2 '
    '|| echo "No such file or directory" >&2; exit 1; }; '
    # timeout: a FIFO swapped in after the -f check must not pin a thread forever.
    f'exec timeout {SEARCH_TIMEOUT_SECONDS} head -c "$2" -- "$1"'
)
# $1 = path, $2 = parent, $3 = exact byte count arriving on stdin.
WRITE_FILE = 'mkdir -p -- "$2" && head -c "$3" > "$1"'
# $1 = pattern, $2 = path, $3 = output cap. -H: name the file even for a single-file path.
SEARCH = (
    '[ -e "$2" ] || { echo "No such file or directory" >&2; exit 2; }; '
    f'timeout {SEARCH_TIMEOUT_SECONDS} grep -rnHIF --null -e "$1" -- "$2" 2>/dev/null '
    '| head -c "$3"; exit 0'
)


@dataclass(frozen=True)
class DockerFileInfo:
    name: str
    is_dir: bool
    size: int
    mod_time: str
    mode: str
    permissions: str
    owner: str
    group: str
    is_symlink: bool = False


@dataclass(frozen=True)
class DockerMatch:
    file: str
    line: int
    content: str


def _validate_mode(mode: str) -> str:
    if not _MODE_PATTERN.fullmatch(str(mode)):
        raise ValueError(f"invalid file mode: {mode!r}")
    return str(mode)


def _error(path: str, stderr: bytes) -> OSError:
    lines = stderr.decode("utf-8", errors="replace").strip().splitlines()
    detail = lines[-1][:300] if lines else "operation failed"
    if "No such file or directory" in detail:
        return FileNotFoundError(f"{path}: not found")
    if _NOT_REGULAR in detail:
        return IsADirectoryError(f"{path}: {_NOT_REGULAR}")
    if "Permission denied" in detail:
        return PermissionError(f"{path}: permission denied")
    return OSError(detail)


def _recv_exact(raw: Any, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = raw.recv(size - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def read_stderr_frames(raw: Any, limit: int = 64 * 1024) -> bytes:
    """Drain a multiplexed (non-TTY) exec stream until EOF; keep only stderr, bounded.
    Each frame is ``[stream, 0, 0, 0, size:uint32be]`` followed by ``size`` bytes."""
    stderr = bytearray()
    while header := _recv_exact(raw, 8):
        if len(header) < 8:
            break
        stream, size = header[0], struct.unpack(">I", header[4:])[0]
        payload = _recv_exact(raw, size)
        if stream == 2 and len(stderr) < limit:
            stderr.extend(payload[: limit - len(stderr)])
        if len(payload) < size:
            break
    return bytes(stderr)


def wait_for_exit_code(api: Any, exec_id: str) -> int | None:
    """The stream can end a little before the daemon records the exit code."""
    inspected = api.exec_inspect(exec_id)
    for _ in range(100):
        if not inspected.get("Running") and inspected.get("ExitCode") is not None:
            break
        time.sleep(0.05)
        inspected = api.exec_inspect(exec_id)
    return inspected.get("ExitCode")


def _text(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def parse_find_records(output: bytes) -> list[DockerFileInfo]:
    """Parse ``find -printf FIND_FORMAT`` output (NUL-separated, so any filename —
    spaces, newlines, quotes — round-trips)."""
    fields = output.split(b"\0")
    entries: list[DockerFileInfo] = []
    for start in range(0, len(fields) - _FIND_FIELDS + 1, _FIND_FIELDS):
        kind, target, size, mtime, perms, symbolic, owner, group, path = (
            _text(value) for value in fields[start : start + _FIND_FIELDS]
        )
        modified = datetime.fromtimestamp(float(mtime or 0), UTC)
        entries.append(
            DockerFileInfo(
                name=basename(path.rstrip("/")) or path,
                is_dir=(target if kind == "l" else kind) == "d",
                size=int(size or 0),
                mod_time=modified.isoformat().replace("+00:00", "Z"),
                mode=symbolic,
                permissions=perms.zfill(4),
                owner=owner,
                group=group,
                is_symlink=kind == "l",
            )
        )
    return entries


def parse_grep_matches(output: bytes) -> list[DockerMatch]:
    """Parse ``grep -n --null`` output: ``<file>\\0<line>:<content>\\n`` per match."""
    matches: list[DockerMatch] = []
    position = 0
    while position < len(output):
        file_end = output.find(b"\0", position)
        colon = output.find(b":", file_end + 1) if file_end >= 0 else -1
        if colon < 0:
            break
        newline = output.find(b"\n", colon + 1)
        if newline < 0:
            newline = len(output)
        matches.append(
            DockerMatch(
                file=_text(output[position:file_end]),
                line=int(output[file_end + 1 : colon]),
                content=output[colon + 1 : newline].decode("utf-8", errors="replace"),
            )
        )
        position = newline + 1
    return matches


class DockerFileSystem:
    """One workspace container's files. Built by ``DockerRuntime.fs`` per service call."""

    def __init__(self, runtime: DockerRuntime, container: Any) -> None:
        self._runtime = runtime
        self._container = container

    def _abs(self, path: str) -> str:
        """Relative paths resolve against the sandbox home, as Daytona's do. Always
        absolute also means a path never starts with ``-`` — ``find`` has no ``--``, so an
        argument like ``-delete`` must be impossible to build."""
        return path if path.startswith("/") else join(self._runtime._home(), path)

    async def _exec(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        def run() -> tuple[int, bytes, bytes]:
            result = self._container.exec_run(argv, demux=True)
            stdout, stderr = result.output or (None, None)
            return result.exit_code, stdout or b"", stderr or b""

        # The exec pool, not the control pool: a file tree fans out many of these, and
        # kills must never queue behind them.
        return await self._runtime._run("exec", run)

    async def _checked(self, path: str, argv: list[str]) -> bytes:
        code, stdout, stderr = await self._exec(argv)
        if code != 0:
            raise _error(path, stderr)
        return stdout

    async def list_files(self, path: str) -> list[DockerFileInfo]:
        path = self._abs(path)
        # -H: a symlinked directory lists its target's contents, as Daytona's does.
        output = await self._checked(
            path,
            ["find", "-H", path, "-mindepth", "1", "-maxdepth", "1", "-printf", FIND_FORMAT],
        )
        return parse_find_records(output)

    async def get_file_info(self, path: str) -> DockerFileInfo:
        path = self._abs(path)
        output = await self._checked(path, ["find", path, "-maxdepth", "0", "-printf", FIND_FORMAT])
        entries = parse_find_records(output)
        if not entries:
            raise FileNotFoundError(f"{path}: not found")
        return entries[0]

    async def download_file(self, path: str) -> bytes:
        path = self._abs(path)
        argv = ["/bin/sh", "-c", READ_FILE, "sh", path, str(MAX_DOWNLOAD_BYTES + 1)]
        client, exec_api = await self._runtime._clients()
        container_id = self._container.id

        def read() -> tuple[int | None, bytes, bytes]:
            # Streamed and joined once: ``exec_run(demux=True)`` concatenates frame by
            # frame, which is quadratic — seconds of CPU (holding the GIL) for 64 MiB.
            created = client.api.exec_create(container_id, argv)
            chunks: list[bytes] = []
            stderr = bytearray()
            for out, err in exec_api.exec_start(created["Id"], stream=True, demux=True):
                if out:
                    chunks.append(out)
                if err and len(stderr) < 64 * 1024:
                    stderr.extend(err)
            return wait_for_exit_code(client.api, created["Id"]), b"".join(chunks), bytes(stderr)

        code, data, stderr = await self._runtime._run("exec", read)
        if code != 0:
            raise _error(path, stderr)
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise OSError(f"{path}: file is too large to download")
        return data

    async def upload_file(self, content: bytes, path: str) -> None:
        path = self._abs(path)
        argv = ["/bin/sh", "-c", WRITE_FILE, "sh", path, dirname(path) or "/", str(len(content))]
        client, exec_api = await self._runtime._clients()
        container_id = self._container.id

        def write() -> tuple[int | None, bytes, bool]:
            created = client.api.exec_create(container_id, argv, stdin=True)
            sock = exec_api.exec_start(created["Id"], socket=True)
            raw = getattr(sock, "_sock", sock)
            raw.settimeout(UPLOAD_SOCKET_TIMEOUT_SECONDS)
            sent = True
            try:
                try:
                    raw.sendall(content)
                except OSError:  # the command exited before reading (its stderr says why)
                    sent = False
                stderr = read_stderr_frames(raw)
            finally:
                close_exec_socket(sock)
            return wait_for_exit_code(client.api, created["Id"]), stderr, sent

        code, stderr, sent = await self._runtime._run("exec", write)
        if code != 0:
            raise _error(path, stderr)
        if not sent:
            # The stream broke mid-way yet ``head`` saw EOF and exited 0: the file is short.
            raise OSError(f"{path}: upload was interrupted")

    async def create_folder(self, path: str, mode: str) -> None:
        path = self._abs(path)
        # Like Daytona's MkdirAll: an existing folder keeps its permissions.
        await self._checked(path, ["mkdir", "-p", "-m", _validate_mode(mode), "--", path])

    async def move_files(self, source: str, destination: str) -> None:
        source, destination = self._abs(source), self._abs(destination)
        # -T: a rename, never "move into" an existing directory.
        await self._checked(source, ["mv", "-T", "--", source, destination])

    async def delete_file(self, path: str, recursive: bool = False) -> None:
        path = self._abs(path)
        await self._checked(path, ["rm", "-r" if recursive else "-d", "--", path])

    async def find_files(self, path: str, pattern: str) -> list[DockerMatch]:
        path = self._abs(path)
        if "\n" in pattern:
            return []  # no line contains a newline (and grep -F would split it into two)
        output = await self._checked(
            path, ["/bin/sh", "-c", SEARCH, "sh", pattern, path, str(MAX_SEARCH_OUTPUT_BYTES)]
        )
        if len(output) >= MAX_SEARCH_OUTPUT_BYTES:
            output = output[: output.rfind(b"\n") + 1]  # drop the record the cap cut
        return parse_grep_matches(output)

    async def set_file_permissions(self, path: str, *, mode: str) -> None:
        path = self._abs(path)
        await self._checked(path, ["chmod", _validate_mode(mode), "--", path])


__all__ = [
    "MAX_DOWNLOAD_BYTES",
    "MAX_SEARCH_OUTPUT_BYTES",
    "DockerFileInfo",
    "DockerFileSystem",
    "DockerMatch",
]
