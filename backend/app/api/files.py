from typing import Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
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
)
from app.schemas.workspaces import DeleteResponse
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

router = APIRouter(prefix="/workspaces/{workspace_id}/files", tags=["files"])

DB_SESSION_DEP = Depends(get_db_session)
SETTINGS_DEP = Depends(get_settings)


class FileServiceProtocol(Protocol):
    async def tree(self, workspace_id: UUID, owner_id: UUID, path: str | None = None) -> FileNode:
        ...

    async def read(self, workspace_id: UUID, owner_id: UUID, path: str) -> FileReadResponse: ...

    async def write(self, workspace_id: UUID, owner_id: UUID, path: str, content: str) -> None:
        ...

    async def upload(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        content: bytes,
        filename: str | None = None,
    ) -> str: ...

    async def create_folder(self, workspace_id: UUID, owner_id: UUID, path: str) -> None: ...

    async def rename(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        path: str,
        new_path: str,
    ) -> str: ...

    async def delete(self, workspace_id: UUID, owner_id: UUID, path: str) -> None: ...

    async def search(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        query: str,
    ) -> list[FileSearchResult]: ...

    async def download_path(self, workspace_id: UUID, owner_id: UUID, path: str): ...


async def get_file_service(
    session: AsyncSession = DB_SESSION_DEP,
    settings: Settings = SETTINGS_DEP,
) -> FileServiceProtocol:
    return FileService(session=session, settings=settings)


FILE_SERVICE_DEP = Depends(get_file_service)


def translate_file_error(error: Exception) -> HTTPException:
    if isinstance(error, WorkspaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
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
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is too large",
        )
    if isinstance(error, FileTreeLimitExceededError):
        return HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File tree is too large",
        )
    if isinstance(error, (FilePathError, FileIgnoredPathError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File path is invalid")
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Request failed")


@router.get("/tree", response_model=FileNode)
async def get_file_tree(
    workspace_id: UUID,
    path: str = Query(default=""),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileNode:
    try:
        return await service.tree(workspace_id, current_user.id, path)
    except (
        WorkspaceNotFoundError,
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
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileReadResponse:
    try:
        return await service.read(workspace_id, current_user.id, path)
    except (
        WorkspaceNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None


@router.post("/write", response_model=FileOperationResponse)
async def write_file(
    workspace_id: UUID,
    payload: FileWriteRequest,
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        await service.write(workspace_id, current_user.id, payload.path, payload.content)
    except (
        WorkspaceNotFoundError,
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


@router.post("/upload", response_model=FileOperationResponse)
async def upload_file(
    workspace_id: UUID,
    request: Request,
    path: str = Query(default="", max_length=4096),
    filename: str | None = Query(default=None, min_length=1, max_length=255),
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        uploaded_path = await service.upload(
            workspace_id,
            current_user.id,
            path,
            await request.body(),
            filename,
        )
    except (
        WorkspaceNotFoundError,
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
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileResponse:
    try:
        target = await service.download_path(workspace_id, current_user.id, path)
    except (
        WorkspaceNotFoundError,
        FileWorkspaceUnavailableError,
        FileNotFoundErrorInWorkspace,
        FileConflictError,
        FileTooLargeError,
        FileTreeLimitExceededError,
        FilePathError,
        FileIgnoredPathError,
    ) as error:
        raise translate_file_error(error) from None
    return FileResponse(target, filename=target.name)


@router.post("/folders", response_model=FileOperationResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    workspace_id: UUID,
    payload: FolderCreateRequest,
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        await service.create_folder(workspace_id, current_user.id, payload.path)
    except (
        WorkspaceNotFoundError,
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
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> FileOperationResponse:
    try:
        new_path = await service.rename(
            workspace_id,
            current_user.id,
            payload.path,
            payload.new_path,
        )
    except (
        WorkspaceNotFoundError,
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
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> DeleteResponse:
    try:
        await service.delete(workspace_id, current_user.id, path)
    except (
        WorkspaceNotFoundError,
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
    current_user: User = CURRENT_USER_DEP,
    service: FileServiceProtocol = FILE_SERVICE_DEP,
) -> list[FileNode]:
    try:
        results = await service.search(workspace_id, current_user.id, q)
    except (
        WorkspaceNotFoundError,
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
