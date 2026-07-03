"""Git worktree management for a workspace's clone.

Worktrees let several branches live side-by-side in one Daytona sandbox: the base clone stays
at ``workspace.workspace_path`` on the default branch, while feature branches are checked out
into a managed sibling directory ``worktrees/<slug>`` (relative to the sandbox home). All git
work runs through ``DaytonaRuntime.exec`` with raw ``git worktree`` because the Daytona git SDK
only wraps high-level ops. This mirrors the pattern already used by ``speckit.py``.

The only way a caller targets a worktree is by its database id, which this service resolves to a
slug-validated path under ``worktrees/`` — raw paths never cross the API boundary.
"""

import re
import shlex
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Workspace, WorkspaceWorktree
from app.services.daytona_runtime import DaytonaRuntime
from app.services.repositories import RepositoryNotClonedError, sanitize_git_output
from app.services.workspaces import WorkspaceNotFoundError

GIT_TIMEOUT_SECONDS = 120
WORKTREES_DIR = "worktrees"
# Branch refs may use letters, digits and ``._/-`` only; no leading ``-``, no ``..`` sequence,
# no whitespace or shell/path metacharacters.
BRANCH_PATTERN = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")


class WorktreeError(Exception):
    pass


class WorktreeNotFoundError(WorktreeError):
    pass


class WorktreeAlreadyExistsError(WorktreeError):
    pass


class WorktreeLimitError(WorktreeError):
    pass


class InvalidBranchNameError(WorktreeError):
    pass


class CannotRemoveBaseWorktreeError(WorktreeError):
    pass


class WorktreeDirtyError(WorktreeError):
    pass


class CannotSyncBaseWorktreeError(WorktreeError):
    pass


class GitWorktreeError(WorktreeError):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class MergeConflictError(GitWorktreeError):
    """A merge/rebase left the worktree with conflicts the user must resolve manually."""


@dataclass(frozen=True)
class WorktreeInfo:
    id: UUID
    branch: str
    path: str
    base_ref: str
    is_base: bool
    status: str
    is_clean: bool
    ahead: int
    behind: int


def slugify_branch(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:50] or "feature"


def validate_branch(value: str, *, field: str = "branch") -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 200 or not BRANCH_PATTERN.fullmatch(candidate):
        raise InvalidBranchNameError(f"Invalid {field} name")
    if candidate.endswith("/") or candidate.endswith(".lock"):
        raise InvalidBranchNameError(f"Invalid {field} name")
    return candidate


