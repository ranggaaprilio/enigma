import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

from app.models import Workspace

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
        command_timeout_seconds: int,
        max_command_output_chars: int,
    ) -> None:
        if workspace.local_path is None:
            raise WorkspaceToolError("Workspace path is not ready")
        self.root = Path(workspace.local_path).resolve()
        self.command_timeout_seconds = command_timeout_seconds
        self.max_command_output_chars = max_command_output_chars

    def resolve_path(self, path: str | None = None) -> Path:
        candidate = self.root if not path else (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceToolError("Path is outside the workspace")
        if any(part in EXCLUDED_NAMES for part in candidate.relative_to(self.root).parts):
            raise WorkspaceToolError("Path is excluded")
        return candidate

    def list_files(self, path: str | None = None, limit: int = 200) -> list[str]:
        directory = self.resolve_path(path)
        if not directory.exists():
            raise WorkspaceToolError("Path does not exist")
        if not directory.is_dir():
            raise WorkspaceToolError("Path is not a directory")

        results: list[str] = []
        for item in sorted(directory.rglob("*")):
            relative = item.relative_to(self.root)
            if any(part in EXCLUDED_NAMES for part in relative.parts):
                continue
            results.append(str(relative) + ("/" if item.is_dir() else ""))
            if len(results) >= limit:
                break
        return results

    def read_file(self, path: str, max_chars: int = 20_000) -> str:
        file_path = self.resolve_path(path)
        if not file_path.exists() or not file_path.is_file():
            raise WorkspaceToolError("File does not exist")
        return file_path.read_text(encoding="utf-8", errors="replace")[:max_chars]

    def write_file(self, path: str, content: str) -> str:
        file_path = self.resolve_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return str(file_path.relative_to(self.root))

    async def run_command(self, command: str) -> CommandResult:
        argv = self._validate_command(command)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.command_timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise WorkspaceToolError("Command timed out") from None

        stdout, stdout_truncated = self._cap(stdout_bytes.decode("utf-8", errors="replace"))
        stderr, stderr_truncated = self._cap(stderr_bytes.decode("utf-8", errors="replace"))
        return CommandResult(
            exit_code=process.returncode or 0,
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
