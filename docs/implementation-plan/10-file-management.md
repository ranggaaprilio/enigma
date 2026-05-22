# Epic 9 - File Management

## Goal

Provide dashboard-level file browsing and basic file operations for workspace repositories.

## User Stories

- As a developer, I can browse workspace files.
- As a developer, I can search files by name and content.
- As a developer, I can upload and download files.
- As a developer, I can create, rename, and delete folders.

## MVP Scope

- File explorer sidebar.
- Read file tree.
- Search by file name.
- Download files.
- Basic create/rename/delete operations with confirmation.

## Backend Tasks

- Create file management endpoints:
  - `GET /workspaces/{workspace_id}/files/tree`
  - `GET /workspaces/{workspace_id}/files/read?path=...`
  - `POST /workspaces/{workspace_id}/files/write`
  - `POST /workspaces/{workspace_id}/files/upload`
  - `GET /workspaces/{workspace_id}/files/download?path=...`
  - `POST /workspaces/{workspace_id}/files/folders`
  - `PATCH /workspaces/{workspace_id}/files/rename`
  - `DELETE /workspaces/{workspace_id}/files`
  - `GET /workspaces/{workspace_id}/files/search`
- Prevent path traversal outside workspace directory.
- Add file size limits.
- Exclude ignored paths like `.git`, `node_modules`, `.venv`, build folders.
- Log destructive operations.

## Frontend Tasks

- Build file explorer in left sidebar.
- Add folder expand/collapse.
- Add file search input.
- Add context menu for file operations.
- Add upload button.
- Add download action.
- Connect file click to VS Code Web when editor epic is ready.

## Database Tasks

- No required tables for MVP.
- Use audit log for create, rename, delete, upload, download events.

## Acceptance Criteria

- User can browse files for owned workspace.
- User cannot access files outside workspace path.
- Search returns matching files.
- Upload and download work for normal project files.
- Delete and rename require explicit confirmation.

## Future Enhancements

- Content search using ripgrep.
- Inline file preview.
- Drag and drop upload.
- Bulk operations.

