# Epic 5 - Terminal Access

## Goal

Provide browser-based terminal access to each workspace for debugging and command execution.

## User Stories

- As a developer, I can open a terminal in the dashboard.
- As a developer, I can run commands inside the workspace directory.
- As a developer, my terminal session survives page refresh.

## MVP Scope

- Single terminal session per workspace.
- xterm.js frontend.
- FastAPI WebSocket backend.
- Session persistence through tmux.

## Backend Tasks

- Create `terminal_sessions` table:
  - `id`
  - `workspace_id`
  - `user_id`
  - `name`
  - `tmux_session_name`
  - `status`
  - `created_at`
  - `last_active_at`
- Create endpoints:
  - `POST /workspaces/{workspace_id}/terminal/sessions`
  - `GET /workspaces/{workspace_id}/terminal/sessions`
  - `DELETE /workspaces/{workspace_id}/terminal/sessions/{session_id}`
  - `WS /workspaces/{workspace_id}/terminal/sessions/{session_id}`
- Spawn or attach to tmux session in workspace directory.
- Bridge PTY input/output over WebSocket.
- Enforce workspace authorization.
- Log terminal session start and stop events.

## Frontend Tasks

- Install and configure xterm.js.
- Build terminal panel in center tabs.
- Add connect/disconnect state.
- Add resize support.
- Add copy/paste support.
- Preserve terminal panel state on refresh.

## Database Tasks

- Add migration for `terminal_sessions`.
- Add index on `workspace_id`.

## Acceptance Criteria

- User can open terminal from dashboard.
- Commands run inside the workspace directory.
- Terminal output streams with low latency.
- Refreshing the browser reconnects to the existing tmux session.
- User cannot access terminal for a workspace they do not own.

## Future Enhancements

- Multiple terminal tabs.
- Terminal command history UI.
- File upload/download from terminal.
- Resource usage display.

