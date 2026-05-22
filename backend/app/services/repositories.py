import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RepositoryCredential, Workspace
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher

GITHUB_REPO_PATTERN = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$")
GIT_TIMEOUT_SECONDS = 60


class InvalidRepositoryUrlError(Exception):
    pass


class RepositoryCredentialNotFoundError(Exception):
    pass


class RepositoryTokenError(Exception):
    pass


class RepositoryPathError(Exception):
    pass


class RepositoryAlreadyClonedError(Exception):
    pass


class RepositoryNotClonedError(Exception):
    pass


class NoChangesToCommitError(Exception):
    pass


class GitCommandError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class GitFileStatus:
    path: str
    status: str


@dataclass(frozen=True)
class GitStatus:
    cloned: bool
    branch: str | None
    is_clean: bool
    files: list[GitFileStatus]


@dataclass(frozen=True)
class GitCommandResult:
    stdout: str
    stderr: str


def sanitize_repository_url(repo_url: str) -> str:
    parsed = urlparse(repo_url.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise InvalidRepositoryUrlError
    if not GITHUB_REPO_PATTERN.fullmatch(parsed.path):
        raise InvalidRepositoryUrlError

    path = parsed.path.rstrip("/")
    if not path.endswith(".git"):
        path = f"{path}.git"

    return urlunparse(("https", "github.com", path, "", "", ""))


def sanitize_git_output(value: str, token: str | None = None) -> str:
    sanitized = value
    if token:
        sanitized = sanitized.replace(token, "[redacted]")
    sanitized = re.sub(r"https://[^@\s]+@github\.com", "https://[redacted]@github.com", sanitized)
    return sanitized


def parse_porcelain_status(output: str) -> list[GitFileStatus]:
    files: list[GitFileStatus] = []
    for line in output.splitlines():
        if not line or line.startswith("## "):
            continue
        if len(line) < 4:
            continue
        status_code = line[:2].strip() or line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        files.append(GitFileStatus(path=path, status=status_code))
    return files


def validate_diff_path(path: str) -> str:
    candidate = path.strip()
    if not candidate:
        raise RepositoryPathError
    pure_path = PurePosixPath(candidate)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise RepositoryPathError
    return candidate


class GitRunner:
    def run(
        self,
        args: list[str],
        cwd: Path | None = None,
        token: str | None = None,
        timeout: int = GIT_TIMEOUT_SECONDS,
    ) -> GitCommandResult:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
        }

        if token is None:
            return self._run(args=args, cwd=cwd, env=env, token=token, timeout=timeout)

        with tempfile.TemporaryDirectory(prefix="execute-git-askpass-") as temp_dir:
            askpass_path = Path(temp_dir) / "askpass.sh"
            askpass_path.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '*Username*) printf "%s\\n" "x-access-token" ;;\n'
                '*Password*) printf "%s\\n" "$EXECUTE_GIT_TOKEN" ;;\n'
                '*) printf "%s\\n" "" ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            askpass_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            env["GIT_ASKPASS"] = str(askpass_path)
            env["EXECUTE_GIT_TOKEN"] = token
            return self._run(args=args, cwd=cwd, env=env, token=token, timeout=timeout)

    def _run(
        self,
        args: list[str],
        cwd: Path | None,
        env: dict[str, str],
        token: str | None,
        timeout: int,
    ) -> GitCommandResult:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitCommandError(sanitize_git_output(str(error), token)) from error

        if completed.returncode != 0:
            output = completed.stderr.strip() or completed.stdout.strip() or "Git command failed"
            raise GitCommandError(sanitize_git_output(output, token))

        return GitCommandResult(
            stdout=sanitize_git_output(completed.stdout, token),
            stderr=sanitize_git_output(completed.stderr, token),
        )


