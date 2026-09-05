# Hybrid Runtime Migration — On Daytona

This folder plans moving Enigma from **one always-persistent Daytona sandbox per workspace**
to a **hybrid, two-tier model — still entirely on Daytona**:

- **Tier 1 — Ephemeral execution**: the AI gets a throwaway Daytona sandbox per agent-run
  (spin up → hydrate from git → run/test → push → delete).
- **Tier 2 — Persistent on-demand**: humans get the current Daytona sandbox for
  IDE/terminal/preview, but it starts **only on access** and auto-stops aggressively.

**We keep Daytona.** No AWS, no provider swap. This is a *usage + lifecycle* change that
leans on primitives Daytona already gives us, so it is far smaller than a provider migration.

It complements `docs/improvement-epics/` (feature backlog) and `docs/implementation-plan/`
(MVP build). Same discipline: one epic at a time, migrations + tests, land behind a flag.

## Why This Change

Enigma's human-facing surface is **secondary/occasional** — the AI does most of the work and
a person only sometimes opens the IDE or a preview. Yet today a single Daytona sandbox stays
provisioned for the whole workspace lifetime and serves both the agent and the human.

Splitting the two audiences means:

- **Agent work runs in clean, isolated, per-run sandboxes** — no state pollution between
  runs, parallel runs don't contend, and we pay for a sandbox only while a run is active.
- **The human sandbox exists only when a human is actually using it** — near-zero cost when
  a workspace is idle (the common case).
- **Git becomes the durable source of truth** connecting the two tiers.

## Why It's Cheap On Daytona (primitives we already have)

The AWS plan had to rebuild Daytona's control plane. Here we reuse it:

| Need | Daytona primitive (already in the code) |
|---|---|
| Throwaway sandbox per run | `CreateSandboxFromSnapshotParams(ephemeral=…)` + `auto_delete_interval` (`daytona_runtime.py:118-133`) |
| Auto-stop when idle | `auto_stop_interval` (currently `daytona_auto_stop_minutes = 30`) |
| Start-on-access | `_ensure_started` already auto-starts a stopped sandbox on use (`daytona_runtime.py:314`) |
| Preview URLs | `signed_preview_url` / `preview_link` (`daytona_runtime.py:295-312`) |
| Egress policy | `_desired_network_settings` / `_apply_network_settings` |
| PTY / exec / fs | `create_or_connect_pty`, `exec`, `stream_command`, `sandbox.fs.*` |
| Fast cold start | custom snapshot with baked deps (`daytona/Dockerfile`, `build-snapshot.sh`) |

So the work is mostly: **route agent runs to ephemeral sandboxes, tune the persistent
sandbox to on-demand, and make git the sync point** — not building infrastructure.

## Target Architecture

```
                    ┌─ Daytona runtime (one provider) ─┐
                    │                                   │
     AI runs test/build/command          Human opens IDE / preview
              │                                        │
   ┌──────────▼───────────┐              ┌─────────────▼────────────┐
   │ TIER 1: EPHEMERAL     │              │ TIER 2: PERSISTENT        │
   │ Daytona sandbox       │              │ Daytona sandbox           │
   │ ephemeral=True        │              │ persistent, on-demand,    │
   │ per agent-run, then   │              │ aggressive auto-stop      │
   │ deleted               │              │ (editor/terminal/preview) │
   └──────────┬───────────┘              └─────────────┬────────────┘
              └──────────────► GIT (durable) ◄─────────┘
                     hydrate from branch; push results
```

**Principle: Git is the source of truth.** Sync granularity between tiers is the *commit*,
not a live shared filesystem. Simultaneous human+AI edits to the same *uncommitted* tree are
out of scope (that is the old always-persistent model). Acceptable because human involvement
is occasional review/inspection.

## The Epics

| # | Epic | Priority | Theme |
|---|------|----------|-------|
| 01 | [Runtime Tiering (execution vs session)](./01-runtime-tiering.md) | **P1** | Foundation |
| 02 | [Tier 1 — Ephemeral Execution Sandboxes](./02-ephemeral-execution-tier.md) | **P1** | Agent execution |
| 03 | [Tier 2 — Persistent On-Demand Human Sandbox](./03-persistent-ondemand-tier.md) | **P1** | Human sessions |
| 04 | [Git as Source of Truth & Tier Sync](./04-git-source-of-truth-and-sync.md) | **P1** | Glue / correctness |
| 05 | [Lifecycle, Cost & Cold-Start Tuning](./05-lifecycle-cost-and-coldstart.md) | P2 | Operations |
| 06 | [Rollout & Migration (keep Daytona)](./06-rollout-and-migration.md) | P2 | Migration |

