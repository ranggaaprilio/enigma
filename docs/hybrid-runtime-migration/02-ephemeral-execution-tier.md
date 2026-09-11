# Epic 02 — Tier 1: Ephemeral Execution Sandboxes

## Goal

Route **agent execution** (commands, unit tests, builds, lint, patch-then-verify) to a
**throwaway Daytona sandbox created per agent-run**: spin up → hydrate from git → run →
commit/push → delete. Agent work no longer keeps the workspace's persistent sandbox alive.
This is the headline win of the hybrid model.

## Why

The AI needs an isolated environment only transiently. A per-run ephemeral sandbox gives:

- **Isolation** — clean environment every run; no state bleed between runs.
- **Parallelism** — multiple runs (different worktrees/branches) run in separate sandboxes
  with no contention.
- **Cost** — a sandbox exists only while a run is active, not for the workspace lifetime.

State that must survive between runs lives in **git** (Epic 04), not in the sandbox.

## Design: one sandbox per agent-run (not per tool-call)

The agent loop is iterative (read → edit → test → read → edit → test). The ephemeral sandbox
is held for the **whole run**, so edits/deps persist across tool calls within the run; it is
deleted only at run end.

```
agent run starts
  → provision ephemeral Daytona sandbox (role=EXECUTION)   [Epic 01 router]
  → hydrate: fetch branch + checkout worktree              [Epic 04]
  → (deps ready via baked snapshot / warm cache)           [Epic 05]
  → loop: read/write/patch/search/run_command/run_tests
  → commit + push branch                                    [Epic 04]
  → delete sandbox
```

Per-tool-call provisioning is rejected (it would re-pay hydration every call).

## Current State

- Agent tools (`agent_tools.py`: `list_files/read_file/write_file/apply_patch/search_code/
  run_command/git_status/git_diff`) and the OpenHands bridge (`openhands_tools.py`) call
  `DaytonaRuntime.exec` / `get_sandbox` / `sandbox.fs.*` against the **workspace's persistent
  sandbox**.
- `stream_command` (`daytona_runtime.py:208`) provides session-based streaming for long runs.
- `provision` sets `ephemeral=False`, `auto_delete_interval=-1` — the persistent defaults;
  the execution tier flips these.
- Command safety (denylist/allowlist, shell-operator block, path confinement, truncation)
  lives in `agent_tools.py` and must be preserved unchanged.

## Scope

- Execution sandboxes created via the router (Epic 01) with role `EXECUTION`.
- Lifecycle bound to an **agent-run/session**, not the workspace.
- Reuse the existing `exec` / `stream_command` / `fs.*` surface (no new transport needed —
  Daytona already provides them).
- Git hydrate on start + commit/push on finish (contract in Epic 04).
- Guaranteed teardown (delete) on success, failure, and timeout.
- Preserve the command-safety model verbatim.

## Backend Tasks

- Provision the execution sandbox in the agent-run entrypoint (`api/agents.py` /
  `speckit.py` implement phase):
  - `provision(role=EXECUTION)` with `ephemeral=True` (or a short
    `auto_delete_interval`), name `run-<ws>-<run_id>`, labels incl. `run-id`.
  - Persist `agent_runs.execution_sandbox_id` (Epic 01 schema).
- Point `agent_tools.py` / `openhands_tools.py` at the run's execution sandbox (via the
  router `intent=agent_run`) instead of the workspace sandbox. The tools' logic and safety
  layer are unchanged — only the resolved sandbox differs.
- Hydrate before the first tool call and push after the last (call the Epic 04 sync service).
- Teardown: `finally`-delete the execution sandbox at run end; rely on
  `ephemeral`/`auto_delete_interval` as a backstop; a reaper (Epic 05) sweeps stragglers by
  the `run-id` label.
- Concurrency: parallel runs → parallel execution sandboxes; enforce per-workspace/team caps
  (Epic 05).

## Frontend Tasks

- Agent-run UI shows an "environment: preparing → running → cleaning up" state to explain the
  per-run cold start (a few extra seconds vs the old warm persistent sandbox).
- Nothing else changes — output/streaming contract is preserved.

## Database Tasks

- `agent_runs`: `execution_sandbox_id` (from Epic 01), `hydrated_sha`, `pushed_sha`,
  `provision_ms` / `teardown_ms` for observability.

## Security & Operational Requirements

- **Preserve command safety** from `agent_tools.py` exactly (denylist/allowlist, shell-op
  blocking, worktree-root path confinement, output truncation).
- Egress policy applied to execution sandboxes (same `_apply_network_settings`).
- PAT injected at hydrate/push time only; never baked in; redacted from output.
- Hard run timeout + forced delete → no orphaned execution sandboxes accruing cost.

## Acceptance Criteria

- An agent run (hybrid on) provisions an ephemeral sandbox, edits + runs unit tests, streams
  output identical in shape to today, commits + pushes, and the sandbox is **deleted** —
  with the workspace's persistent sandbox never started for the run.
- Two runs on different branches execute in parallel in separate sandboxes.
- All command-safety tests pass unchanged.
- No execution sandbox survives its run (verified by a leak test against the `run-id` label).
- With hybrid off, agent runs still use the persistent sandbox (unchanged).

## Dependencies

- Depends on **01** (tiering/router) and **04** (git hydrate/push). Pairs with **05**
  (cold-start snapshot, reaper, quotas).

## Future Enhancements

- Snapshot the post-`install` filesystem so hydration skips dependency install per run.
- VFS-backed editing (agent reads/writes in app state) with the ephemeral sandbox used only
  for `execute` — further cuts cold starts.
- Reuse a still-warm execution sandbox for a follow-up run on the same branch within a short
  window (opt-in), trading a little isolation for speed.
