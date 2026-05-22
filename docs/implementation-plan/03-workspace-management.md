# Epic 2 - Workspace Management

## Goal

Allow users to create and manage isolated project workspaces.

## User Stories

- As a developer, I can create a workspace for a project.
- As a developer, I can view workspace status.
- As a developer, I can configure environment variables for a workspace.
- As a developer, I can clone a workspace later for testing.

## MVP Scope

- Single workspace per user.
- One repository per workspace.
- Store metadata and basic status.
- Environment variables UI can be basic.

## Backend Tasks

- Create `workspaces` table:
  - `id`
  - `owner_id`
  - `name`
  - `slug`
  - `repo_url`
  - `default_branch`
  - `local_path`
  - `status`
  - `created_at`
  - `updated_at`
- Create `workspace_env_vars` table:
  - `id`
  - `workspace_id`
  - `key`
  - `encrypted_value`
  - `created_at`
  - `updated_at`
- Create endpoints:
  - `POST /workspaces`
  - `GET /workspaces`
  - `GET /workspaces/{workspace_id}`
  - `PATCH /workspaces/{workspace_id}`
  - `DELETE /workspaces/{workspace_id}`
  - `GET /workspaces/{workspace_id}/status`
- Implement workspace directory creation on server.
- Add ownership checks for every workspace endpoint.
- Add workspace status enum: `creating`, `ready`, `busy`, `error`, `stopped`.

## Frontend Tasks

- Build workspace creation screen.
- Build workspace switcher placeholder, even if MVP only supports one workspace.
- Build workspace settings page.
- Display workspace status in top bar and bottom status bar.
- Add environment variable management UI with masked values.

## Database Tasks

- Add migrations for `workspaces` and `workspace_env_vars`.
- Add indexes for `owner_id`, `slug`, and `status`.

## Acceptance Criteria

- Authenticated user can create one workspace.
- Workspace appears in dashboard after creation.
- Workspace has a server-side directory.
- User cannot access another user's workspace.
- Environment variables are encrypted before storage.

## Future Enhancements

- Multiple workspaces per user.
- Workspace clone.
- Workspace hibernation.
- Resource quota per workspace.

