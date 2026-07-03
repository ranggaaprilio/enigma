import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Workspace, WorkspaceWorktree
from app.schemas.files import FileNode, FileReadResponse
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

logger = structlog.get_logger(__name__)

IGNORED_PATH_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".next",
    ".nuxt",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}


class FilePathError(Exception):
    pass


class FileIgnoredPathError(Exception):
    pass


class FileNotFoundErrorInWorkspace(Exception):
    pass


class FileConflictError(Exception):
    pass


class FileTooLargeError(Exception):
    pass


class FileWorkspaceUnavailableError(Exception):
    pass


class FileTreeLimitExceededError(Exception):
    pass


@dataclass
class FileSearchResult:
    path: str
    name: str
    type: str
    size: int | None
    modified_at: datetime


class FileService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: DaytonaRuntime | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_daytona_runtime()

    async def tree(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str | None = None,
        worktree_id: UUID | None = None,
    ) -> FileNode:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        root_relative = self._normalize_relative(path or "", allow_empty=True)
        root_remote = self._remote_path(root, root_relative)
        semaphore = asyncio.Semaphore(max(1, self.settings.file_tree_concurrency))
        counter = 0

        def count_entry() -> None:
            nonlocal counter
            counter += 1
            if counter > self.settings.file_tree_max_entries:
                raise FileTreeLimitExceededError

        async def build_node(info: object, relative_path: str) -> FileNode:
            count_entry()
            is_dir = self._is_dir(info)
            children: list[FileNode] = []
            if is_dir:
                async with semaphore:
                    entries = await sandbox.fs.list_files(
                        self._remote_path(root, relative_path)
                    )
                visible = [
                    entry
                    for entry in entries
                    if not self._is_ignored(self._entry_relative(relative_path, entry))
                ]
                visible.sort(key=lambda entry: (not self._is_dir(entry), self._name(entry).lower()))
                children = list(
                    await asyncio.gather(
                        *(
                            build_node(entry, self._entry_relative(relative_path, entry))
                            for entry in visible
                        )
                    )
                )
            return FileNode(
                path=relative_path,
                name=self._name(info) or workspace.name,
                type="directory" if is_dir else "file",
                size=None if is_dir else self._size(info),
                modified_at=self._modified_at(info),
                children=children,
            )

        try:
            root_info = await sandbox.fs.get_file_info(root_remote)
        except Exception as error:
            raise FileNotFoundErrorInWorkspace from error
        return await build_node(root_info, root_relative)

    async def read(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> FileReadResponse:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        relative = self._normalize_relative(path)
        remote = self._remote_path(root, relative)
        try:
            info = await sandbox.fs.get_file_info(remote)
            if self._is_symlink(info):
                raise FilePathError
            if self._is_dir(info):
                raise FilePathError
            size = self._size(info)
            if size > self.settings.file_max_read_bytes:
                raise FileTooLargeError
            content = await sandbox.fs.download_file(remote)
        except (FilePathError, FileTooLargeError):
            raise
        except Exception as error:
            raise FileNotFoundErrorInWorkspace from error
        data = content or b""
        return FileReadResponse(
            path=relative,
            name=PurePosixPath(relative).name,
            type="file",
            size=len(data),
            modified_at=self._modified_at(info),
            content=data.decode("utf-8", errors="replace"),
        )

    async def write(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        content: str,
        worktree_id: UUID | None = None,
    ) -> None:
        await self.upload(
            workspace_id, owner_id, path, content.encode("utf-8"), worktree_id=worktree_id
        )
        await self._audit(owner_id, workspace_id, "file_write", path)

    async def upload(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        content: bytes,
        filename: str | None = None,
        worktree_id: UUID | None = None,
    ) -> str:
        if len(content) > self.settings.file_max_upload_bytes:
            raise FileTooLargeError
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        upload_path = f"{path.rstrip('/')}/{filename.strip()}" if filename else path
        relative = self._normalize_relative(upload_path)
        parent = PurePosixPath(relative).parent.as_posix()
        if parent != ".":
            await sandbox.fs.create_folder(self._remote_path(root, parent), "755")
        await sandbox.fs.upload_file(content, self._remote_path(root, relative))
        self._log("file_upload", workspace_id, owner_id, relative)
        await self._audit(owner_id, workspace_id, "file_upload", relative)
        return relative

    async def create_folder(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> None:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        relative = self._normalize_relative(path)
        try:
            await sandbox.fs.get_file_info(self._remote_path(root, relative))
        except Exception:
            await sandbox.fs.create_folder(self._remote_path(root, relative), "755")
        else:
            raise FileConflictError
        await self._audit(owner_id, workspace_id, "file_folder_create", relative)

    async def rename(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        new_path: str,
        worktree_id: UUID | None = None,
    ) -> str:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        source = self._normalize_relative(path)
        destination = self._normalize_relative(new_path)
        if destination == source or destination.startswith(f"{source}/"):
            raise FilePathError
        try:
            await sandbox.fs.get_file_info(self._remote_path(root, destination))
        except Exception:
            pass
        else:
            raise FileConflictError
        await sandbox.fs.move_files(
            self._remote_path(root, source),
            self._remote_path(root, destination),
        )
        await self._audit(
            owner_id,
            workspace_id,
            "file_rename",
            destination,
            {"from_path": source},
        )
        return destination

    async def delete(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> None:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        relative = self._normalize_relative(path)
        await sandbox.fs.delete_file(self._remote_path(root, relative), recursive=True)
        await self._audit(owner_id, workspace_id, "file_delete", relative)

    async def search(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        query: str,
        worktree_id: UUID | None = None,
    ) -> list[FileSearchResult]:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        normalized = query.strip()
        if not normalized:
            return []
        matches = await sandbox.fs.find_files(root, f"*{normalized}*")
        results: list[FileSearchResult] = []
        for match in matches[: self.settings.file_tree_max_entries]:
            path = str(getattr(match, "file", getattr(match, "path", "")))
            relative = self._relative_from_remote(root, path)
            if not relative or self._is_ignored(relative):
                continue
            try:
                info = await sandbox.fs.get_file_info(path)
            except Exception:
                continue
            is_dir = self._is_dir(info)
            results.append(
                FileSearchResult(
                    path=relative,
                    name=PurePosixPath(relative).name,
                    type="directory" if is_dir else "file",
                    size=None if is_dir else self._size(info),
                    modified_at=self._modified_at(info),
                )
            )
        return results

    async def download_path(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> tuple[str, bytes]:
        workspace, sandbox, root = await self._workspace(workspace_id, owner_id, worktree_id)
        relative = self._normalize_relative(path)
        try:
            content = await sandbox.fs.download_file(self._remote_path(root, relative))
        except Exception as error:
            raise FileNotFoundErrorInWorkspace from error
        return PurePosixPath(relative).name, content or b""

    async def _workspace(
        self, workspace_id: UUID, owner_id: UUID, worktree_id: UUID | None = None
    ):
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        root = await self._resolve_root(workspace, worktree_id)
        try:
            sandbox = await self.runtime.get_sandbox(workspace)
        except DaytonaRuntimeError as error:
            raise FileWorkspaceUnavailableError from error
        return workspace, sandbox, root

    async def _resolve_root(self, workspace: Workspace, worktree_id: UUID | None) -> str:
        """Map a worktree id to its confined working directory; ``None`` => base clone.

        Mirrors ``WorktreeService.resolve_path`` / ``DevServerService._resolve_worktree`` —
        raw paths never cross the API; a worktree is only ever targeted by its database id,
        confined to the workspace."""
        if worktree_id is None:
            return workspace.workspace_path
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            raise WorktreeNotFoundError
        return row.path

    def _normalize_relative(self, path: str, *, allow_empty: bool = False) -> str:
        candidate = path.strip().strip("/")
        if not candidate and allow_empty:
            return ""
        pure = PurePosixPath(candidate)
        if not candidate or pure.is_absolute() or ".." in pure.parts:
            raise FilePathError
        normalized = pure.as_posix()
        if self._is_ignored(normalized):
            raise FileIgnoredPathError
        return normalized

    def _remote_path(self, root: str, relative: str) -> str:
        normalized = relative.strip("/")
        if not normalized:
            return root
        return f"{root}/{normalized}"

    def _relative_from_remote(self, root: str, remote: str) -> str:
        prefix = root.rstrip("/") + "/"
        return remote[len(prefix) :] if remote.startswith(prefix) else remote.lstrip("/")

    def _entry_relative(self, parent: str, entry) -> str:
        entry_path = str(getattr(entry, "path", ""))
        if entry_path.startswith("/"):
            name = PurePosixPath(entry_path).name
        else:
            name = self._name(entry)
        return f"{parent}/{name}".strip("/")

    def _name(self, info) -> str:
        return str(getattr(info, "name", PurePosixPath(str(getattr(info, "path", ""))).name))

    def _is_dir(self, info) -> bool:
        return bool(getattr(info, "is_dir", getattr(info, "is_directory", False)))

    def _is_symlink(self, info) -> bool:
        return bool(getattr(info, "is_symlink", False))

    def _size(self, info) -> int:
        return int(getattr(info, "size", 0) or 0)

    def _modified_at(self, info) -> datetime:
        value = getattr(info, "modified_at", getattr(info, "mod_time", None))
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass
        return datetime.now(UTC)

    def _is_ignored(self, path: str) -> bool:
        return any(part in IGNORED_PATH_PARTS for part in PurePosixPath(path).parts)

    def _log(self, event: str, workspace_id: UUID, user_id: UUID, path: str) -> None:
        logger.info(event, workspace_id=str(workspace_id), user_id=str(user_id), path=path, ok=True)

    async def _audit(
        self,
        user_id: UUID,
        workspace_id: UUID,
        action: str,
        path: str,
        metadata: dict | None = None,
    ) -> None:
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action=action,
            target_type="file",
            target_id=path,
            metadata=metadata or {"path": path},
        )
