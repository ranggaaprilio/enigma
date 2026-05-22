# Epic 10 - Notifications And Audit

## Goal

Track important actions and notify users when background operations complete or fail.

## User Stories

- As a developer, I receive in-app notifications when agent runs finish.
- As a developer, I receive notifications when builds or commands fail.
- As an owner, I can review important workspace actions in an audit log.

## MVP Scope

- In-app notifications only.
- Audit log backend table and simple UI.
- Events for auth, repository, agent, command, terminal, file, and preview actions.

## Backend Tasks

- Create `notifications` table:
  - `id`
  - `user_id`
  - `workspace_id`
  - `type`
  - `title`
  - `message`
  - `read_at`
  - `created_at`
- Create `audit_logs` table:
  - `id`
  - `user_id`
  - `workspace_id`
  - `action`
  - `target_type`
  - `target_id`
  - `metadata`
  - `created_at`
- Create endpoints:
  - `GET /notifications`
  - `PATCH /notifications/{notification_id}/read`
  - `POST /notifications/read-all`
  - `GET /workspaces/{workspace_id}/audit-logs`
- Add WebSocket or SSE channel for real-time in-app notifications.
- Add service helper for creating audit log entries.
- Add service helper for creating notifications from background events.

## Frontend Tasks

- Build notification bell in top bar.
- Build notification dropdown.
- Add unread count.
- Add audit log page in workspace settings.
- Show audit logs in reverse chronological order.

## Database Tasks

- Add migrations for `notifications` and `audit_logs`.
- Add indexes for `user_id`, `workspace_id`, `action`, and `created_at`.

## Acceptance Criteria

- User receives in-app notification when agent run completes or fails.
- User receives in-app notification when custom command completes or fails.
- Audit log records commits, pushes, deletes, command runs, and agent runs.
- User can mark notifications as read.
- Audit log is scoped to workspace authorization.

## Future Enhancements

- Email notifications.
- Slack webhook.
- Discord webhook.
- Push notifications.
- Audit export.

