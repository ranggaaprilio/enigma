# Epic 8 - Custom Commands

## Goal

Allow users to define reusable workspace commands and run them from the dashboard.

## User Stories

- As a tech lead, I can define commands like run tests, lint, build, and deploy staging.
- As a developer, I can run a command with one click.
- As a developer, I can see command execution logs.
- As a developer, I can provide parameters for dynamic commands.

## MVP Scope

- Define commands in UI.
- Run commands in workspace directory.
- Save execution logs.
- Confirmation flag for dangerous commands.

## Backend Tasks

- Create `custom_commands` table:
  - `id`
  - `workspace_id`
  - `name`
  - `command`
  - `description`
  - `icon`
  - `hotkey`
  - `requires_confirmation`
  - `created_at`
  - `updated_at`
- Create `command_runs` table:
  - `id`
  - `custom_command_id`
  - `workspace_id`
  - `user_id`
  - `status`
  - `exit_code`
  - `output`
  - `started_at`
  - `completed_at`
- Create endpoints:
  - `POST /workspaces/{workspace_id}/commands`
  - `GET /workspaces/{workspace_id}/commands`
  - `PATCH /workspaces/{workspace_id}/commands/{command_id}`
  - `DELETE /workspaces/{workspace_id}/commands/{command_id}`
  - `POST /workspaces/{workspace_id}/commands/{command_id}/run`
  - `GET /workspaces/{workspace_id}/commands/{command_id}/runs`
  - `WS /workspaces/{workspace_id}/command-runs/{run_id}/stream`
- Execute commands through the same restricted command service used by the AI agent.
- Store command output.
- Log run events in audit log.

## Frontend Tasks

- Add custom commands section in sidebar.
- Build command create/edit modal.
- Add confirmation modal for risky commands.
- Build command run output panel.
- Add command status indicators.
- Support optional hotkey display.

## Database Tasks

- Add migrations for `custom_commands` and `command_runs`.
- Add indexes for `workspace_id`, `status`, and `created_at`.

## Acceptance Criteria

- User can create, edit, and delete a command.
- User can run a command from sidebar.
- Command output streams to frontend.
- Completed command run is saved with output and exit code.
- Commands requiring confirmation cannot run accidentally.

## Future Enhancements

- YAML import from `.dashboard/commands.yaml`.
- Dynamic parameters.
- Command templates marketplace.
- Scheduled commands.

