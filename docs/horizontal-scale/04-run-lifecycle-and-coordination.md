# Epic 04 — Run Lifecycle Durability & Distributed Coordination

## Goal

Make the run lifecycle **correct under crashes and races** once execution is distributed
(epic 02) and streaming is on a bus (epic 03). Add ownership leases, heartbeats, automatic
recovery of dead runs, and distributed locks for invariants that were previously guaranteed by
"it all runs in one process".

## User Value

No workspace ever gets permanently wedged by a crashed worker. No run silently dies without
the UI being told. No double-execution or two-active-runs races when many replicas and workers
are live.

## Current State

- Ownership is an in-memory `asyncio.Task` handle (`agent_runtime.tasks`, `agents.py:54`) with
  a done-callback cleanup (`agents.py:60`). Nothing survives a process exit.
- The "one active run per workspace" invariant is a pre-insert DB `SELECT`
  (`agents.py:158-162`) — a check-then-act race between concurrent enqueuers.
- Stop relies on the owning process being alive (`agents.py:70`).
- There is no heartbeat, so a worker that dies mid-run leaves the row `executing` forever
  (the problem epic 01 papers over at startup; this epic solves it continuously).
- `_BRIDGES` (`openhands_tools.py:64`) is a process-global tool-bridge cache keyed per run —
  fine on a worker that owns the run, but must be created/torn down per job, not shared.

## Scope

- Run ownership **leases** with periodic heartbeats.
- A **reaper** that recovers runs whose lease expired (crashed worker).
- **Distributed locks** for per-workspace single-active-run (and any other cross-replica
  invariant).
- Idempotent, replay-safe run state transitions.

## Design

- **Lease**: when a worker picks up a job it writes `worker_id`, `lease_expires_at`, and
  bumps a heartbeat column every T seconds while running. Stop/finish clears the lease.
- **Reaper**: a periodic job (on workers, or a small scheduler) finds runs in
  `ACTIVE_RUN_STATUSES` (`agents.py:22`) with `lease_expires_at < now()`, marks them `error`
  (`interrupted_worker_died`), publishes a terminal event over the bus (epic 03), and frees
  the workspace's active-run slot. This generalises epic 01's startup reconciliation into a
  continuous safety net.
- **Distributed lock for single-active-run**: acquire a Redis lock (or Postgres advisory lock)
  keyed `workspace:{id}:active-run` around the check-insert-enqueue path, **plus** the partial
  unique index from epic 02 as the durable backstop. Lock = fast rejection; index =
  correctness guarantee even if the lock service blips.
- **State machine**: define explicit transitions (`thinking → executing → completed|error|
  stopped`) and make each transition a conditional UPDATE (`... WHERE status = expected`) so
  concurrent actors can't clobber each other. Stops and completions become idempotent.
- **Per-job bridge lifecycle**: create the `DaytonaToolBridge` at job start and drop it at job
  end; do not rely on the module-level `_BRIDGES` dict (`openhands_tools.py:64`) surviving or
  being shared across processes.

## Backend Tasks

- Add lease/heartbeat columns to the run models (`worker_id`, `heartbeat_at`,
  `lease_expires_at`) via migration.
- Heartbeat loop in the worker job handler; renew while the provider runs; release on exit.
- Implement the reaper (interval configurable) and wire terminal-event publication.
- Wrap run creation (`agents.py:145-205`) and command creation (`commands.py`) in the
  distributed lock; keep the DB uniqueness backstop.
- Convert status writes throughout `agents.py` (e.g. the transitions around
  `agents.py:661-687`, `agents.py:732`) to conditional UPDATEs.
- Ensure `stop` is idempotent: stopping an already-terminal run is a no-op that still returns
  cleanly.

## Frontend Tasks

- Handle new terminal reasons (`interrupted_worker_died`) with a clear, retryable message.
- Reflect a `stopped` vs `error` distinction the user can act on.

## Database Tasks

- Migration for lease/heartbeat columns + index on `(status, lease_expires_at)` for the reaper
  query.
- Partial unique index enforcing one active run per workspace (shared with epic 02).

## Security Requirements

- Only the run's owning worker may renew its lease; the reaper only *expires* leases, it never
  hijacks a live one.
- Lock keys are tenant-namespaced; a lock bug must fail closed (reject the run), never allow a
  cross-tenant action.

## Acceptance Criteria

- `kill -9` on a worker mid-run: within one lease interval the run flips to `error`, the UI is
  notified, and the workspace can start a new run — no manual DB fix.
- Two API replicas racing to start a run for the same workspace: exactly one succeeds; the
  other gets `AgentRunAlreadyActiveError`.
- Replaying a job (at-least-once delivery) or double-clicking stop causes no invalid state
  transition.
- No run can be left active with an expired lease for longer than the reaper interval.
