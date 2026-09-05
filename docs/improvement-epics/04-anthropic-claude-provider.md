# Epic 04 - Anthropic / Claude Agent Provider

## Goal

Add Anthropic Claude as a first-class agent provider alongside OpenAI and OpenHands, and let
each workspace choose its provider + model. Claude is strong at coding and tool use, which
directly improves the SDD spec/plan/implement phases.

## User Value

Higher-quality agent output and the freedom to pick the right model per workspace/run.

## Current State

- `services/agent_providers.py` implements only `OpenAIResponsesProvider`
  (`provider_name = "openai"`); `openhands_provider.py` adds OpenHands.
- `config.py` has `agent_provider = "auto"`, `openai_*`, and `openhands_*` settings — **no
  Anthropic settings**.
- `agent_runs` already stores `provider`, `model`, and `tokens_used` per run.

## Scope

- A streaming `AnthropicProvider` using the Messages API with tool use.
- Provider/model selection per workspace (and optionally per run).
- Consistent token-usage reporting into `agent_runs.tokens_used` (feeds Epic 06).

## Backend Tasks

- Add `AnthropicProvider` (`provider_name = "anthropic"`) implementing the same
  `AgentProvider` interface (`stream_response`, tool-call handling) as the OpenAI provider.
  - Use the Anthropic Messages API with streaming and `tool_use` / `tool_result` blocks,
    mapped to the existing `WorkspaceTools`.
  - Map provider events to the existing `ProviderEvent` types (`output_text`, `tool_call`,
    `tool_result`, `error`, `status`).
  - Report input/output token usage back into the run.
- Config (`config.py`): `anthropic_api_key`, `anthropic_model`
  (default a current Claude model, e.g. a Sonnet 4.x for cost/latency, Opus for hard tasks),
  `anthropic_base_url` (optional). Document in `.env.example`.
- Extend provider selection (`agent_provider`) so `"anthropic"` is valid and `"auto"` can
  pick it when the key is set.
- Add a per-workspace default provider/model (column on `workspaces` or a settings table)
  and allow per-run override in the run-create payload.

> When implementing, confirm the current Claude model IDs and Messages API surface via the
> Anthropic docs / Context7 rather than relying on memory.

## Frontend Tasks

- Provider + model selector in the workspace settings and/or the agent prompt composer.
- Show the provider/model used on each run in the run history.

## Database Tasks

- Migration: per-workspace default provider/model (and/or allow `provider`/`model` override
  on run create — `agent_runs` already records the chosen values).

## Security Requirements

- Store the Anthropic key like other secrets (env / encrypted settings); never expose it
  client-side; redact from logs.

## Acceptance Criteria

- A workspace can be set to Claude and a run streams output + tool calls correctly.
- `agent_runs.provider`/`model`/`tokens_used` are populated for Claude runs.
- Switching providers requires no code change — only config + selection.

## Dependencies

- Independent. Strongly improves **Epic 01** and is a prerequisite for accurate **Epic 06**
  cost tracking.

## Future Enhancements

- Prompt caching for long system prompts / repo context.
- Per-team model policy (allowed models, default model).
- Automatic model routing (cheap model for cheap phases, strong model for `plan`/`implement`).