## Recommended Build Order

1. **01 — Runtime tiering** first. A near-pure refactor: teach `DaytonaRuntime` (and the
   consumers) the difference between an *execution* sandbox and a *session* sandbox, and add
   a router. Default everything to the current persistent sandbox so nothing changes yet.
2. **04 — Git source of truth** — the hydrate/push contract; pairs tightly with 02.
3. **02 — Ephemeral execution tier** — route agent runs to throwaway sandboxes. The headline
   win. Depends on 01 + 04.
4. **03 — Persistent on-demand tier** — make the human sandbox start-on-access + aggressive
   auto-stop; ensure agent runs no longer keep it alive. Depends on 01.
5. **05 — Lifecycle/cost/cold-start** — aggressive auto-stop tuning, ephemeral cleanup
   reaper, baked-deps snapshot, quotas, observability.
6. **06 — Rollout & migration** — flip workspaces to hybrid behind a flag; rollback = flag
   off. Daytona stays throughout.

## Shared Rules For Every Epic

- **Keep Daytona.** No epic removes the Daytona SDK or config. This is a lifecycle redesign,
  not a provider swap.
- **Land behind a `hybrid` flag** (per workspace/global). Flag off = today's behavior.
- Clear internal contracts before UI.
- A DB migration for every schema change (Alembic, async engine).
- Backend tests (`pytest`) for the tiering/router + lifecycle; frontend tests (Vitest) for
  state transitions (env "starting/stopped/ready").
- **Preserve the agent command-safety model** in `agent_tools.py` verbatim (denylist/
  allowlist, shell-operator blocking, path confinement, output truncation).
- Never log PATs/secrets; keep `sanitize_git_output` in every git path.

## Grounding: Current State Snapshot (July 2026)

- **One runtime class**: `backend/app/services/daytona_runtime.py` — `DaytonaRuntime` wraps
  the Daytona SDK; singleton via `get_daytona_runtime()` (`@lru_cache`, line 354).
- **Provisioning** (`provision`, line 105): idempotent by name `execute-<workspace_id>`;
  today sets `ephemeral=False`, `auto_stop_interval=daytona_auto_stop_minutes` (30),
  `auto_archive_interval=0`, `auto_delete_interval=-1`. **These flags are exactly the knobs
  the hybrid tiers turn.**
- **One sandbox per workspace**: `Workspace.daytona_sandbox_id` (`models/workspace.py:53`),
  `runtime` default `"daytona"` (line 52), `workspace_path` default `"workspace"`,
  `dev_autostart` (line 57). No per-agent-run sandbox concept yet.
- **Consumers** of the runtime: services `agent_tools.py`, `dev_server.py`, `editor.py`,
  `terminal.py`, `files.py`, `repositories.py`, `worktrees.py`, `speckit.py`,
  `openhands_tools.py`; APIs `workspaces.py`, `dev_server.py`, `agents.py`, `terminal.py`,
  `editor.py`; and `preview_proxy.py`. Agent execution (`agent_tools`/`openhands_tools`) and
  human features (`editor`/`terminal`/`dev_server`/`preview_proxy`) currently share the
  **same** sandbox — this epic set splits them.
- **Git**: `repositories.py` (GitHub clone/status/diff/pull/commit/push, per-user identity)
  and `worktrees.py` (`worktrees/<slug>` via raw `git worktree`). Reused as the sync layer.
- **Snapshot**: `daytona/Dockerfile` + `build-snapshot.sh` (mise, code-server, specify, uv;
  `--disk 10`; egress restricted). The baked-deps base that makes ephemeral cold-starts fast.

## Decision Log

- **Hybrid (two Daytona tiers), not a provider migration.** Keep Daytona; change how we use
  it. Reuse `ephemeral`/`auto_stop`/`auto_delete`/preview/egress primitives.
- **Tier 1 = ephemeral Daytona sandbox per agent-run**, deleted after push. Isolation +
  parallelism + pay-per-run; cold-start mitigated by the baked snapshot (Epic 05).
- **Tier 2 = the existing persistent sandbox, on-demand + aggressive auto-stop.** Editor/
  terminal/preview/dev-server code is reused as-is.
- **Git commit granularity for sync**; concurrent human+AI edits on the same uncommitted
  tree are out of scope.
- **Trade-off accepted**: ephemeral runs re-pay hydration (clone/fetch, maybe deps) per run.
  Mitigated by the baked snapshot and warm-cache options in Epic 05.
