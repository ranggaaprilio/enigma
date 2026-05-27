import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Workspace
from app.schemas.files import FileNode, FileReadResponse
from app.services.workspaces import WorkspaceNotFoundError

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
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def tree(self, workspace_id: UUID, owner_id: UUID, path: str | None = None) -> FileNode:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        target = self._resolve_existing(workspace_root, path or "")
        if not target.is_dir():
            raise FilePathError

        counter = 0

        def build_node(item: Path) -> FileNode:
            nonlocal counter
            counter += 1
            if counter > self.settings.file_tree_max_entries:
                raise FileTreeLimitExceededError
            stat = item.stat()
            children: list[FileNode] = []
            if item.is_dir():
                children = [
                    build_node(child)
                    for child in sorted(
                        item.iterdir(),
                        key=lambda child_path: (not child_path.is_dir(), child_path.name.lower()),
                    )
                    if not self._is_ignored_relative(workspace_root, child)
                ]
            return FileNode(
                path=self._relative_path(workspace_root, item),
                name=item.name or workspace_root.name,
                type="directory" if item.is_dir() else "file",
                size=None if item.is_dir() else stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                children=children,
            )

        return build_node(target)

    async def read(self, workspace_id: UUID, owner_id: UUID, path: str) -> FileReadResponse:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        target = self._resolve_existing(workspace_root, path)
        if not target.is_file():
            raise FilePathError
        stat = target.stat()
        if stat.st_size > self.settings.file_max_read_bytes:
            raise FileTooLargeError
        return FileReadResponse(
            path=self._relative_path(workspace_root, target),
            name=target.name,
            type="file",
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            content=target.read_text(encoding="utf-8", errors="replace"),
        )

    async def write(self, workspace_id: UUID, owner_id: UUID, path: str, content: str) -> None:
        if len(content.encode("utf-8")) > self.settings.file_max_upload_bytes:
            raise FileTooLargeError
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        target = self._resolve_target(workspace_root, path)
        if target.exists() and target.is_dir():
            raise FilePathError
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._log("file_write", workspace_id, owner_id, self._relative_path(workspace_root, target))

    async def upload(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        content: bytes,
        filename: str | None = None,
    ) -> str:
        if len(content) > self.settings.file_max_upload_bytes:
            raise FileTooLargeError
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        upload_path = f"{path.rstrip('/')}/{filename.strip()}" if filename else path
        target = self._resolve_target(workspace_root, upload_path)
        if target.exists() and target.is_dir():
            raise FilePathError
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        relative_path = self._relative_path(workspace_root, target)
        self._log("file_upload", workspace_id, owner_id, relative_path)
        return relative_path

    async def create_folder(self, workspace_id: UUID, owner_id: UUID, path: str) -> None:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        target = self._resolve_target(workspace_root, path)
        if target.exists():
            raise FileConflictError
        target.mkdir(parents=True)
        self._log(
            "file_folder_create",
            workspace_id,
            owner_id,
            self._relative_path(workspace_root, target),
        )

    async def rename(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        new_path: str,
    ) -> str:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        source = self._resolve_existing(workspace_root, path)
        target = self._resolve_target(workspace_root, new_path)
        if target.exists():
            raise FileConflictError
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        relative_path = self._relative_path(workspace_root, target)
        logger.info(
            "file_rename",
            workspace_id=str(workspace_id),
            user_id=str(owner_id),
            path=self._relative_path(workspace_root, source),
            new_path=relative_path,
            ok=True,
        )
        return relative_path

    async def delete(self, workspace_id: UUID, owner_id: UUID, path: str) -> None:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        target = self._resolve_existing(workspace_root, path)
        if target == workspace_root:
            raise FilePathError
        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()
        self._log("file_delete", workspace_id, owner_id, path)

    async def search(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        query: str,
    ) -> list[FileSearchResult]:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []
        results: list[FileSearchResult] = []
        visited = 0
        for item in self._walk_visible(workspace_root):
            visited += 1
            if visited > self.settings.file_tree_max_entries:
                break
            if normalized_query not in item.name.lower():
                continue
            stat = item.stat()
            results.append(
                FileSearchResult(
                    path=self._relative_path(workspace_root, item),
                    name=item.name,
                    type="directory" if item.is_dir() else "file",
                    size=None if item.is_dir() else stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                )
            )
        return results

    async def download_path(self, workspace_id: UUID, owner_id: UUID, path: str) -> Path:
        workspace_root = await self._workspace_root(workspace_id, owner_id)
        target = self._resolve_existing(workspace_root, path)
        if not target.is_file():
            raise FilePathError
        self._log(
            "file_download",
            workspace_id,
            owner_id,
            self._relative_path(workspace_root, target),
        )
        return target

    async def _workspace_root(self, workspace_id: UUID, owner_id: UUID) -> Path:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != owner_id:
            raise WorkspaceNotFoundError
        if workspace.local_path is None:
            raise FileWorkspaceUnavailableError
        root = Path(workspace.local_path).resolve()
        if not root.is_dir():
            raise FileWorkspaceUnavailableError
        return root

    def _resolve_existing(self, workspace_root: Path, path: str) -> Path:
        self._validate_relative_path(path)
        candidate = (workspace_root / path).resolve()
        self._ensure_inside(workspace_root, candidate)
        if self._is_ignored_relative(workspace_root, candidate):
            raise FileIgnoredPathError
        if not candidate.exists():
            raise FileNotFoundErrorInWorkspace
        return candidate

    def _resolve_target(self, workspace_root: Path, path: str) -> Path:
        self._validate_relative_path(path)
        candidate = workspace_root / path
        parent = candidate.parent.resolve()
        self._ensure_inside(workspace_root, parent)
        target = parent / candidate.name
        if self._is_ignored_relative(workspace_root, target):
            raise FileIgnoredPathError
        return target

    def _validate_relative_path(self, path: str) -> None:
        candidate = Path(path)
        if candidate.is_absolute() or any(part in {"..", ""} for part in candidate.parts):
            raise FilePathError

    def _ensure_inside(self, workspace_root: Path, target: Path) -> None:
        if target != workspace_root and workspace_root not in target.parents:
            raise FilePathError

    def _is_ignored_relative(self, workspace_root: Path, target: Path) -> bool:
        if target.exists():
            resolved = target.resolve()
            if resolved != workspace_root and workspace_root not in resolved.parents:
                return True
        try:
            relative = target.relative_to(workspace_root)
        except ValueError:
            return True
        return any(part in IGNORED_PATH_PARTS for part in relative.parts)

    def _relative_path(self, workspace_root: Path, target: Path) -> str:
        if target == workspace_root:
            return ""
        try:
            return target.relative_to(workspace_root).as_posix()
        except ValueError:
            return ""

    def _walk_visible(self, workspace_root: Path):
        for current_root, dirnames, filenames in os.walk(workspace_root):
            current_path = Path(current_root)
            dirnames[:] = sorted(
                (
                    dirname
                    for dirname in dirnames
                    if not self._is_ignored_relative(workspace_root, current_path / dirname)
                ),
                key=str.lower,
            )
            for dirname in dirnames:
                yield current_path / dirname
            for filename in sorted(filenames, key=str.lower):
                item = current_path / filename
                if not self._is_ignored_relative(workspace_root, item):
                    yield item

    def _log(self, event: str, workspace_id: UUID, user_id: UUID, path: str) -> None:
        logger.info(event, workspace_id=str(workspace_id), user_id=str(user_id), path=path, ok=True)
