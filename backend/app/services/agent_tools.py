import asyncio
import difflib
import shlex
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import structlog

from app.models import Workspace
from app.services.daytona_runtime import DaytonaRuntime
from app.services.repositories import (
    GitFileStatus,
    RepositoryPathError,
    parse_porcelain_status,
    sanitize_git_output,
    validate_diff_path,
)

logger = structlog.get_logger(__name__)

EXCLUDED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
DANGEROUS_COMMANDS = {
    "chmod",
    "chown",
    "curl",
    "dd",
    "docker",
    "docker-compose",
    "git",
    "mkfs",
    "mount",
    "npm",
    "pnpm",
    "rm",
    "sudo",
    "su",
    "uv",
    "wget",
    "yarn",
}
DANGEROUS_TOKENS = {"&&", "||", ";", "|", ">", ">>", "<", "$(", "`"}


class WorkspaceToolError(Exception):
    pass


class DangerousCommandError(WorkspaceToolError):
    pass


class PatchError(WorkspaceToolError):
    pass


SEARCH_MARKER = "<<<<<<< SEARCH"
DIVIDER_MARKER = "======="
REPLACE_MARKER = ">>>>>>> REPLACE"
MAX_SEARCH_QUERY_CHARS = 1000
MAX_SEARCH_LINE_CHARS = 500


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


@dataclass(frozen=True)
class PatchResult:
    path: str
    diff: str
    blocks_applied: int


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line_number: int
    text: str


@dataclass(frozen=True)
class SearchResult:
    matches: list[SearchMatch]
    truncated: bool


@dataclass(frozen=True)
class GitStatusResult:
    branch: str | None
    is_clean: bool
    files: list[GitFileStatus]