class WorktreeService:
    def __init__(
        self,
        session: AsyncSession,
        runtime: DaytonaRuntime,
        settings: Settings,
    ) -> None:
        self.session = session
        self.runtime = runtime
        self.settings = settings

    # ----- public API ---------------------------------------------------------------

    async def list_worktrees(self, workspace_id: UUID, owner_id: UUID) -> list[WorktreeInfo]:
        workspace = await self._get_workspace(workspace_id, owner_id)
        await self._ensure_base_row(workspace)
        on_disk = await self._on_disk_worktrees(workspace)
        await self._reconcile(workspace, on_disk)

        rows = await self._list_rows(workspace_id)
        infos: list[WorktreeInfo] = []
        for row in rows:
            present = row.is_base or row.path in on_disk
            if present:
                is_clean, ahead, behind = await self._stats(workspace, row)
            else:
                is_clean, ahead, behind = True, 0, 0
            infos.append(
                WorktreeInfo(
                    id=row.id,
                    branch=row.branch,
                    path=row.path,
                    base_ref=row.base_ref,
                    is_base=row.is_base,
                    status=row.status,
                    is_clean=is_clean,
                    ahead=ahead,
                    behind=behind,
                )
            )
        # Base first, then by branch for a stable UI ordering.
        infos.sort(key=lambda i: (not i.is_base, i.branch))
        return infos

    async def create_worktree(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        branch: str,
        base_ref: str | None = None,
    ) -> WorkspaceWorktree:
        workspace = await self._get_workspace(workspace_id, owner_id)
        branch = validate_branch(branch, field="branch")
        base = validate_branch(base_ref, field="base") if base_ref else workspace.default_branch
        await self._ensure_cloned(workspace)

        slug = slugify_branch(branch)
        path = f"{WORKTREES_DIR}/{slug}"
        await self._assert_unique(workspace_id, branch, path)

        active = await self._count_feature_worktrees(workspace_id)
        if active >= self.settings.max_worktrees_per_workspace:
            raise WorktreeLimitError(
                f"Worktree limit reached ({self.settings.max_worktrees_per_workspace})"
            )

        await self.runtime.exec(
            workspace,
            f"mkdir -p ../{WORKTREES_DIR}",
            cwd=workspace.workspace_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        result = await self._run_git(
            workspace,
            ["worktree", "add", f"../{path}", "-b", branch, base],
            cwd=workspace.workspace_path,
        )
        if result.exit_code != 0:
            raise GitWorktreeError(
                sanitize_git_output(
                    f"git worktree add failed: {result.stderr or result.stdout}".strip()
                )
            )
        # Make the new branch push-ready: a credentialed ``git push`` creates and tracks the
        # remote branch instead of failing on a missing upstream.
        await self._run_git(workspace, ["config", "push.autoSetupRemote", "true"], cwd=path)
        await self._run_git(workspace, ["config", "push.default", "current"], cwd=path)

        row = WorkspaceWorktree(
            workspace_id=workspace_id,
            branch=branch,
            path=path,
            base_ref=base,
            is_base=False,
            status="active",
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            raise WorktreeAlreadyExistsError from error
        await self.session.refresh(row)
        return row

    async def remove_worktree(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        worktree_id: UUID,
        force: bool = False,
    ) -> WorkspaceWorktree:
        workspace = await self._get_workspace(workspace_id, owner_id)
        row = await self._get_row(workspace_id, worktree_id)
        if row.is_base:
            raise CannotRemoveBaseWorktreeError

        if not force:
            is_clean, _, _ = await self._stats(workspace, row)
            if not is_clean:
                raise WorktreeDirtyError

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(f"../{row.path}")
        result = await self._run_git(workspace, args, cwd=workspace.workspace_path)
        if result.exit_code != 0:
            raise GitWorktreeError(
                sanitize_git_output(
                    f"git worktree remove failed: {result.stderr or result.stdout}".strip()
                )
            )
        await self._run_git(workspace, ["worktree", "prune"], cwd=workspace.workspace_path)

        await self.session.delete(row)
        await self.session.commit()
        return row

    async def sync_from_base(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        worktree_id: UUID,
        strategy: str = "merge",
    ) -> str:
        """Bring a feature worktree up to date with its base branch.

        The caller is expected to have fetched ``origin`` first (the API layer does this via
        ``RepositoryService.fetch``), so this merges/rebases the already-fetched ``origin/<base>``
        entirely inside the worktree — the base clone is never checked out or dirtied. A conflict
        leaves the worktree mid-merge/rebase and raises ``MergeConflictError`` for the user to
        resolve in the editor/terminal.
        """
        workspace = await self._get_workspace(workspace_id, owner_id)
        row = await self._get_row(workspace_id, worktree_id)
        if row.is_base:
            raise CannotSyncBaseWorktreeError
        target = f"origin/{row.base_ref}"
        if strategy == "rebase":
            args = ["rebase", target]
        else:
            args = ["merge", "--no-edit", target]
        result = await self._run_git(workspace, args, cwd=row.path)
        if result.exit_code != 0:
            output = sanitize_git_output(f"{result.stdout}\n{result.stderr}".strip())
            if "conflict" in output.lower():
                raise MergeConflictError(
                    output or f"{strategy} left conflicts to resolve"
                )
            raise GitWorktreeError(f"git {strategy} failed: {output}".strip())
        return sanitize_git_output((result.stdout or "").strip())

    async def prune_worktrees(self, workspace_id: UUID, owner_id: UUID) -> None:
        workspace = await self._get_workspace(workspace_id, owner_id)
        await self._run_git(workspace, ["worktree", "prune"], cwd=workspace.workspace_path)
        on_disk = await self._on_disk_worktrees(workspace)
        await self._reconcile(workspace, on_disk)

    async def resolve_path(self, workspace: Workspace, worktree_id: UUID | None) -> str:
        """Map a worktree id to its confined working directory; ``None`` => base clone."""
        if worktree_id is None:
            return workspace.workspace_path
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            raise WorktreeNotFoundError
        return row.path

    async def get_worktree(
        self, workspace_id: UUID, owner_id: UUID, worktree_id: UUID
    ) -> WorkspaceWorktree:
        await self._get_workspace(workspace_id, owner_id)
        return await self._get_row(workspace_id, worktree_id)

    # ----- helpers ------------------------------------------------------------------

    async def _stats(
        self, workspace: Workspace, row: WorkspaceWorktree
    ) -> tuple[bool, int, int]:
        try:
            porcelain = await self.runtime.exec(
                workspace, "git status --porcelain", cwd=row.path, timeout=GIT_TIMEOUT_SECONDS
            )
            is_clean = not porcelain.stdout.strip()
        except Exception:
            is_clean = True
        ahead = behind = 0
        if not row.is_base:
            try:
                rev = await self.runtime.exec(
                    workspace,
                    "git rev-list --left-right --count "
                    f"{shlex.quote(row.base_ref)}...HEAD",
                    cwd=row.path,
                    timeout=GIT_TIMEOUT_SECONDS,
                )
                if rev.exit_code == 0:
                    parts = rev.stdout.split()
                    if len(parts) == 2:
                        behind, ahead = int(parts[0]), int(parts[1])
            except Exception:
                pass
        return is_clean, ahead, behind

    async def _on_disk_worktrees(self, workspace: Workspace) -> dict[str, str | None]:
        """Return ``{relative_path: branch_or_None}`` for every worktree git knows about."""
        result = await self._run_git(
            workspace, ["worktree", "list", "--porcelain"], cwd=workspace.workspace_path
        )
        if result.exit_code != 0:
            return {}
        home = self.settings.daytona_sandbox_home.rstrip("/")
        entries: dict[str, str | None] = {}
        cur_path: str | None = None
        cur_branch: str | None = None

        def flush() -> None:
            if cur_path is None:
                return
            rel = cur_path
            if rel.startswith(home + "/"):
                rel = rel[len(home) + 1 :]
            entries[rel] = cur_branch

        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                flush()
                cur_path = line[len("worktree ") :].strip()
                cur_branch = None
            elif line.startswith("branch "):
                cur_branch = line[len("branch ") :].strip().removeprefix("refs/heads/")
        flush()
        return entries

    async def _reconcile(self, workspace: Workspace, on_disk: dict[str, str | None]) -> None:
        """Adopt on-disk feature worktrees missing from the DB; drop DB rows gone from disk."""
        rows = await self._list_rows(workspace.id)
        known_paths = {row.path for row in rows}
        changed = False

        for row in rows:
            if not row.is_base and row.path not in on_disk:
                await self.session.delete(row)
                changed = True

        for rel, branch in on_disk.items():
            if rel == workspace.workspace_path or rel in known_paths:
                continue
            if not rel.startswith(f"{WORKTREES_DIR}/"):
                continue
            self.session.add(
                WorkspaceWorktree(
                    workspace_id=workspace.id,
                    branch=branch or rel.split("/", 1)[-1],
                    path=rel,
                    base_ref=workspace.default_branch,
                    is_base=False,
                    status="active",
                )
            )
            changed = True

        if changed:
            try:
                await self.session.commit()
            except IntegrityError:
                await self.session.rollback()

    async def _ensure_base_row(self, workspace: Workspace) -> None:
        result = await self.session.execute(
            select(WorkspaceWorktree).where(
                WorkspaceWorktree.workspace_id == workspace.id,
                WorkspaceWorktree.is_base.is_(True),
            )
        )
        if result.scalar_one_or_none() is not None:
            return
        self.session.add(
            WorkspaceWorktree(
                workspace_id=workspace.id,
                branch=workspace.default_branch,
                path=workspace.workspace_path,
                base_ref=workspace.default_branch,
                is_base=True,
                status="active",
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()

    async def _assert_unique(self, workspace_id: UUID, branch: str, path: str) -> None:
        result = await self.session.execute(
            select(WorkspaceWorktree.id).where(
                WorkspaceWorktree.workspace_id == workspace_id,
                (WorkspaceWorktree.branch == branch) | (WorkspaceWorktree.path == path),
            )
        )
        if result.first() is not None:
            raise WorktreeAlreadyExistsError

    async def _count_feature_worktrees(self, workspace_id: UUID) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(WorkspaceWorktree)
            .where(
                WorkspaceWorktree.workspace_id == workspace_id,
                WorkspaceWorktree.is_base.is_(False),
            )
        )
        return int(result.scalar_one() or 0)

    async def _list_rows(self, workspace_id: UUID) -> list[WorkspaceWorktree]:
        result = await self.session.execute(
            select(WorkspaceWorktree).where(WorkspaceWorktree.workspace_id == workspace_id)
        )
        return list(result.scalars().all())

    async def _get_row(self, workspace_id: UUID, worktree_id: UUID) -> WorkspaceWorktree:
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace_id:
            raise WorktreeNotFoundError
        return row

    async def _ensure_cloned(self, workspace: Workspace) -> None:
        result = await self.runtime.exec(
            workspace,
            "test -e .git",
            cwd=workspace.workspace_path,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        if result.exit_code != 0:
            raise RepositoryNotClonedError

    async def _run_git(self, workspace: Workspace, args: list[str], *, cwd: str):
        command = "git " + " ".join(shlex.quote(arg) for arg in args)
        return await self.runtime.exec(
            workspace, command, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS
        )

    async def _get_workspace(self, workspace_id: UUID, owner_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        return workspace
