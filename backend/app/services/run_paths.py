"""Working-directory resolution for everything a workspace can run.

A run's working directory is composed from three independently-owned pieces::

    <worktree path | workspace_path> / <workspace.app_root> / <command.cwd>

Before this module each caller composed the first piece by hand — ``WorktreeService.resolve_path``,
``FileService._resolve_root`` and ``DevServerService._resolve_worktree`` are three copies of the
same lookup. Epic 10 adds two more segments, so the composition lives here once and
``commands``/``dev_server``/``terminal`` share it.

Two invariants hold throughout:

* A worktree is addressed **only** by its database id, confined to the owning workspace. Raw paths
  never cross the API boundary.
* ``app_root`` and ``cwd`` are relative, ``..``-free and non-absolute. They are validated on write
  by the request schemas *and* again here on read, so a row written before a validator changed can
  still never escape the checkout.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Workspace, WorkspaceWorktree
from app.services.worktrees import WorktreeNotFoundError


class RunPathError(Exception):
    """A configured subdirectory is absolute, escapes the checkout, or is otherwise unusable."""


def validate_relative_subpath(value: str | None) -> str | None:
    """Normalise a repo-relative subdirectory, or ``None`` when it is empty.

    Stricter than ``FileService._normalize_relative``, which strips a leading slash and treats
    the rest as relative. These values are typed by a user rather than sent by the file tree, so
    an absolute path is rejected instead of silently reinterpreted — ``/etc`` becoming
    ``workspace/etc`` would be contained but baffling. A trailing slash is still forgiven.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("/"):
        raise RunPathError("Path must be relative to the repository, not absolute")
    candidate = candidate.rstrip("/")
    if not candidate:
        raise RunPathError("Path must be relative to the repository, not absolute")
    pure = PurePosixPath(candidate)
    if pure.is_absolute() or ".." in pure.parts:
        raise RunPathError("Path must stay inside the workspace checkout")
    return pure.as_posix()


def join_subpath(root: str, *segments: str | None) -> str:
    """Append validated relative segments to a resolved checkout root."""
    parts = [root.rstrip("/")]
    for segment in segments:
        normalized = validate_relative_subpath(segment)
        if normalized:
            parts.append(normalized)
    return "/".join(parts)


async def resolve_worktree(
    session: AsyncSession, workspace: Workspace, worktree_id: UUID | None
) -> tuple[str, str, UUID | None]:
    """Map a worktree id to ``(checkout path, branch, worktree_id)``; ``None`` => base clone.

    Mirrors ``WorktreeService.resolve_path`` / ``DevServerService._resolve_worktree``.
    """
    if worktree_id is None:
        return workspace.workspace_path, workspace.default_branch, None
    row = await session.get(WorkspaceWorktree, worktree_id)
    if row is None or row.workspace_id != workspace.id:
        raise WorktreeNotFoundError
    return row.path, row.branch, row.id


async def resolve_run_cwd(
    session: AsyncSession,
    workspace: Workspace,
    *,
    worktree_id: UUID | None = None,
    command_cwd: str | None = None,
) -> tuple[str, str, UUID | None, str]:
    """Resolve where a command runs.

    Returns ``(cwd, relative_cwd, worktree_id, branch)`` where ``cwd`` is the path inside the
    sandbox and ``relative_cwd`` is the ``app_root``/``command_cwd`` portion recorded on the run
    so history can explain where it executed.
    """
    checkout, branch, resolved_id = await resolve_worktree(session, workspace, worktree_id)
    app_root = validate_relative_subpath(getattr(workspace, "app_root", None))
    command_relative = validate_relative_subpath(command_cwd)
    relative = "/".join(part for part in (app_root, command_relative) if part)
    return join_subpath(checkout, relative), relative, resolved_id, branch