class WorkspaceTools:
    def __init__(
        self,
        workspace: Workspace,
        runtime: DaytonaRuntime,
        command_timeout_seconds: int,
        max_command_output_chars: int,
        working_dir: str | None = None,
        allowlist_enabled: bool = False,
        allowed_commands: frozenset[str] | None = None,
        deny_dangerous: bool = True,
    ) -> None:
        self.workspace = workspace
        self.runtime = runtime
        self.command_timeout_seconds = command_timeout_seconds
        self.max_command_output_chars = max_command_output_chars
        # Root the tools at an explicit working directory (e.g. a feature
        # worktree such as "worktrees/001-feature") when provided, otherwise the
        # base clone. All path resolution stays confined to this root.
        self.root = working_dir if working_dir is not None else workspace.workspace_path
        # When allowlist mode is enabled, run_command accepts only programs in
        # ``allowed_commands`` (the denylist is superseded). When disabled the
        # legacy DANGEROUS_COMMANDS denylist applies. Shell-operator and
        # absolute-path checks always apply in both modes.
        self.allowlist_enabled = allowlist_enabled
        self.allowed_commands = frozenset(allowed_commands or ())
        self.deny_dangerous = deny_dangerous

    def resolve_path(self, path: str | None = None) -> str:
        candidate = (path or "").strip().strip("/")
        pure = PurePosixPath(candidate)
        if pure.is_absolute() or ".." in pure.parts:
            raise WorkspaceToolError("Path is outside the workspace")
        if any(part in EXCLUDED_NAMES for part in pure.parts):
            raise WorkspaceToolError("Path is excluded")
        return self.root if not candidate else f"{self.root}/{pure.as_posix()}"

    async def list_files(self, path: str | None = None, limit: int = 200) -> list[str]:
        sandbox = await self.runtime.get_sandbox(self.workspace)
        root = self.resolve_path(path)
        bounded_limit = max(1, min(limit, 200))
        results: list[str] = []
        prefix = self.root.rstrip("/") + "/"
        directories = deque([root])

        try:
            while directories and len(results) < bounded_limit:
                directory = directories.popleft()
                async with asyncio.timeout(self.command_timeout_seconds):
                    entries = await sandbox.fs.list_files(directory)
                entries = sorted(entries, key=lambda entry: self._entry_name(entry).lower())
                for entry in entries:
                    remote = self._entry_path(directory, entry)
                    relative = remote[len(prefix) :] if remote.startswith(prefix) else remote
                    relative_path = PurePosixPath(relative)
                    if any(part in EXCLUDED_NAMES for part in relative_path.parts):
                        continue
                    if self._entry_is_dir(entry):
                        directories.append(remote)
                    else:
                        results.append(relative_path.as_posix())
                        if len(results) >= bounded_limit:
                            break
        except Exception as error:
            raise WorkspaceToolError(str(error)) from error
        return results

    @staticmethod
    def _entry_name(entry: Any) -> str:
        name = getattr(entry, "name", None)
        if name:
            return str(name)
        path = str(getattr(entry, "path", getattr(entry, "file", "")))
        return PurePosixPath(path).name

    @classmethod
    def _entry_path(cls, directory: str, entry: Any) -> str:
        path = str(getattr(entry, "path", getattr(entry, "file", "")))
        if path:
            return path
        return f"{directory.rstrip('/')}/{cls._entry_name(entry)}"

    @staticmethod
    def _entry_is_dir(entry: Any) -> bool:
        value = getattr(entry, "is_dir", False)
        return bool(value() if callable(value) else value)

    async def read_file(self, path: str, max_chars: int = 20_000) -> str:
        sandbox = await self.runtime.get_sandbox(self.workspace)
        try:
            content = await sandbox.fs.download_file(self.resolve_path(path))
        except Exception as error:
            raise WorkspaceToolError("File does not exist") from error
        return (content or b"").decode("utf-8", errors="replace")[:max_chars]

    async def _download_text(self, path: str) -> str:
        # Non-truncating read used by apply_patch: read_file caps to max_chars,
        # which would silently corrupt a large file when written back.
        sandbox = await self.runtime.get_sandbox(self.workspace)
        try:
            content = await sandbox.fs.download_file(self.resolve_path(path))
        except Exception as error:
            raise WorkspaceToolError("File does not exist") from error
        return (content or b"").decode("utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> str:
        sandbox = await self.runtime.get_sandbox(self.workspace)
        remote = self.resolve_path(path)
        parent = str(PurePosixPath(remote).parent)
        await sandbox.fs.create_folder(parent, "755")
        await sandbox.fs.upload_file(content.encode("utf-8"), remote)
        return PurePosixPath(path).as_posix()

    async def apply_patch(self, path: str, patch: str) -> PatchResult:
        # Apply one or more SEARCH/REPLACE blocks against an existing file. Each
        # SEARCH section must match the current content exactly and uniquely;
        # otherwise we fail loudly without writing anything (no destructive
        # overwrite). All blocks are applied in memory and uploaded once.
        blocks = self._parse_search_replace(patch)
        original = await self._download_text(path)
        updated = original
        for index, (search, replace) in enumerate(blocks):
            count = updated.count(search)
            if count == 0:
                raise PatchError(
                    f"Block {index + 1}: search context not found, no changes made"
                )
            if count > 1:
                raise PatchError(
                    f"Block {index + 1}: ambiguous match, {count} occurrences found, "
                    "no changes made"
                )
            updated = updated.replace(search, replace, 1)
        relative = PurePosixPath(path).as_posix()
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        await self.write_file(path, updated)
        return PatchResult(path=relative, diff=diff, blocks_applied=len(blocks))

    @staticmethod
    def _parse_search_replace(patch: str) -> list[tuple[str, str]]:
        if not patch or not patch.strip():
            raise PatchError("Patch is empty")
        lines = patch.splitlines()
        blocks: list[tuple[str, str]] = []
        i = 0
        while i < len(lines):
            if lines[i].rstrip() != SEARCH_MARKER:
                if lines[i].strip():
                    raise PatchError("Malformed patch: expected '<<<<<<< SEARCH'")
                i += 1
                continue
            i += 1
            search_lines: list[str] = []
            while i < len(lines) and lines[i].rstrip() != DIVIDER_MARKER:
                if lines[i].rstrip() == REPLACE_MARKER:
                    raise PatchError("Malformed patch: missing '=======' divider")
                search_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise PatchError("Malformed patch: missing '=======' divider")
            i += 1  # skip divider
            replace_lines: list[str] = []
            while i < len(lines) and lines[i].rstrip() != REPLACE_MARKER:
                if lines[i].rstrip() == SEARCH_MARKER:
                    raise PatchError("Malformed patch: missing '>>>>>>> REPLACE' marker")
                replace_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise PatchError("Malformed patch: missing '>>>>>>> REPLACE' marker")
            i += 1  # skip replace marker
            blocks.append(("\n".join(search_lines), "\n".join(replace_lines)))
        if not blocks:
            raise PatchError("Patch contains no SEARCH/REPLACE blocks")
        return blocks

    async def search_code(
        self,
        query: str,
        path_glob: str | None = None,
        max_results: int = 200,
    ) -> SearchResult:
        # ripgrep-style search scoped to the workspace root. Runs via
        # runtime.exec directly (the query may contain regex characters that
        # run_command's DANGEROUS_TOKENS check would reject). Falls back to grep
        # when ripgrep is unavailable.
        if not query or not query.strip():
            raise WorkspaceToolError("Search query is empty")
        query = query[:MAX_SEARCH_QUERY_CHARS]
        bounded = max(1, min(max_results, 1000))

        rg = [
            "rg",
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--max-count",
            str(bounded),
            "-e",
            query,
        ]
        for name in EXCLUDED_NAMES:
            rg += ["-g", f"!{name}"]
        if path_glob:
            rg += ["-g", path_glob]
        # Explicit search path is required. With no path, ripgrep searches the
        # current directory only when stdin is a TTY; under a non-interactive
        # exec (stdin is not a TTY) it reads from the empty stdin instead and
        # silently matches nothing. Passing "." forces a directory search.
        rg.append(".")
        result = await self._exec_search(rg)

        if self._is_command_missing(result):
            grep = ["grep", "-rIn"]
            for name in EXCLUDED_NAMES:
                grep.append(f"--exclude-dir={name}")
            if path_glob:
                grep.append(f"--include={path_glob}")
            grep += ["-e", query, "."]
            result = await self._exec_search(grep)

        # rg/grep exit 1 == "no matches" (success, empty result); exit >=2 is a
        # real error. Treat anything else (and the no-match case) as non-fatal.
        return self._parse_search_output(result.stdout, bounded)

    async def _exec_search(self, argv: list[str]) -> Any:
        return await self.runtime.exec(
            self.workspace,
            " ".join(shlex.quote(value) for value in argv),
            cwd=self.root,
            timeout=self.command_timeout_seconds,
        )

    @staticmethod
    def _is_command_missing(result: Any) -> bool:
        """Whether the search binary itself was absent, as opposed to simply not matching.

        Keyed off exit code 127 — the only reliable signal. Sniffing output for "not found"
        misfired constantly: ``DaytonaRuntime.exec`` returns *combined* output in ``stdout``,
        so any repository containing the phrase (``detail="Workspace not found"``, say) looked
        like a missing ripgrep. The correct hits were then thrown away and the search re-ran
        as POSIX-BRE ``grep``, silently returning wrong or zero matches for any ERE/PCRE
        pattern the agent had asked for.
        """
        if getattr(result, "exit_code", 0) == 127:
            return True
        # Some shells report a missing binary on stderr without propagating 127; require the
        # full phrase *and* an empty stdout so real match output is never mistaken for it.
        if (getattr(result, "stdout", "") or "").strip():
            return False
        stderr = (getattr(result, "stderr", "") or "").lower()
        return "command not found" in stderr or "no such file or directory" in stderr

    def _parse_search_output(self, output: str, bounded: int) -> SearchResult:
        prefix = self.root.rstrip("/") + "/"
        matches: list[SearchMatch] = []
        truncated = False
        for line in output.splitlines():
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            raw_path, raw_line, text = parts
            try:
                line_number = int(raw_line)
            except ValueError:
                continue
            path = raw_path
            if path.startswith("./"):
                path = path[2:]
            if path.startswith(prefix):
                path = path[len(prefix) :]
            if len(matches) >= bounded:
                truncated = True
                break
            matches.append(
                SearchMatch(
                    path=path,
                    line_number=line_number,
                    text=text[:MAX_SEARCH_LINE_CHARS],
                )
            )
        return SearchResult(matches=matches, truncated=truncated)

    async def git_status(self) -> GitStatusResult:
        porcelain = await self.runtime.exec(
            self.workspace,
            "git status --porcelain",
            cwd=self.root,
            timeout=self.command_timeout_seconds,
        )
        if porcelain.exit_code != 0:
            raise WorkspaceToolError(
                sanitize_git_output(porcelain.stderr or porcelain.stdout or "git status failed")
            )
        files = parse_porcelain_status(porcelain.stdout)
        branch_result = await self.runtime.exec(
            self.workspace,
            "git branch --show-current",
            cwd=self.root,
            timeout=self.command_timeout_seconds,
        )
        branch = branch_result.stdout.strip() if branch_result.exit_code == 0 else ""
        return GitStatusResult(
            branch=branch or None,
            is_clean=len(files) == 0,
            files=files,
        )

    async def git_diff(self, path: str | None = None) -> str:
        args = ["diff", "--"]
        if path is not None:
            try:
                args.append(validate_diff_path(path))
            except RepositoryPathError as error:
                raise WorkspaceToolError("Path is outside the workspace") from error
        result = await self.runtime.exec(
            self.workspace,
            "git " + " ".join(shlex.quote(arg) for arg in args),
            cwd=self.root,
            timeout=self.command_timeout_seconds,
        )
        if result.exit_code != 0:
            raise WorkspaceToolError(
                sanitize_git_output(result.stderr or result.stdout or "git diff failed")
            )
        capped, _ = self._cap(result.stdout)
        return capped

    async def run_command(self, command: str) -> CommandResult:
        argv = self._validate_command(command)
        try:
            response = await self.runtime.exec(
                self.workspace,
                " ".join(shlex.quote(value) for value in argv),
                cwd=self.root,
                timeout=self.command_timeout_seconds,
            )
        except Exception as error:
            raise WorkspaceToolError(str(error)) from error
        stdout, stdout_truncated = self._cap(response.stdout)
        stderr, stderr_truncated = self._cap(response.stderr)
        return CommandResult(
            exit_code=response.exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )

    def _validate_command(self, command: str) -> list[str]:
        if any(token in command for token in DANGEROUS_TOKENS):
            self._log_rejected("shell_operator", None)
            raise DangerousCommandError("Shell control operators are not allowed")
        try:
            argv = shlex.split(command)
        except ValueError as error:
            self._log_rejected("parse_error", None)
            raise DangerousCommandError("Command could not be parsed") from error
        if not argv:
            raise DangerousCommandError("Command cannot be empty")
        program = argv[0]
        if "/" in program:
            self._log_rejected("path_in_argv0", program)
            raise DangerousCommandError("Command is not allowed")
        if self.allowlist_enabled:
            # Allowlist supersedes the denylist: only permitted programs run.
            if program not in self.allowed_commands:
                self._log_rejected("not_in_allowlist", program)
                raise DangerousCommandError("Command is not allowed")
        elif self.deny_dangerous and program in DANGEROUS_COMMANDS:
            self._log_rejected("denylisted", program)
            raise DangerousCommandError("Command is not allowed")
        return argv

    def _log_rejected(self, reason: str, program: str | None) -> None:
        # Audit rejected commands. Log only the reason + program (never the raw
        # command) to avoid leaking prompt/file content into logs. Rejections
        # also persist as tool_result error events in agent_events.
        logger.warning(
            "agent_command_rejected",
            workspace_id=str(self.workspace.id),
            reason=reason,
            program=program,
            allowlist_enabled=self.allowlist_enabled,
        )

    def _cap(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_command_output_chars:
            return value, False
        return value[: self.max_command_output_chars], True
