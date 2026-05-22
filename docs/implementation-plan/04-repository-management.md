# Epic 3 - Repository Management

## Goal

Connect workspaces to Git repositories so the AI agent and developer can read, modify, review, commit, and push code.

## User Stories

- As a developer, I can connect a GitHub repository.
- As a developer, I can clone the repository into the workspace.
- As a developer, I can view code changes as a diff.
- As a developer, I can commit and push changes from the dashboard.

## MVP Scope

- GitHub HTTPS repository support.
- Personal access token authentication.
- Clone, status, diff, commit, push.
- Branch display.

## Backend Tasks

- Create `repository_credentials` table:
  - `id`
  - `workspace_id`
  - `provider`
  - `auth_type`
  - `encrypted_token`
  - `created_at`
  - `updated_at`
- Create repository endpoints:
  - `POST /workspaces/{workspace_id}/repo/connect`
  - `POST /workspaces/{workspace_id}/repo/clone`
  - `GET /workspaces/{workspace_id}/repo/status`
  - `GET /workspaces/{workspace_id}/repo/diff`
  - `POST /workspaces/{workspace_id}/repo/pull`
  - `POST /workspaces/{workspace_id}/repo/commit`
  - `POST /workspaces/{workspace_id}/repo/push`
- Implement Git service using a reliable Git library or subprocess wrapper.
- Sanitize repository URLs.
- Redact credentials from logs.
- Store commit operation in audit log.

## Frontend Tasks

- Build repository connection form.
- Build branch/status indicator.
- Build changed files list.
- Build diff viewer page using a code diff library.
- Build commit modal with message input.
- Add push button with success/error feedback.

## Database Tasks

- Add migration for `repository_credentials`.
- Add audit log relation when audit epic exists.

## Acceptance Criteria

- User can connect a GitHub repo with a token.
- Repo is cloned into the workspace directory.
- Dashboard shows changed files after edits.
- User can commit changes with a custom message.
- User can push committed changes.
- Tokens are encrypted and never returned to frontend.

## Future Enhancements

- GitLab support.
- Bitbucket support.
- SSH key support.
- Branch create/switch/delete.
- Pull request creation.
- Accept/reject diff hunks.

