# Epic 08 - Real-Time Collaboration

## Goal

Let teammates work in the same workspace together: shared agent sessions, presence, and
review threads. Teams already exist as a concept, but workspaces are single-owner today.

## User Value

A team can watch an agent run together, hand off, and discuss spec/plan/PR review in context
— turning Enigma from a single-player tool into a team workspace.

## Current State

- Hierarchy is Projects → Teams → Workspaces, but `Workspace.owner_id` is a single owner;
  team membership grants access (see `dashboard.py` access logic) yet there is no real-time
  multi-user presence or shared session.
- Streams (agent/terminal/notifications) are per-connection; no presence layer.

## Scope

- Presence (who's viewing a workspace / session).
- Shared/observable agent sessions (multiple viewers on one run stream).
- Comment threads on agent runs and SDD artifacts (Epic 01).

## Backend Tasks

- Presence service over Redis pub/sub: track viewers per workspace/session, broadcast
  join/leave.
- Allow multiple subscribers on an agent run stream (fan-out), with clear "who can drive vs.
  observe" rules tied to role.
- `comments` table (`target_type` [run|spec_phase|pr], `target_id`, `user_id`, `body`,
  timestamps); endpoints to add/list/resolve.
- Authorization via existing team/project roles.

## Frontend Tasks

- Presence avatars on the workspace and session.
- Live shared view of an in-progress run for observers.
- Comment threads on runs and SDD artifacts; notifications on new comments.

## Database Tasks

- Migration for `comments`; presence is ephemeral (Redis), not persisted.
- Indexes on `target_type` + `target_id`.

## Acceptance Criteria

- Two users viewing the same workspace see each other's presence.
- An observer sees a run stream live without driving it.
- Comments persist on runs/artifacts and notify relevant users.

## Dependencies

- Depends on **Epic 07** WebSocket reconnect/resilience. Comments integrate with **Epic 01**
  (artifact review) and **Epic 02** (PR review).

## Future Enhancements

- Collaborative cursors in the editor.
- Live "request review" hand-off from agent to a specific teammate.
- @mentions and threaded discussions.
