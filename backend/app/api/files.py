from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.api.rate_limits import SANDBOX_RATE_LIMIT_DEP
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import User
from app.schemas.files import (
    FileNode,
    FileOperationResponse,
    FileReadResponse,
    FileRenameRequest,
    FileWriteRequest,
    FolderCreateRequest,
    normalize_path,
)
from app.schemas.workspaces import DeleteResponse
from app.services.daytona_runtime import get_daytona_runtime
from app.services.files import (
    FileConflictError,
    FileIgnoredPathError,
    FileNotFoundErrorInWorkspace,
    FilePathError,
    FileSearchResult,
    FileService,
    FileTooLargeError,
    FileTreeLimitExceededError,
    FileWorkspaceUnavailableError,
)
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError

router = APIRouter(prefix="/workspaces/{workspace_id}/files", tags=["files"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class FileServiceProtocol(Protocol):
    settings: Settings

    async def tree(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str | None = None,
        worktree_id: UUID | None = None,
    ) -> FileNode: ...

    async def read(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> FileReadResponse: ...

    async def write(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        content: str,
        worktree_id: UUID | None = None,
    ) -> None: ...

    async def upload(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        content: bytes,
        filename: str | None = None,
        worktree_id: UUID | None = None,
    ) -> str: ...

    async def create_folder(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> None: ...

    async def rename(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        new_path: str,
        worktree_id: UUID | None = None,
    ) -> str: ...

    async def delete(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> None: ...

    async def search(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        query: str,
        worktree_id: UUID | None = None,
    ) -> list[FileSearchResult]: ...

    async def download_path(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        worktree_id: UUID | None = None,
    ) -> tuple[str, bytes]: ...


async def get_file_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> FileServiceProtocol:
    return FileService(session=session, settings=settings, runtime=get_daytona_runtime())


FILE_SERVICE_DEP = Depends(get_file_service)


def normalize_query_path(path: str) -> str:
    return normalize_path(path)


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise FileTooLargeError
        except ValueError as error:
            raise FilePathError from error

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise FileTooLargeError
    return bytes(body)


def translate_file_error(error: Exception) -> HTTPException:
    if isinstance(error, WorkspaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    if isinstance(error, WorktreeNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worktree not found")
    if isinstance(error, FileWorkspaceUnavailableError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workspace directory is unavailable",
        )
    if isinstance(error, FileNotFoundErrorInWorkspace):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    if isinstance(error, FileConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="File already exists")
    if isinstance(error, FileTooLargeError):
        return HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File is too large",
        )
    if isinstance(error, FileTreeLimitExceededError):
        return HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File tree is too large",
        )
    if isinstance(error, (FilePathError, FileIgnoredPathError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File path is invalid")
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


@router.get("/tree", response_model=FileNode)
async def get_file_tree(
    workspace_id: UUID,
    path: str = Query(default=""),
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileNode:
    try:
        return await service.tree(
            workspace_id, current_user.id, normalize_query_path(path), worktree_id=worktree_id
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None


@router.get("/read", response_model=FileReadResponse)
async def read_file(
    workspace_id: UUID,
    path: str = Query(min_length=1, max_length=4096),
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileReadResponse:
    try:
        return await service.read(
            workspace_id, current_user.id, normalize_query_path(path), worktree_id=worktree_id
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None


@router.post("/write", response_model=FileOperationResponse, dependencies=[SANDBOX_RATE_LIMIT_DEP])
async def write_file(
    workspace_id: UUID,
    payload: FileWriteRequest,
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        await service.write(
            workspace_id,
            current_user.id,
            payload.path,
            payload.content,
            worktree_id=worktree_id,
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return FileOperationResponse(ok=True, path=payload.path)


@router.post("/upload", response_model=FileOperationResponse, dependencies=[SANDBOX_RATE_LIMIT_DEP])
async def upload_file(
    workspace_id: UUID,
    request: Request,
    path: str = Query(default="", max_length=4096),
    filename: str | None = Query(default=None, min_length=1, max_length=255),
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        content = await read_limited_body(request, service.settings.file_max_upload_bytes)
        uploaded_path = await service.upload(
            workspace_id,
            current_user.id,
            normalize_query_path(path),
            content,
            filename,
            worktree_id=worktree_id,
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return FileOperationResponse(ok=True, path=uploaded_path)


@router.get("/download")
async def download_file(
    workspace_id: UUID,
    path: str = Query(min_length=1, max_length=4096),
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> Response:
    try:
        filename, content = await service.download_path(
            workspace_id,
            current_user.id,
            normalize_query_path(path),
            worktree_id=worktree_id,
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/folders", response_model=FileOperationResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    workspace_id: UUID,
    payload: FolderCreateRequest,
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        await service.create_folder(
            workspace_id, current_user.id, payload.path, worktree_id=worktree_id
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return FileOperationResponse(ok=True, path=payload.path)


@router.patch("/rename", response_model=FileOperationResponse)
async def rename_file(
    workspace_id: UUID,
    payload: FileRenameRequest,
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        new_path = await service.rename(
            workspace_id,
            current_user.id,
            payload.path,
            payload.new_path,
            worktree_id=worktree_id,
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return FileOperationResponse(ok=True, path=new_path)


@router.delete("", response_model=DeleteResponse)
async def delete_file(
    workspace_id: UUID,
    path: str = Query(min_length=1, max_length=4096),
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete(
            workspace_id, current_user.id, normalize_query_path(path), worktree_id=worktree_id
        )
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return DeleteResponse(ok=True)


@router.get("/search", response_model=list[FileNode])
async def search_files(
    workspace_id: UUID,
    q: str = Query(min_length=1, max_length=255),
    worktree_id: UUID | None = Query(default=None),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> list[FileNode]:
    try:
        results = await service.search(workspace_id, current_user.id, q, worktree_id=worktree_id)
    except (
        WorkspaceNotFoundError,
        WorktreeNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return [
        FileNode(
            path=result.path,
            name=result.name,
            type=result.type,
            size=result.size,
            modified_at=result.modified_at,
            children=[],
        )
        for result in results
    ]
