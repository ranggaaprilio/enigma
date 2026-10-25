import base64
import re
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import RepositoryCredential, Workspace
from app.services.git_providers import (
    AUTO_PROVIDER,
    InvalidRepositoryUrlError,
    detect_provider,
    http_basic_username,
    parse_repository_url,
    redact_secrets,
)
from app.services.git_providers import sanitize_repository_url as _sanitize_repository_url
from app.services.sandbox import RuntimeFileSystem, SandboxRuntime
from app.services.ssh_keys import generate_ed25519_keypair
from app.services.workspaces import WorkspaceNotFoundError, WorkspaceSecretCipher

GIT_TIMEOUT_SECONDS = 60
CLONE_TIMEOUT_SECONDS = 300
# Private keys are materialised under here for the duration of one git command, then removed.
SSH_TMP_ROOT = "/tmp/enigma-ssh"

# Re-exported for callers that historically imported the URL error from this module.
__all__ = ["InvalidRepositoryUrlError"]

# Commit SHAs are hex; accept an abbreviated (>=4) or full (40) hash. ``shlex.quote`` already blocks
# shell injection, but validating the shape keeps arbitrary refs/paths out of ``git show``.
COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{4,40}$")

# ASCII unit (\x1f) / record (\x1e) separators delimit the machine-parseable log format so commit
# metadata never collides with the delimiter — subjects may contain almost anything but not these
# control bytes.
_LOG_FIELD_SEP = "\x1f"
_LOG_RECORD_SEP = "\x1e"
LOG_FORMAT = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1e"
# Same field layout as ``LOG_FORMAT`` but with the body (%b) appended last so embedded newlines can
# never shift earlier fields; used by ``commit_detail`` with ``git show -s``.
COMMIT_META_FORMAT = "%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b"

# Fallback commit identity used when the acting user has no name/email. The signed-in user's
# name/email is preferred; this is shared with speckit so the fallback is consistent.
DEFAULT_COMMIT_AUTHOR = "Enigma app"
DEFAULT_COMMIT_EMAIL = "author@enigma.ai"


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


class PullRequestsUnsupportedError(Exception):
    """Provider-native pull requests need a GitHub repository connected with a token."""

    def __init__(self, provider: str, auth_type: str | None = None) -> None:
        self.provider = provider
        self.auth_type = auth_type
        super().__init__(provider)


@dataclass(frozen=True)
class GitAuth:
    """Decrypted credential material for one git operation. Never logged or persisted."""

    kind: Literal["token", "ssh"]
    username: str | None = None
    token: str | None = None
    private_key: str | None = None

    @property
    def secrets(self) -> tuple[str, ...]:
        return tuple(secret for secret in (self.token, self.private_key) if secret)


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
class GitCommit:
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: str
    subject: str


@dataclass(frozen=True)
class GitCommitDetail:
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: str
    subject: str
    body: str
    diff: str


def sanitize_repository_url(
    repo_url: str,
    *,
    provider: str = AUTO_PROVIDER,
    auth_type: str | None = None,
) -> str:
    """Canonical, credential-free repository URL (see ``git_providers``)."""
    return _sanitize_repository_url(repo_url, provider=provider, auth_type=auth_type)


def sanitize_git_output(value: str, *secrets: str | None) -> str:
    """Redact tokens/userinfo from git output regardless of the remote host."""
    return redact_secrets(value, *secrets)


GIT_STATUS_COMMAND = "git status --porcelain -b --untracked-files=all"


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


# go-git's status words, which the SDK's ``git.status`` returned and the UI / the agent
# file-change badge mapper (``AgentService._map_git_status_to_badge``) expect.
_PORCELAIN_STATUS_WORDS = {
    "?": "Untracked",
    "!": "Ignored",
    "M": "Modified",
    "A": "Added",
    "D": "Deleted",
    "R": "Renamed",
    "C": "Copied",
    "U": "UpdatedButUnmerged",
}


def porcelain_status_word(code: str) -> str:
    """Map a porcelain ``XY`` code (as ``parse_porcelain_status`` strips it) to go-git's word.

    Like the SDK status did, the worktree column wins over staging when both are set
    (``AM`` → ``Modified``); ``??`` → ``Untracked``.
    """
    selected = code[-1] if len(code) == 2 else code
    return _PORCELAIN_STATUS_WORDS.get(selected, selected)


