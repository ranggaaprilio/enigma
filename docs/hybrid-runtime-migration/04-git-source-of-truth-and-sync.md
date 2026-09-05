# Epic 04 — Git as Source of Truth & Tier Sync

## Goal

Make **git the durable state that connects the two tiers**. Define and implement the
**hydration** (git → sandbox) and **persistence** (sandbox → git) contract so an ephemeral
execution sandbox can reconstruct the right branch on start and publish its work on finish,
and so the persistent human sandbox reflects that work when a person opens it. This is the
correctness backbone of the hybrid model.

## Why

In the old model, state lived in one sandbox forever. Now the execution sandbox is thrown
away after each run and the human sandbox can stop/resume — so the only thing carrying state
between them and across time is the git repo. Getting this contract right is what lets us
drop always-on persistence without losing work.

## The Sync Contract

- **Source of truth**: the workspace's GitHub repo (branches + worktree branches).
- **Sync granularity**: the **commit**. Tiers exchange state by push/pull of branches, not a
  shared live filesystem.
- **Non-goal**: simultaneous human+AI edits to the same *uncommitted* tree (that requires the
  old always-persistent model). Concurrent pushes to one branch → conflict surfaced, not
  real-time co-edit.

```
Tier 1 (ephemeral run)                 Tier 2 (human session)
  hydrate: fetch <branch> ──┐        ┌── on open: fetch/pull <branch>
  edit + test               │  GIT   │   edit in IDE / terminal
  commit + push <branch> ───┴─►(SoT)─┴─► commit + push <branch>
```

## Current State

- `repositories.py`: GitHub clone/status/diff/pull/commit/push against
  `workspace.workspace_path`, per-user commit identity, PAT encrypted via
  `WorkspaceSecretCipher`.
- `worktrees.py`: `worktrees/<slug>` dirs + branch lifecycle via `runtime.exec` raw
  `git worktree`; each worktree configured with `push.autoSetupRemote` + `push.default`.
- All git ops assume one long-lived sandbox with a persistent clone.

## Scope

- A tier-agnostic **hydration service** (git → a freshly started sandbox of either tier).
- A **persistence service** (commit + push at run/session boundaries).
- Worktree reconstruction on hydrate for the target branch.
- Uncommitted-work policy at ephemeral teardown (don't lose in-flight agent work).
- Divergence/conflict handling when a branch moved remotely between hydrate and push.

## Backend Tasks

- `services/git_sync.py` (uses `DaytonaRuntime.exec` + the PAT; tier-agnostic):
  - `hydrate(sandbox, workspace, *, branch, worktree=None)`: clone or fetch; checkout the
    target branch; recreate the `worktrees/<slug>` dir if needed; set `push.autoSetupRemote`
    + user identity (parity with `worktrees.py`).
  - `persist(sandbox, workspace, *, branch, message, author)`: stage, commit (per-user
    identity), push with the PAT; return the pushed SHA.
  - `has_uncommitted(sandbox, path)`: teardown safety.
- **Ephemeral (Tier 1) hydration**: because each run gets a fresh sandbox from the baked
  snapshot, hydrate = `git fetch origin <branch>` + `checkout`/`reset` (deps already baked —
  Epic 05). Falls back to full clone if the repo isn't present.
- **Persistent (Tier 2) refresh**: on human open, `pull`/`fetch` the branch so the IDE shows
  the agent's latest pushed work (surface divergence rather than silently overwriting local
  edits).
- **Teardown policy** (configurable, default safe):
  - Tier 1: auto-commit remaining WIP to the run branch (or a `wip/` ref) before deleting the
    sandbox, so nothing is lost if the loop ends mid-edit.
  - Tier 2: state persists in the (stopped) sandbox; commits are user-driven.
- **Divergence**: if the remote branch advanced, fetch + rebase/merge with a clear conflict
  surface; never silent force-push.
- Extend `sanitize_git_output` coverage so PATs never appear in hydrate/persist logs.

## Database Tasks

- Per run/session: `hydrated_from_sha`, `pushed_sha`, `hydration_mode` (cold/warm),
  `hydrate_ms` / `push_ms` (observability + parity checks). Track `wip_ref` when
  auto-commit-on-teardown fires.

## Security & Operational Requirements

- PAT read from the encrypted store at hydrate/persist time; never written to disk in the
  clear; redacted from all output.
- Confine worktree reconstruction under the managed `worktrees/` dir (reuse the existing
  path-traversal protection in `worktrees.py`/`agent_tools.py`).
- Idempotent hydrate/persist (safe to retry after a transient sandbox failure).

## Acceptance Criteria

- A Tier-1 run in a throwaway sandbox hydrates the correct branch, commits, and pushes; a
  later Tier-2 open of that branch shows those commits.
- Deleting a Tier-1 sandbox mid-run loses no committed work; the WIP policy behaves as
  configured.
- A branch that advanced remotely between hydrate and push is handled without silent data
  loss (conflict surfaced).
- Hydrate/push timings are recorded (baseline for Epic 05 cold-start work).
- PATs never appear in logs.

## Dependencies

- Depends on **01** (tiering). Consumed by **02** (ephemeral) and **03** (persistent refresh).
  Reuses `repositories.py` / `worktrees.py`.

## Future Enhancements

- Lockfile-keyed dependency cache to skip installs on hydrate.
- Streamlined "human takes over an agent run" hand-off (still commit-granular).
