# Epic 05 — Lifecycle, Cost & Cold-Start Tuning

## Goal

Tune the two tiers for cost and speed using **Daytona's own lifecycle knobs**: aggressive
auto-stop for the session tier, guaranteed cleanup of ephemeral execution sandboxes, fast
cold starts via the baked snapshot, per-tenant quotas, and observability of sandbox
count/duration/cost. Because Daytona provides the control plane, this epic is mostly
configuration, a cleanup reaper, and a snapshot strategy — not new infrastructure.

## Why

The hybrid model's cost win only materializes if idle sandboxes actually stop and ephemeral
sandboxes actually get deleted. Its UX only works if per-run cold starts stay short. Daytona
gives us `auto_stop_interval`, `auto_delete_interval`, and custom snapshots — this epic sets
them correctly and adds the safety nets Daytona doesn't automate for us.

## Current State

- `provision` (`daytona_runtime.py:118`) sets `auto_stop_interval = daytona_auto_stop_minutes`
  (**30**), `auto_archive_interval = 0`, `auto_delete_interval = -1` (never) for the single
  persistent sandbox.
- No per-run ephemeral cleanup, no concurrency caps, no sandbox-level cost/latency metrics.
- Snapshot `daytona/Dockerfile` + `build-snapshot.sh` bake mise/code-server/specify/uv,
  `--disk 10`, restricted egress — a good base for fast starts, but project deps are not
  pre-installed.

## Scope

- Independent lifecycle knobs per tier (session auto-stop vs execution auto-delete).
- A **reaper** for orphaned ephemeral sandboxes (defense in depth beyond `ephemeral`/
  `auto_delete`).
- Cold-start mitigation via snapshot/deps strategy + optional warm cache.
- Quotas: max concurrent execution sandboxes per workspace/team; disk awareness
  (`--disk 10`).
- Observability + cost attribution per workspace/tier.

## Backend / Ops Tasks

- **Lifecycle knobs**:
  - Session tier: `daytona_session_auto_stop_minutes` (e.g. 10) — aggressive idle stop.
  - Execution tier: `ephemeral=True` and/or a short `auto_delete_interval` so a crashed
    backend still can't leak a run sandbox forever.
  - Keep `auto_archive_interval` sensible for long-idle session sandboxes.
- **Reaper** (scheduled task in-app, or a periodic job):
  - `client.list(ListSandboxesQuery(labels=...))` for `role=EXECUTION` sandboxes whose owning
    agent-run is terminal or older than a max lifetime → `delete`. Uses the `run-id`/`role`
    labels from Epic 01.
  - Alert if orphans are found in steady state (indicates a teardown bug).
- **Cold-start mitigation**:
  - Extend the snapshot build to pre-install common toolchains/deps so ephemeral hydrate is
    fetch-only. Document a per-project overlay for project-specific deps.
  - Evaluate a Daytona **snapshot of the post-install state** to skip dependency install per
    run (ties to Epic 04 hydration modes: cold vs warm).
  - Measure `provision_ms` + `hydrate_ms`; set and track a cold-start target.
- **Quotas & limits**:
  - Cap concurrent execution sandboxes per workspace/team; queue or reject with a clear error
    (protects Daytona spend and account limits).
  - Surface disk pressure relative to the `--disk 10` provisioning.
- **Observability & cost**:
  - Metrics: active sandboxes by tier, run duration, provision/hydrate/push ms, auto-stop and
    reap counts, per-workspace sandbox-hours.
  - Structured logs (`structlog`) correlated by workspace/run id; alarms on leaks/oversub.
  - Attribute Daytona cost per workspace/tier using labels for reporting.

## Database Tasks

- Workspace: `last_active_at`, optional per-plan `session_auto_stop_minutes` override.
- Reuse/extend audit or a small `runtime_events` table: sandbox create/stop/delete/reap with
  reason + timestamps (feeds dashboards + debugging).

## Security & Operational Requirements

- Reaper acts only on sandboxes it owns by label; never touches unrelated Daytona resources.
- Fail safe: session auto-stop is always safe (Daytona persists state); ephemeral delete is
  gated by the Epic 04 WIP-commit policy so no uncommitted agent work is lost.
- Quotas enforced server-side; a burst of runs cannot exhaust the Daytona account.

## Acceptance Criteria

- A session sandbox with no human activity **and no agent run** auto-stops within the
  configured window; reopening resumes from stopped with state intact.
- Every ephemeral sandbox is deleted at run end; the reaper finds zero orphans in steady
  state (leak test), and cleans any it does find.
- Warm-path hydration (baked/cached deps) is measurably faster than cold clone+install
  (target documented and met).
- Exceeding the concurrency quota returns a clear, handled error — not account exhaustion.
- Per-workspace sandbox-hours/cost are visible; alarms fire on leaks or oversubscription.

## Dependencies

- Depends on **02** (execution sandboxes to reap) and **03** (session auto-stop). Uses the
  snapshot from `daytona/`. Feeds **06** rollout with the cost baseline.

## Future Enhancements

- Warm pool of pre-hydrated execution sandboxes for instant agent starts (opt-in, cost
  trade-off).
- Budget guardrails with hard per-team caps.
- Predictive pre-warming from usage patterns.