def branch_from_porcelain_header(output: str) -> str | None:
    """Current branch from a ``git status --porcelain -b`` header line, ``None`` when detached."""
    for line in output.splitlines():
        if not line.startswith("## "):
            continue
        header = line[3:]
        for unborn in ("No commits yet on ", "Initial commit on "):
            if header.startswith(unborn):
                return header[len(unborn) :].strip() or None
        if header.startswith("HEAD "):
            return None
        return header.split("...", 1)[0].split(" ", 1)[0] or None
    return None


def validate_diff_path(path: str) -> str:
    candidate = path.strip()
    if not candidate:
        raise RepositoryPathError
    pure_path = PurePosixPath(candidate)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise RepositoryPathError
    return candidate


def validate_commit_sha(value: str) -> str:
    candidate = value.strip()
    if not COMMIT_SHA_PATTERN.fullmatch(candidate):
        raise RepositoryPathError
    return candidate


class RepositoryService:
    def __init__(
        self,
        session: AsyncSession,
        cipher: WorkspaceSecretCipher,
        runtime: SandboxRuntime,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.runtime = runtime
        self.settings = settings or get_settings()

    async def connect(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        repo_url: str,
        *,
        provider: str = AUTO_PROVIDER,
        auth_type: str = "personal_access_token",
        token: str | None = None,
        username: str | None = None,
    ) -> Workspace:
        """Point the workspace at a remote and store the credential used to reach it.

        Token auth stores the (encrypted) token; SSH auth reuses the workspace's deploy key pair
        or generates one. Only the workspace row and the single credential row change — the
        sandbox is untouched (``origin`` is reconciled lazily by push/pull/fetch).
        """
        workspace = await self._get_workspace(workspace_id, owner_id)
        parsed = parse_repository_url(repo_url, provider=provider, auth_type=auth_type)

        credential = await self._load_credential(workspace.id)
        if credential is None:
            credential = RepositoryCredential(
                workspace_id=workspace.id, provider=parsed.provider, auth_type=auth_type
            )
        credential.provider = parsed.provider
        credential.auth_type = auth_type
        if auth_type == "ssh_key":
            if not credential.encrypted_private_key or not credential.public_key:
                self._assign_keypair(credential, workspace.id)
            credential.encrypted_token = None
            credential.username = None
        else:
            if not token:
                raise ValueError("token is required for personal_access_token authentication")
            credential.encrypted_token = self.cipher.encrypt(token)
            credential.username = (username or "").strip() or None
            credential.encrypted_private_key = None
            credential.public_key = None

        workspace.repo_url = parsed.url
        self.session.add(workspace)
        self.session.add(credential)
        await self.session.commit()
        await self.session.refresh(workspace)
        return workspace

    async def ensure_ssh_key(
        self, workspace_id: UUID, owner_id: UUID, *, rotate: bool = False
    ) -> RepositoryCredential:
        """Make sure the workspace has an SSH deploy key pair and return its credential row.

        Usable before a remote is connected (the wizard shows the public key first) — the
        provider is then a best guess from ``repo_url`` and is corrected by ``connect``.

        Making a key available never switches a working HTTPS+token workspace over to SSH.
        Doing so used to drop the (encrypted, unrecoverable) token *and* leave an ``https://``
        remote paired with ``ssh_key`` auth, so every later fetch/pull/push went through
        ``_run_ssh_git`` and failed. SSH only becomes the active mode when the remote can
        actually be reached over it — an SSH URL, or no remote yet (the wizard's key-first
        flow). Switching an existing HTTPS workspace is ``connect``'s job; it re-parses the
        URL and enforces the URL/auth pairing.
        """
        workspace = await self._get_workspace(workspace_id, owner_id)
        credential = await self._load_credential(workspace.id)
        if credential is None:
            credential = RepositoryCredential(
                workspace_id=workspace.id,
                provider=detect_provider(workspace.repo_url),
                auth_type="ssh_key",
            )
        needs_key = (
            rotate
            or credential.auth_type != "ssh_key"
            or not credential.encrypted_private_key
            or not credential.public_key
        )
        if needs_key:
            self._assign_keypair(credential, workspace.id)
        if self._ssh_reachable(workspace.repo_url):
            credential.auth_type = "ssh_key"
            credential.encrypted_token = None
            credential.username = None
        self.session.add(credential)
        await self.session.commit()
        await self.session.refresh(credential)
        return credential

    async def get_credential(self, workspace_id: UUID, owner_id: UUID) -> RepositoryCredential:
        workspace = await self._get_workspace(workspace_id, owner_id)
        credential = await self._load_credential(workspace.id)
        if credential is None:
            raise RepositoryCredentialNotFoundError
        return credential

    @staticmethod
    def _ssh_reachable(repo_url: str | None) -> bool:
        """Whether ``repo_url`` is one SSH auth can actually be used against.

        An unset remote counts: the connect wizard shows the deploy key before a URL is
        entered, and ``connect`` settles the pairing afterwards.
        """
        if not repo_url:
            return True
        try:
            parse_repository_url(repo_url, auth_type="ssh_key")
        except InvalidRepositoryUrlError:
            return False
        return True

    def _assign_keypair(self, credential: RepositoryCredential, workspace_id: UUID) -> None:
        pair = generate_ed25519_keypair(f"enigma-workspace-{workspace_id}")
        credential.encrypted_private_key = self.cipher.encrypt(pair.private_key)
        credential.public_key = pair.public_key

    async def clone(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        if not workspace.repo_url:
            raise RepositoryCredentialNotFoundError
        auth = await self._get_auth(workspace.id)
        fs = await self.runtime.fs(workspace)
        if await self._is_cloned(fs, workspace.workspace_path):
            raise RepositoryAlreadyClonedError
        try:
            await fs.get_file_info(workspace.workspace_path)
        except Exception:
            await fs.create_folder(workspace.workspace_path, "755")
        # Clone with the git CLI (not the SDK's server-side go-git) so token and SSH auth share
        # one code path — the same reason push/pull already run through the CLI.
        result = await self._run_authenticated_git(
            workspace,
            ["clone", "--branch", workspace.default_branch, "--", workspace.repo_url, "."],
            auth,
            cwd=workspace.workspace_path,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            raise GitCommandError(
                sanitize_git_output(
                    result.stdout or result.stderr or "git clone failed", *auth.secrets
                )
            )

    async def status(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
    ) -> GitStatus:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        if not await self._is_cloned(fs, repo_path):
            return GitStatus(cloned=False, branch=None, is_clean=True, files=[])
        # Always the git CLI: it is provider-neutral and, unlike a server-side git, resolves a
        # linked worktree's ``.git`` pointer file — the same way diffs and worktree stats do.
        return await self._status_via_cli(workspace, repo_path)

    async def _status_via_cli(self, workspace: Workspace, repo_path: str) -> GitStatus:
        # One exec: ``-b`` carries the branch in the header, ``-uall`` lists untracked files
        # individually (go-git never collapsed a new directory to ``dir/``).
        porcelain = await self.runtime.exec(workspace, GIT_STATUS_COMMAND, cwd=repo_path)
        if porcelain.exit_code != 0:
            raise GitCommandError(porcelain.stderr or porcelain.stdout or "Git status failed")
        files = [
            GitFileStatus(path=item.path, status=porcelain_status_word(item.status))
            for item in parse_porcelain_status(porcelain.stdout)
        ]
        return GitStatus(
            cloned=True,
            branch=branch_from_porcelain_header(porcelain.stdout),
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
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
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

    async def log(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
        limit: int = 50,
        skip: int = 0,
    ) -> list[GitCommit]:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
        capped = max(1, min(limit, 200))
        offset = max(0, skip)
        args = ["log", "-n", str(capped), f"--skip={offset}", f"--pretty=format:{LOG_FORMAT}"]
        result = await self.runtime.exec(
            workspace,
            "git " + " ".join(shlex.quote(arg) for arg in args),
            cwd=repo_path,
        )
        if result.exit_code != 0:
            output = (result.stderr or result.stdout or "").lower()
            # A branch with no commits yet is an empty history, not an error.
            if "does not have any commits" in output or "unknown revision" in output:
                return []
            raise GitCommandError(result.stderr or result.stdout or "git log failed")
        return self._parse_log(result.stdout)

    async def commit_detail(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        sha: str,
        *,
        worktree_path: str | None = None,
    ) -> GitCommitDetail:
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
        rev = validate_commit_sha(sha)
        # Metadata without the diff (``-s``); body (%b) is the final field so it can't shift others.
        meta = await self.runtime.exec(
            workspace,
            "git "
            + " ".join(
                shlex.quote(arg)
                for arg in ["show", "-s", f"--pretty=format:{COMMIT_META_FORMAT}", rev]
            ),
            cwd=repo_path,
        )
        if meta.exit_code != 0:
            raise GitCommandError(meta.stderr or meta.stdout or "git show failed")
        fields = meta.stdout.split(_LOG_FIELD_SEP)
        if len(fields) < 7:
            raise GitCommandError("Unexpected git show output")
        # Patch only — an empty ``--pretty=format:`` prints just the unified diff and handles the
        # root commit (no parent) by showing the file contents as additions.
        patch = await self.runtime.exec(
            workspace,
            "git " + " ".join(shlex.quote(arg) for arg in ["show", "--pretty=format:", rev]),
            cwd=repo_path,
        )
        if patch.exit_code != 0:
            raise GitCommandError(patch.stderr or patch.stdout or "git show failed")
        return GitCommitDetail(
            hash=fields[0],
            short_hash=fields[1],
            author_name=fields[2],
            author_email=fields[3],
            date=fields[4],
            subject=fields[5],
            body=fields[6],
            diff=patch.stdout.lstrip("\n"),
        )

    @staticmethod
    def _parse_log(output: str) -> list[GitCommit]:
        commits: list[GitCommit] = []
        for raw in output.split(_LOG_RECORD_SEP):
            record = raw.strip("\n")
            if not record.strip():
                continue
            fields = record.split(_LOG_FIELD_SEP)
            if len(fields) < 6:
                continue
            commits.append(
                GitCommit(
                    hash=fields[0],
                    short_hash=fields[1],
                    author_name=fields[2],
                    author_email=fields[3],
                    date=fields[4],
                    subject=fields[5],
                )
            )
        return commits

    async def pull(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
    ) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        auth = await self._get_auth(workspace.id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
        await self._ensure_origin(workspace, repo_path)
        result = await self._run_authenticated_git(workspace, ["pull"], auth, cwd=repo_path)
        if result.exit_code != 0:
            raise GitCommandError(
                sanitize_git_output(
                    result.stdout or result.stderr or "git pull failed", *auth.secrets
                )
            )

    async def fetch(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        worktree_path: str | None = None,
    ) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        auth = await self._get_auth(workspace.id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
        await self._ensure_origin(workspace, repo_path)
        # A fetch updates the shared object store for every worktree in the clone, so ahead/behind
        # counts (WorktreeService._stats) become accurate afterward regardless of the cwd used.
        result = await self._run_authenticated_git(
            workspace, ["fetch", "origin"], auth, cwd=repo_path
        )
        if result.exit_code != 0:
            raise GitCommandError(
                sanitize_git_output(
                    result.stdout or result.stderr or "git fetch failed", *auth.secrets
                )
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
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
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
        auth = await self._get_auth(workspace.id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
        await self._ensure_origin(workspace, repo_path)
        # Push via the git CLI (not the Daytona SDK's server-side go-git). go-git cannot resolve
        # references through a linked worktree's ``.git`` pointer file — the same limitation that
        # forces ``status`` onto the CLI — so a new feature branch created in a worktree never
        # reaches the remote. ``push --set-upstream origin HEAD`` pushes the current branch to a
        # like-named remote branch (creating it if absent) and records the upstream, and behaves
        # identically in the base clone and inside worktrees.
        result = await self._run_authenticated_git(
            workspace,
            ["push", "--set-upstream", "origin", "HEAD"],
            auth,
            cwd=repo_path,
        )
        if result.exit_code != 0:
            raise GitCommandError(
                sanitize_git_output(
                    result.stdout or result.stderr or "git push failed", *auth.secrets
                )
            )

    async def discard(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        *,
        path: str | None = None,
        worktree_path: str | None = None,
    ) -> None:
        """Discard uncommitted changes — a single file when ``path`` is given, otherwise all.

        This is destructive and cannot be undone. Callers must confirm intent in the UI.
        """
        workspace = await self._get_workspace(workspace_id, owner_id)
        repo_path = worktree_path or workspace.workspace_path
        fs = await self.runtime.fs(workspace)
        await self._ensure_cloned(fs, repo_path)
        if path is None:
            # Everything: staged + unstaged tracked changes (reset --hard) and untracked files/dirs
            # (clean -fd).
            reset = await self.runtime.exec(
                workspace, "git reset --hard HEAD", cwd=repo_path, timeout=GIT_TIMEOUT_SECONDS
            )
            if reset.exit_code != 0:
                raise GitCommandError(reset.stderr or reset.stdout or "git reset failed")
            clean = await self.runtime.exec(
                workspace, "git clean -fd", cwd=repo_path, timeout=GIT_TIMEOUT_SECONDS
            )
            if clean.exit_code != 0:
                raise GitCommandError(clean.stderr or clean.stdout or "git clean failed")
            return
        target = validate_diff_path(path)
        # Handle the working-tree states the Changes list surfaces: an untracked file is removed
        # (clean), a tracked modification/deletion is restored from HEAD (checkout).
        status = await self.runtime.exec(
            workspace,
            "git " + " ".join(shlex.quote(arg) for arg in ["status", "--porcelain", "--", target]),
            cwd=repo_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if status.exit_code != 0:
            raise GitCommandError(status.stderr or status.stdout or "git status failed")
        untracked = any(line.startswith("??") for line in status.stdout.splitlines())
        args = ["clean", "-fd", "--", target] if untracked else ["checkout", "HEAD", "--", target]
        result = await self.runtime.exec(
            workspace,
            "git " + " ".join(shlex.quote(arg) for arg in args),
            cwd=repo_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            raise GitCommandError(result.stderr or result.stdout or "git discard failed")

    async def _get_workspace(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _ensure_origin(self, workspace: Workspace, repo_path: str) -> None:
        """Make ``origin`` point at ``workspace.repo_url`` (best effort).

        Template and blank workspaces start with no remote, and a re-``connect`` may switch the
        URL (e.g. HTTPS → SSH). Reconciling lazily here keeps ``connect`` DB-only; any failure is
        left for the following push/pull/fetch to surface with git's own message.
        """
        if not workspace.repo_url:
            return
        current = await self.runtime.exec(
            workspace, "git remote get-url origin", cwd=repo_path, timeout=GIT_TIMEOUT_SECONDS
        )
        if current.exit_code != 0:
            args = ["remote", "add", "origin", workspace.repo_url]
        elif current.stdout.strip() != workspace.repo_url:
            args = ["remote", "set-url", "origin", workspace.repo_url]
        else:
            return
        await self.runtime.exec(
            workspace,
            "git " + " ".join(shlex.quote(part) for part in args),
            cwd=repo_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )

    async def _run_authenticated_git(
        self,
        workspace: Workspace,
        args: list[str],
        auth: GitAuth,
        *,
        cwd: str,
        timeout: int = GIT_TIMEOUT_SECONDS,
    ):
        """Run a git CLI command against ``origin`` with the workspace's credential.

        Token auth rides on a per-command HTTP Basic ``Authorization`` header
        (``http.extraHeader``) whose username half is provider-specific (``x-access-token`` for
        GitHub, ``oauth2`` for GitLab, ``x-token-auth`` or the account name for Bitbucket).
        Passing it as a per-invocation ``-c`` value keeps it out of ``.git/config`` and the
        remote URL. SSH auth materialises the deploy key inside the sandbox for the duration of
        the command and points ``GIT_SSH_COMMAND`` at it. Nothing is persisted either way.
        """
        if auth.kind == "ssh":
            return await self._run_ssh_git(workspace, args, auth, cwd=cwd, timeout=timeout)
        basic = base64.b64encode(f"{auth.username}:{auth.token}".encode()).decode()
        command = "git " + " ".join(
            shlex.quote(part)
            for part in ("-c", f"http.extraHeader=AUTHORIZATION: basic {basic}", *args)
        )
        return await self.runtime.exec(workspace, command, cwd=cwd, timeout=timeout)

    async def _run_ssh_git(
        self,
        workspace: Workspace,
        args: list[str],
        auth: GitAuth,
        *,
        cwd: str,
        timeout: int,
    ):
        if not auth.private_key:
            raise RepositoryTokenError
        fs = await self.runtime.fs(workspace)
        ssh_dir = f"{self.settings.daytona_sandbox_home.rstrip('/')}/.ssh"
        async with self._ssh_identity(fs, auth.private_key) as key_path:
            ssh_command = " ".join(
                (
                    "ssh",
                    "-i",
                    shlex.quote(key_path),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                    "-o",
                    f"UserKnownHostsFile={shlex.quote(ssh_dir + '/known_hosts')}",
                )
            )
            # ``accept-new`` needs a writable known_hosts location; create ~/.ssh on first use.
            command = f"mkdir -p {shlex.quote(ssh_dir)} && git " + " ".join(
                shlex.quote(part) for part in args
            )
            result = await self.runtime.exec(
                workspace,
                command,
                cwd=cwd,
                env={"GIT_SSH_COMMAND": ssh_command, "GIT_TERMINAL_PROMPT": "0"},
                timeout=timeout,
            )
        output = (result.stdout or "") + (result.stderr or "")
        if result.exit_code != 0 and "cannot run ssh" in output.lower():
            raise GitCommandError("SSH client is not available in this sandbox image")
        return result

    @asynccontextmanager
    async def _ssh_identity(self, fs: RuntimeFileSystem, private_key: str) -> AsyncIterator[str]:
        """Write the private key to a private temp dir in the sandbox; always remove it."""
        directory = f"{SSH_TMP_ROOT}/{uuid4().hex}"
        key_path = f"{directory}/id_ed25519"
        try:
            await fs.create_folder(directory, "700")
            await fs.upload_file(private_key.encode("utf-8"), key_path)
            await fs.set_file_permissions(key_path, mode="600")
        except Exception as error:
            await self._remove_quietly(fs, directory)
            raise GitCommandError(
                redact_secrets(f"Could not stage SSH key: {error}", private_key)
            ) from error
        try:
            yield key_path
        finally:
            await self._remove_quietly(fs, directory)

    @staticmethod
    async def _remove_quietly(fs: RuntimeFileSystem, path: str) -> None:
        try:
            await fs.delete_file(path, recursive=True)
        except Exception:
            return

    async def _load_credential(self, workspace_id: UUID) -> RepositoryCredential | None:
        result = await self.session.execute(
            select(RepositoryCredential).where(
                RepositoryCredential.workspace_id == workspace_id,
            )
        )
        return result.scalar_one_or_none()

    async def _get_auth(self, workspace_id: UUID) -> GitAuth:
        credential = await self._load_credential(workspace_id)
        if credential is None:
            raise RepositoryCredentialNotFoundError
        if credential.auth_type == "ssh_key":
            private_key = self.cipher.decrypt(credential.encrypted_private_key or "")
            if private_key is None:
                raise RepositoryTokenError
            return GitAuth(kind="ssh", private_key=private_key)
        token = self.cipher.decrypt(credential.encrypted_token or "")
        if token is None:
            raise RepositoryTokenError
        return GitAuth(
            kind="token",
            username=http_basic_username(credential.provider, credential.username),
            token=token,
        )

    async def _is_cloned(self, fs: RuntimeFileSystem, repo_path: str) -> bool:
        # In the base clone ``.git`` is a directory; inside a linked worktree it is a file that
        # points at the shared git dir. Either means the path is a usable git working tree.
        try:
            await fs.get_file_info(f"{repo_path}/.git")
        except Exception:
            return False
        return True

    async def _ensure_cloned(self, fs: RuntimeFileSystem, repo_path: str) -> None:
        if not await self._is_cloned(fs, repo_path):
            raise RepositoryNotClonedError

    async def delete_repo_directory(self, workspace: Workspace) -> None:
        fs = await self.runtime.fs(workspace)
        await fs.delete_file(workspace.workspace_path, recursive=True)
        await fs.create_folder(workspace.workspace_path, "755")
