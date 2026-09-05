# Epic 05 - Multi-Turn Agent Sessions

## Goal

Let users have a conversation with the agent — follow-up messages that retain context —
instead of every run being a single, isolated prompt.

## User Value

"Now also handle the error case" or "make the button blue instead" should continue the prior
context, not start from scratch.

## Current State

- `models/agent.py`: `AgentRun` has a single `prompt` per run; there is **no** conversation,
  `parent_run`, or `session` linkage. `agent_events` capture the run's stream.
- Each run starts cold with no memory of prior runs.

## Scope

- A session/conversation concept grouping multiple runs (turns) with shared context.
- Context assembly: prior turns' messages + tool results fed into the next turn.
- UI for a continuous chat thread per workspace.

## Backend Tasks

- Add `agent_sessions` (`id`, `workspace_id`, `user_id`, `title`, `status`, timestamps) and
  link `agent_runs.session_id` (nullable FK; a run without a session = one-shot, preserving
  current behavior).
- Build conversation context for each turn from prior runs' messages/tool results, with a
  token-budget-aware truncation/summarization strategy.
- Endpoints: create session, list sessions, append a turn (`POST .../sessions/{id}/turns`),
  get session with ordered turns. Reuse the existing run stream per turn.
- Optional: server-side summarization of long histories to stay within context limits.

## Frontend Tasks

- Convert the agent panel into a threaded chat (turns with user prompt + agent response +
  tool cards), keeping run-level detail available.
- Session list / "New session" control; rename + delete session.
- "Continue" affordance that appends a turn rather than starting a fresh run.

## Database Tasks

- Migration for `agent_sessions` + `agent_runs.session_id` (nullable, indexed).

## Acceptance Criteria

- A follow-up turn demonstrably uses prior context (e.g. references a file edited earlier
  without re-stating it).
- One-shot runs still work (session optional).
- Sessions persist and reopen with full history.

## Dependencies

- Independent; better with **Epic 04** (larger context windows) and ties naturally to
  **Epic 06** (per-session cost).

## Future Enhancements

- Branching conversations / forking a session.
- Shared sessions (ties into Epic 08 collaboration).
- Automatic session titling from the first prompt.
