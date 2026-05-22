# Epic 7 - Preview System

## Goal

Automatically expose running development servers from a workspace through preview URLs.

## User Stories

- As a developer, I can see a preview URL when my app server starts.
- As a developer, I can open the preview in the dashboard.
- As a developer, I can share preview URL for demo or review.

## MVP Scope

- Detect one common port.
- Display preview iframe.
- Local reverse proxy for development.
- Store preview session metadata.

## Backend Tasks

- Create `preview_sessions` table:
  - `id`
  - `workspace_id`
  - `port`
  - `public_url`
  - `status`
  - `created_at`
  - `last_seen_at`
- Create endpoints:
  - `GET /workspaces/{workspace_id}/preview/sessions`
  - `POST /workspaces/{workspace_id}/preview/detect`
  - `POST /workspaces/{workspace_id}/preview/sessions/{session_id}/refresh`
  - `POST /workspaces/{workspace_id}/preview/sessions/{session_id}/stop`
- Detect common ports: `3000`, `5173`, `8000`, `8080`.
- Add reverse proxy route from dashboard backend to workspace port.
- Add WebSocket support for hot reload traffic.
- Track preview status.

## Frontend Tasks

- Add Preview tab in center panel.
- Build iframe preview frame.
- Add device mode toggle: desktop, tablet, mobile.
- Add refresh button.
- Add copy/share URL button.
- Show detected port and status in bottom status bar.

## Database Tasks

- Add migration for `preview_sessions`.
- Add indexes for `workspace_id` and `status`.

## Acceptance Criteria

- Backend detects a running app on a common port.
- Frontend displays preview inside iframe.
- Preview can be refreshed manually.
- Preview route is scoped to authorized workspace users.
- Hot reload works for common Vite/React dev server traffic.

## Future Enhancements

- Public subdomain per preview.
- HTTPS using Caddy or Traefik.
- QR code.
- Multiple preview ports.
- Custom domain support.
- Preview logs viewer.

