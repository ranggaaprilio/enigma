import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlparse, urlunparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RepositoryCredential, Workspace
from app.services.daytona_runtime import DaytonaRuntime, DaytonaRuntimeError
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher

GITHUB_REPO_PATTERN = re.compile(r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$")
GIT_TIMEOUT_SECONDS = 60

# Fallback commit identity used when the acting user has no name/email. The signed-in user's
# name/email is preferred; this is shared with speckit so the fallback is consistent.
DEFAULT_COMMIT_AUTHOR = "Enigma app"
DEFAULT_COMMIT_EMAIL = "author@enigma.ai"


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


class RepositoryService:
    def __init__(
        self,
        session: AsyncSession,
        cipher: WorkspaceSecretCipher,
        runtime: DaytonaRuntime,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.runtime = runtime

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
        await self.session.refresh(workspace)
        return workspace

    async def clone(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._get_token(workspace.id)
        sandbox = await self.runtime.get_sandbox(workspace)
        if await self._is_cloned(sandbox, workspace.workspace_path):
            raise RepositoryAlreadyClonedError
        try:
            await sandbox.git.clone(
                workspace.repo_url,
                workspace.workspace_path,
                branch=workspace.default_branch,
                username="x-access-token",
                password=token,
            )
        except DaytonaRuntimeError as error:
            raise GitCommandError(str(error)) from error
        except Exception as error:
            raise GitCommandError(sanitize_git_output(str(error), token)) from error

    async def status(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
    ) -> GitStatus:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        sandbox = await self.runtime.get_sandbox(workspace)
        if not await self._is_cloned(sandbox, repo_path):
            return GitStatus(cloned=False, branch=None, is_clean=True, files=[])
        # A linked worktree's ``.git`` is a pointer file that Daytona's server-side git (go-git)
        # cannot resolve references through ("reference not found"), so read its status with the
        # git CLI — the same way diffs and worktree stats already do. The base clone keeps the
        # richer SDK status.
        if worktree_path is not None:
            return await self._status_via_cli(workspace, repo_path)
        try:
            result = await sandbox.git.status(repo_path)
        except Exception as error:
            raise GitCommandError(str(error)) from error
        files = [
            GitFileStatus(
                path=str(getattr(item, "path", getattr(item, "name", ""))),
                status=self._file_status(item),
            )
            for item in (getattr(result, "file_status", None) or [])
        ]
        return GitStatus(
            cloned=True,
            branch=getattr(result, "current_branch", None),
            is_clean=len(files) == 0,
            files=files,
        )

    async def _status_via_cli(self, workspace: Workspace, repo_path: str) -> GitStatus:
        porcelain = await self.runtime.exec(
            workspace, "git status --porcelain", cwd=repo_path
        )
        if porcelain.exit_code != 0:
            raise GitCommandError(
                porcelain.stderr or porcelain.stdout or "Git status failed"
            )
        files = parse_porcelain_status(porcelain.stdout)
        branch_result = await self.runtime.exec(
            workspace, "git branch --show-current", cwd=repo_path
        )
        branch = branch_result.stdout.strip() if branch_result.exit_code == 0 else ""
        return GitStatus(
            cloned=True,
            branch=branch or None,
            is_clean=len(files) == 0,
            files=files,
        )

    async def diff(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str | None = None,
        *,
        worktree_path: str | None = None,
    ) -> str:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        sandbox = await self.runtime.get_sandbox(workspace)
        await self._ensure_cloned(sandbox, repo_path)
        args = ["diff", "--"]
        if path is not None:
            args.append(validate_diff_path(path))
        result = await self.runtime.exec(
            workspace,
            "git " + " ".join(shlex.quote(arg) for arg in args),
            cwd=repo_path,
        )
        if result.exit_code != 0:
            raise GitCommandError(result.stderr or result.stdout or "Git diff failed")
        return result.stdout

    async def pull(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._get_token(workspace.id)
        sandbox = await self.runtime.get_sandbox(workspace)
        await self._ensure_cloned(sandbox, workspace.workspace_path)
        await sandbox.git.pull(
            workspace.workspace_path,
            username="x-access-token",
            password=token,
        )

    async def commit(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        message: str,
        *,
        worktree_path: str | None = None,
        author_name: str | None = None,
        author_email: str | None = None,
    ) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        sandbox = await self.runtime.get_sandbox(workspace)
        await self._ensure_cloned(sandbox, repo_path)
        # Stop early when there is nothing staged or unstaged to commit.
        porcelain = await self.runtime.exec(
            workspace,
            "git status --porcelain",
            cwd=repo_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if not porcelain.stdout.strip():
            raise NoChangesToCommitError
        name = author_name or DEFAULT_COMMIT_AUTHOR
        email = author_email or DEFAULT_COMMIT_EMAIL
        # Use the per-call -c identity form (mirrors speckit) so the author is explicit and we
        # never rely on a global/hardcoded git identity.
        add = await self.runtime.exec(
            workspace, "git add -A", cwd=repo_path, timeout=GIT_TIMEOUT_SECONDS
        )
        if add.exit_code != 0:
            raise GitCommandError(add.stderr or add.stdout or "git add failed")
        commit_cmd = "git " + " ".join(
            shlex.quote(arg)
            for arg in [
                "-c",
                f"user.name={name}",
                "-c",
                f"user.email={email}",
                "commit",
                "-m",
                message,
            ]
        )
        commit = await self.runtime.exec(
            workspace, commit_cmd, cwd=repo_path, timeout=GIT_TIMEOUT_SECONDS
        )
        if commit.exit_code != 0:
            output = (commit.stdout + commit.stderr).lower()
            if "nothing to commit" in output:
                raise NoChangesToCommitError
            raise GitCommandError(commit.stderr or commit.stdout or "git commit failed")

    async def push(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
    ) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        token = await self._get_token(workspace.id)
        repo_path = worktree_path or workspace.workspace_path
        sandbox = await self.runtime.get_sandbox(workspace)
        await self._ensure_cloned(sandbox, repo_path)
        await sandbox.git.push(
            repo_path,
            username="x-access-token",
            password=token,
        )

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

    async def _is_cloned(self, sandbox, repo_path: str) -> bool:
        # In the base clone ``.git`` is a directory; inside a linked worktree it is a file that
        # points at the shared git dir. Either means the path is a usable git working tree.
        try:
            await sandbox.fs.get_file_info(f"{repo_path}/.git")
        except Exception:
            return False
        return True

    def _file_status(self, item) -> str:
        worktree = getattr(item, "worktree", None)
        staging = getattr(item, "staging", None)
        selected = worktree if getattr(worktree, "value", worktree) != "Unmodified" else staging
        return str(getattr(selected, "value", selected) or "")

    async def _ensure_cloned(self, sandbox, repo_path: str) -> None:
        if not await self._is_cloned(sandbox, repo_path):
            raise RepositoryNotClonedError

    async def delete_repo_directory(self, workspace: Workspace) -> None:
        sandbox = await self.runtime.get_sandbox(workspace)
        await sandbox.fs.delete_file(workspace.workspace_path, recursive=True)
        await sandbox.fs.create_folder(workspace.workspace_path, "755")
