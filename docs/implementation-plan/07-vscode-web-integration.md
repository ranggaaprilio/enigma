# Epic 6 - VS Code Web Integration

## Goal

Embed code-server or OpenVSCode Server into the dashboard so developers can manually inspect and edit workspace files.

## User Stories

- As a developer, I can open VS Code Web inside the dashboard.
- As a developer, I can edit files in the workspace.
- As a developer, changes made in VS Code are visible to Git diff and AI agent.

## MVP Scope

- Basic code-server container or process.
- One VS Code Web instance per workspace.
- Iframe embed in dashboard.
- Workspace directory mounted into code-server.

## Backend Tasks

- Create `editor_sessions` table:
  - `id`
  - `workspace_id`
  - `url`
  - `status`
  - `started_at`
  - `last_active_at`
- Create endpoints:
  - `POST /workspaces/{workspace_id}/editor/start`
  - `GET /workspaces/{workspace_id}/editor/status`
  - `POST /workspaces/{workspace_id}/editor/stop`
- Start code-server for workspace directory.
- Generate internal editor URL.
- Protect editor access with auth token or reverse proxy auth.
- Ensure code-server has restricted file access.

## Frontend Tasks

- Add VS Code Web tab in center panel.
- Render code-server iframe.
- Add loading and failed states.
- Add open/restart editor button.
- Sync dashboard theme preference where possible.

## Database Tasks

- Add migration for `editor_sessions`.
- Add index on `workspace_id`.

## Acceptance Criteria

- User can start editor from workspace dashboard.
- Editor opens inside the dashboard.
- Editor shows the workspace repository files.
- File edits appear in repository diff.
- Editor URL is not publicly accessible without authorization.

## Future Enhancements

- Preinstalled extensions.
- User-installed extensions.
- Per-user editor settings.
- Open specific file from file explorer.