class RepositoryService:
    def __init__(
        self,
        session: AsyncSession,
        workspace_root: Path,
        cipher: WorkspaceSecretCipher,
        git_runner: GitRunner | None = None,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.cipher = cipher
        self.git_runner = git_runner or GitRunner()

    async def connect(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        repo_url: str,
        token: str,
    ) -> Workspace:
        workspace = await self._get_workspace(workspace_id, owner_id)
        sanitized_url = sanitize_repository_url(repo_url)
        encrypted_token = self.cipher.encrypt(token)

        result = await self.session.execute(
            select(RepositoryCredential).where(
                RepositoryCredential.workspace_id == workspace.id,
            )
        )
        credential = result.scalar_one_or_none()
        if credential is None:
            credential = RepositoryCredential(
                workspace_id=workspace.id,
                provider="github",
                auth_type="personal_access_token",
                encrypted_token=encrypted_token,
            )
        else:
            credential.provider = "github"
            credential.auth_type = "personal_access_token"
            credential.encrypted_token = encrypted_token

        workspace.repo_url = sanitized_url
        self.session.add(workspace)
        self.session.add(credential)
        await self.session.commit()
        return workspace

    async def clone(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._get_token(workspace.id)
        repo_path = self._repo_path(workspace)
        if self._is_cloned(repo_path):
            raise RepositoryAlreadyClonedError
        if repo_path.exists() and any(repo_path.iterdir()):
            raise RepositoryAlreadyClonedError
        repo_path.mkdir(parents=True, exist_ok=True)
        self.git_runner.run(["clone", workspace.repo_url, str(repo_path)], token=token)

    async def status(self, workspace_id: UUID, owner_id: UUID) -> GitStatus:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = self._repo_path(workspace)
        if not self._is_cloned(repo_path):
            return GitStatus(cloned=False, branch=None, is_clean=True, files=[])

        branch = self.git_runner.run(["branch", "--show-current"], cwd=repo_path).stdout.strip()
        status_output = self.git_runner.run(
            ["status", "--porcelain=v1", "-b"],
            cwd=repo_path,
        ).stdout
        files = parse_porcelain_status(status_output)
        return GitStatus(
            cloned=True,
            branch=branch or None,
            is_clean=len(files) == 0,
            files=files,
        )

    async def diff(self, workspace_id: UUID, owner_id: UUID, path: str | None = None) -> str:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = self._repo_path(workspace)
        self._ensure_cloned(repo_path)
        args = ["diff", "--"]
        if path is not None:
            args.append(validate_diff_path(path))
        return self.git_runner.run(args, cwd=repo_path).stdout

    async def pull(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._get_token(workspace.id)
        repo_path = self._repo_path(workspace)
        self._ensure_cloned(repo_path)
        self.git_runner.run(["pull", "--ff-only"], cwd=repo_path, token=token)

    async def commit(self, workspace_id: UUID, owner_id: UUID, message: str) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = self._repo_path(workspace)
        self._ensure_cloned(repo_path)
        status_result = self.git_runner.run(["status", "--porcelain=v1"], cwd=repo_path)
        if not parse_porcelain_status(status_result.stdout):
            raise NoChangesToCommitError
        self.git_runner.run(["add", "-A"], cwd=repo_path)
        self.git_runner.run(["commit", "-m", message], cwd=repo_path)

    async def push(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._get_token(workspace.id)
        repo_path = self._repo_path(workspace)
        self._ensure_cloned(repo_path)
        self.git_runner.run(["push"], cwd=repo_path, token=token)

    async def _get_workspace(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _get_token(self, workspace_id: UUID) -> str:
        result = await self.session.execute(
            select(RepositoryCredential).where(
                RepositoryCredential.workspace_id == workspace_id,
            )
        )
        credential = result.scalar_one_or_none()
        if credential is None:
            raise RepositoryCredentialNotFoundError
        token = self.cipher.decrypt(credential.encrypted_token)
        if token is None:
            raise RepositoryTokenError
        return token

    def _repo_path(self, workspace: Workspace) -> Path:
        if workspace.local_path is None:
            raise RepositoryPathError
        root = self.workspace_root.resolve()
        path = Path(workspace.local_path).resolve()
        if path == root or root not in path.parents:
            raise RepositoryPathError
        return path

    def _is_cloned(self, repo_path: Path) -> bool:
        return (repo_path / ".git").is_dir()

    def _ensure_cloned(self, repo_path: Path) -> None:
        if not self._is_cloned(repo_path):
            raise RepositoryNotClonedError

    def delete_repo_directory(self, workspace: Workspace) -> None:
        repo_path = self._repo_path(workspace)
        shutil.rmtree(repo_path, ignore_errors=True)
