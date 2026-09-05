# Epic 06 - Cost & Usage Analytics

## Goal

Surface token usage and cost across runs, workspaces, teams, and projects, with optional
budgets and limits. For an AI platform this is essential for operations and chargeback.

## User Value

Owners and team leads can see what the agent costs, attribute spend, and set guardrails
before a runaway run burns budget.

## Current State

- `agent_runs.tokens_used` **exists but is never surfaced** anywhere in the API or UI.
- Providers don't all report usage consistently yet (addressed alongside Epic 04).
- `services/dashboard.py` already aggregates project/team/workspace counts — a natural home
  for cost rollups.

## Scope

- Capture input/output tokens (and derived cost) per run.
- Aggregate by workspace → team → project and by time window.
- Optional budgets/limits with warnings and (optionally) enforcement.

## Backend Tasks

- Ensure every provider populates `tokens_used` (and ideally split input/output). Add
  `input_tokens` / `output_tokens` columns if useful.
- A pricing map (per provider/model) in config to derive cost; store `estimated_cost` per run
  or compute on read.
- Aggregation endpoints (extend `dashboard.py`):
  - `GET /dashboard/usage?scope=project|team|workspace&id=...&from=...&to=...`
  - per-period totals, top workspaces/users by spend, run counts.
- Optional budgets: `usage_budgets` table (scope, limit, period); a check before starting a
  run; warn at threshold, optionally block over-limit (config-driven).
- Audit budget changes.

## Frontend Tasks

- Usage panel on the dashboard overview: tokens + cost over time, breakdown by
  project/team/workspace, top spenders.
- Per-workspace usage on the workspace overview.
- Budget configuration UI (for admins/leads) with threshold warnings.

## Database Tasks

- Migration: optional `input_tokens`/`output_tokens`/`estimated_cost` on `agent_runs`;
  `usage_budgets` table. Index by scope + period.
- Consider a retention/rollup strategy for high-volume event data (see Epic 07).

## Acceptance Criteria

- Each completed run shows tokens and estimated cost.
- The dashboard shows usage rollups by project/team/workspace and time window.
- A budget can warn (and optionally block) when exceeded.

## Dependencies

- Best after **Epic 04** (consistent usage reporting). Pairs with **Epic 05** (per-session
  cost).

## Future Enhancements

- Export (CSV) and scheduled cost reports.
- Per-user quotas.
- Cost-aware model routing (link to Epic 04 future work).
