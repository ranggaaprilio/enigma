# Epic 4 - AI Agent Interaction

## Goal

Provide a dashboard prompt interface that can start an AI coding agent, stream output, show tool activity, and save prompt history.

## User Stories

- As a developer, I can send a prompt to an AI agent from the dashboard.
- As a developer, I can see streaming output in real time.
- As a developer, I can see files and commands used by the agent.
- As a developer, I can stop an active agent run.
- As a developer, I can view previous prompts and rerun them.

## MVP Scope

- One AI provider.
- One active run per workspace.
- Streaming text output.
- Basic tool event visualization.
- Stop active run.
- Prompt history list.

## Backend Tasks

- Create `agent_runs` table:
  - `id`
  - `workspace_id`
  - `user_id`
  - `prompt`
  - `status`
  - `provider`
  - `model`
  - `tokens_used`
  - `started_at`
  - `completed_at`
  - `error_message`
- Create `agent_events` table:
  - `id`
  - `agent_run_id`
  - `event_type`
  - `content`
  - `metadata`
  - `created_at`
- Create endpoints:
  - `POST /workspaces/{workspace_id}/agent/runs`
  - `GET /workspaces/{workspace_id}/agent/runs`
  - `GET /workspaces/{workspace_id}/agent/runs/{run_id}`
  - `POST /workspaces/{workspace_id}/agent/runs/{run_id}/stop`
  - `WS /workspaces/{workspace_id}/agent/runs/{run_id}/stream`
- Implement AI provider abstraction:
  - `generate_response`
  - `stream_response`
  - `tool_call`
- Implement workspace tool layer:
  - read file
  - write file
  - list files
  - run command with restrictions
- Add dangerous command detection for MVP.
- Save every streamed event to database.

## Frontend Tasks

- Build right-side AI prompt panel.
- Add markdown-capable prompt textarea.
- Add run button and stop button.
- Build streaming output panel.
- Build collapsible tool call cards.
- Build run status indicator: `idle`, `thinking`, `executing`, `completed`, `error`, `stopped`.
- Build prompt history sidebar section.
- Add rerun prompt action.

## Database Tasks

- Add migrations for `agent_runs` and `agent_events`.
- Add indexes for `workspace_id`, `user_id`, `status`, and `created_at`.

## Acceptance Criteria

- User can submit a prompt for a workspace.
- Backend creates an agent run and streams output to frontend.
- Frontend updates output without page refresh.
- Agent events persist and can be reopened later.
- User can stop a running agent.
- Only authenticated workspace members can access run streams.

## Future Enhancements

- Multiple AI providers.
- Confirmation mode before dangerous tool calls.
- File attachments.
- `@file` mentions in prompt.
- Tag and search prompt history.
- Token budget limits.

