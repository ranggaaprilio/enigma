# Epic 06 — Rollout & Migration (Keep Daytona)

## Goal

Turn the hybrid model on **per workspace, behind a flag**, validate feature and cost parity
against the current single-persistent-sandbox behavior, migrate existing workspaces, and make
hybrid the default — with a clean **rollback** at every step. **Daytona stays**; nothing is
removed. This is the last epic and only runs once 01–05 are proven.

## Why

Flipping every workspace at once is risky. The tiering flag from Epic 01 lets the old and new
lifecycles coexist, so we can roll out gradually (canary → cohort → default) and revert
instantly if something regresses.

## Current State

- `hybrid_enabled` flag (global default + optional per-workspace) introduced in Epic 01;
  when off, the router returns the session sandbox for everything (today's behavior).
- Existing workspaces each have one persistent Daytona sandbox
  (`workspace.daytona_sandbox_id`) shared by agent + human work.

## Scope

- Per-workspace rollout controls + rollback.
- Migration of existing workspaces to the hybrid lifecycle.
- Parity + cost validation gates before default-on.
- Documentation updates. **No Daytona decommission** (we keep it).

## Backend / Ops Tasks

- **Rollout controls**:
  - Global default + per-workspace `hybrid_enabled`; a rollout percentage / cohort selector
    (canary → team cohort → 100%).
  - An admin action to toggle a single workspace, and a **rollback** (flag off) that restores
    the shared-persistent behavior with no data migration required.
- **Migration of existing workspaces** (mostly a behavior switch, not data movement):
  - Ensure all branches/worktrees are pushed to git (the source of truth — Epic 04); a
    dry-run reports any unpushed work before enabling hybrid.
  - Enable `hybrid_enabled`: subsequent agent runs use ephemeral execution sandboxes; the
    existing persistent sandbox becomes the session (human) sandbox with aggressive auto-stop
    (Epic 03/05).
  - No sandbox needs to be recreated — the current one simply becomes the session-tier
    sandbox.
- **Parity validation** (gate before default-on):
  - Contract/router tests (Epic 01) + feature parity checks: agent run/tests, terminal,
    editor, dev-server preview, worktrees, egress — hybrid on vs off.
  - Cost comparison on the canary cohort: sandbox-hours before vs after (expect a large drop
    for idle-heavy workspaces).
- **Docs**:
  - Update `docs/improvement-epics/00-index.md` "Sandbox" note and the README to describe the
    two-tier Daytona lifecycle.
  - Operator runbook: how to read tier metrics, tune auto-stop, and roll a workspace back.

## Database Tasks

- Backfill `hybrid_enabled` for the rollout cohort; `last_active_at` populated on activity.
- No destructive migration — `daytona_sandbox_id` remains (now documented as the session
  sandbox); `agent_runs.execution_sandbox_id` populated going forward.

## Security & Operational Requirements

- No workspace is switched to hybrid until its work is confirmed pushed (dry-run gate).
- Rollback (flag off) must always be available and require no data migration.
- Reaper + quotas (Epic 05) active before broad rollout so a bad batch can't leak cost.

## Acceptance Criteria

- A workspace can be switched to hybrid and rolled back via the flag with no data loss and no
  recreation of sandboxes.
- Parity suite green with hybrid on; canary cost report shows the expected idle-cost
  reduction and is accepted.
- Default-on for new workspaces after the canary/cohort gates pass.
- Daytona remains fully in place: SDK, config, snapshot build, and preview/egress features
  all unchanged.

## Dependencies

- Depends on **01–05** shipped and proven. Intentionally last.

## Future Enhancements

- Per-plan defaults (e.g. free tier = aggressive stop, paid = warm pool).
- Automated cohort rollout with a progress + cost-delta dashboard.
- Keep the tiering seam so a future execution backend (e.g. a different sandbox for heavy
  jobs) could slot in without another migration of this size.
