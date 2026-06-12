import asyncio
import shlex
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from app.models import Workspace
from app.services.daytona_runtime import DaytonaRuntime

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


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool


class WorkspaceTools:
    def __init__(
        self,
        workspace: Workspace,
        runtime: DaytonaRuntime,
        command_timeout_seconds: int,
        max_command_output_chars: int,
    ) -> None:
        self.workspace = workspace
        self.runtime = runtime
        self.command_timeout_seconds = command_timeout_seconds
        self.max_command_output_chars = max_command_output_chars

    def resolve_path(self, path: str | None = None) -> str:
        candidate = (path or "").strip().strip("/")
        pure = PurePosixPath(candidate)
        if pure.is_absolute() or ".." in pure.parts:
            raise WorkspaceToolError("Path is outside the workspace")
        if any(part in EXCLUDED_NAMES for part in pure.parts):
            raise WorkspaceToolError("Path is excluded")
        return (
            self.workspace.workspace_path
            if not candidate
            else f"{self.workspace.workspace_path}/{pure.as_posix()}"
        )

    async def list_files(self, path: str | None = None, limit: int = 200) -> list[str]:
        sandbox = await self.runtime.get_sandbox(self.workspace)
        root = self.resolve_path(path)
        bounded_limit = max(1, min(limit, 200))
        results: list[str] = []
        prefix = self.workspace.workspace_path.rstrip("/") + "/"
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

    async def write_file(self, path: str, content: str) -> str:
        sandbox = await self.runtime.get_sandbox(self.workspace)
        remote = self.resolve_path(path)
        parent = str(PurePosixPath(remote).parent)
        await sandbox.fs.create_folder(parent, "755")
        await sandbox.fs.upload_file(content.encode("utf-8"), remote)
        return PurePosixPath(path).as_posix()

    async def run_command(self, command: str) -> CommandResult:
        argv = self._validate_command(command)
        try:
            response = await self.runtime.exec(
                self.workspace,
                " ".join(shlex.quote(value) for value in argv),
                cwd=self.workspace.workspace_path,
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
            raise DangerousCommandError("Shell control operators are not allowed")
        try:
            argv = shlex.split(command)
        except ValueError as error:
            raise DangerousCommandError("Command could not be parsed") from error
        if not argv:
            raise DangerousCommandError("Command cannot be empty")
        if "/" in argv[0] or argv[0] in DANGEROUS_COMMANDS:
            raise DangerousCommandError("Command is not allowed")
        return argv

    def _cap(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_command_output_chars:
            return value, False
        return value[: self.max_command_output_chars], True
