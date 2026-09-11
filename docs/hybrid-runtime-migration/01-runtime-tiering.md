# Epic 01 — Runtime Tiering (Execution vs Session)

## Goal

Teach the codebase the difference between two **roles** of a Daytona sandbox — an
**execution sandbox** (ephemeral, for agent runs) and a **session sandbox** (persistent, for
humans) — and add a **router** that picks the right one per operation. Both roles are backed
by the same `DaytonaRuntime`. This is a near-pure refactor: with the router defaulting every
operation to today's persistent sandbox, **behavior is unchanged** until Epics 02/03 turn on
the tiers. It is the seam the whole hybrid model plugs into.

## Why First

Today agent execution (`agent_tools.py`, `openhands_tools.py`) and human features
(`editor.py`, `terminal.py`, `dev_server.py`, `preview_proxy.py`) all resolve the **same**
`workspace.daytona_sandbox_id`. To route agent work to a throwaway sandbox while humans keep
a persistent one, the app first needs a concept of "which sandbox does *this* operation
use". Introduce it once, here, so Epics 02/03 are just wiring.

## Current State

- `DaytonaRuntime.provision` (`daytona_runtime.py:105`) always creates one persistent
  sandbox named `execute-<workspace_id>` and stores it in `workspace.daytona_sandbox_id`.
- `get_sandbox(workspace)` (line 147) resolves **that** sandbox for every consumer.
- There is no per-agent-run sandbox and no notion of sandbox role/tier.
- `Workspace.runtime` = `"daytona"`; `sandbox_id` property aliases `daytona_sandbox_id`.

## Scope

- A `SandboxRole` concept: `SESSION` (persistent) and `EXECUTION` (ephemeral).
- Role-aware provisioning + resolution in `DaytonaRuntime`.
- A **router** that maps an operation/intent → the sandbox it should use.
- Data model to hold an execution sandbox id **per agent-run** (separate from the workspace's
  session sandbox).
- A `hybrid` feature flag; when off, the router returns the session sandbox for everything
  (today's behavior).
- **Out of scope**: actually creating ephemeral sandboxes for runs (Epic 02) and changing
  the session sandbox lifecycle (Epic 03).

## Backend Tasks

- Extend `DaytonaRuntime` with role awareness (keep the existing methods working):
  - `provision(..., role: SandboxRole = SESSION)` — parameterize the create params that
    already differ by tier: `ephemeral`, `auto_stop_interval`, `auto_delete_interval`,
    `name` (session = `execute-<ws>`; execution = `run-<ws>-<run_id>`), and `labels`
    (add `execute-sandbox-role`).
  - `get_sandbox` gains an explicit sandbox id/handle argument path so a caller can resolve
    an execution sandbox by its own id, not just `workspace.daytona_sandbox_id`.
  - `delete` works for an arbitrary sandbox id (needed to tear down ephemeral sandboxes).
- New `services/runtime/router.py`:
  - `sandbox_for(workspace, *, intent) -> SandboxTarget` where `intent ∈ {agent_run,
    editor, terminal, preview, dev_server, files, spec}`.
  - When `hybrid` is off → always the session sandbox (no change).
  - When on → `agent_run`/`spec-implement` → execution sandbox (created/held by the run,
    Epic 02); human intents → session sandbox (Epic 03).
  - Encapsulate the mapping so consumers ask the router, not `workspace.daytona_sandbox_id`.
- Rewire consumers to resolve via the router + the intent they represent (mechanical: agent
  tools pass `agent_run`, editor passes `editor`, etc.).
- Keep `get_daytona_runtime()` as-is; the router sits above it.

## Database Tasks

- `agent_runs` (and `agent_sessions` / `spec_workflows` if they drive execution): add
  `execution_sandbox_id` (nullable) + `runtime_role` snapshot, so an ephemeral sandbox is
  tracked against the run that owns it (not the workspace).
- Workspace: rename-in-spirit `daytona_sandbox_id` to "the **session** sandbox" (no column
  change needed; document it). Optionally add `hybrid_enabled` (bool) for per-workspace
  rollout (Epic 06).
- Config: add `hybrid_enabled` global default + `daytona_execution_auto_delete_minutes` and
  `daytona_session_auto_stop_minutes` (so the two tiers get independent lifecycle knobs).

## Security & Operational Requirements

- Egress policy (`_desired_network_settings`) applies to **both** roles unchanged.
- Command-safety layer in `agent_tools.py` is above the runtime and is untouched.
- Labels on every sandbox (`role`, `workspace-id`, `owner-id`, and `run-id` for execution)
  so orphans can be found and reaped (Epic 05).

## Acceptance Criteria

- With `hybrid` **off**, every feature behaves exactly as today (terminal, editor, preview,
  dev server, agent runs, worktrees, specs) — proven by the existing test suite.
- The router resolves the correct sandbox target per intent, and can be unit-tested in
  isolation (on → agent_run maps to execution; human intents map to session).
- `DaytonaRuntime` can provision, resolve, and delete a sandbox by arbitrary id/role.
- No consumer reads `workspace.daytona_sandbox_id` directly anymore; all go through the
  router.

## Dependencies

- Blocks **02, 03**. Depends on nothing. Reversible (flag off = no-op).

## Future Enhancements

- Capability flags per role (execution role advertises `supports_preview = False`) so the UI
  degrades gracefully.
- A generic `sandbox_ref` abstraction if a second provider is ever added (not planned).
