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

## Implementation Notes (shipped)

Epic 02 had already delivered part of this: the conditional claim (`claimed_at IS NULL`), so a
replayed job never re-runs; the `uq_agent_runs_one_active_per_workspace` backstop; and
`worker_lost` for a redelivered job. The per-job bridge lifecycle also already existed:
`OpenHandsAgentProvider.stream_response` registers its `DaytonaToolBridge` at stream start and
unregisters it in `finally`, so no change was needed. This epic added:

- **Leases** — migration `0033_run_leases` adds `heartbeat_at` and `lease_expires_at` to
  `agent_runs` and `command_runs`. Deviation: there is **no** `(status, lease_expires_at)`
  index. The reaper compares against the volatile `clock_timestamp()`, so an index on the lease
  could not serve as an index condition. The scan is already narrow (active rows only, at most
  one agent run per workspace). An indexed lease would also turn every heartbeat into a non-HOT
  update across all of the table's indexes. `claim_run` stamps the lease.
  `renew_lease` extends it, but only for the owning `worker_id` and only while the run is
  active. Every lease time is Postgres `clock_timestamp()`, never the worker's clock.
  Settings: `RUN_LEASE_SECONDS=60`, `RUN_HEARTBEAT_SECONDS=15` (validated to be shorter than
  the lease) and `RUN_REAPER_INTERVAL_SECONDS=30`.
- **Heartbeat** — `heartbeat()` (`services/leases.py`) runs beside each agent and command job
  (`_holding_lease` in `app/worker.py`). A renewal that raises is retried on the next beat. A
  renewal that matches no row means the run was already ended: an agent job stops its provider,
  and a command job, which has no stop signal, is cancelled. arq then redelivers it, and the
  redelivery finds the run terminal. A redelivered job that finds its run already claimed now
  ends it as `worker_lost` only if no live lease is held. A live lease means the first delivery
  is still running, and that delivery is left alone.
- **Reaper** — `services/reaper.py`. Each worker starts `run_reaper` at startup (it reaps once,
  then every interval) and cancels it at shutdown. It ends active runs with an expired lease as
  `error`/`interrupted_worker_died` through `fail_active_run(..., lease_expired=True)`, and that
  UPDATE re-checks the expiry. A renewal that races the scan therefore wins, and concurrent
  reapers end a run exactly once. Each reaped run gets the usual flow: the error event is
  persisted, then published with `metadata.reason`, and the user gets a notification. Runs with
  no lease (inline mode) are never touched.
- **Lost jobs** — the same pass (`reap_lost_jobs`, given arq's Redis by the worker) ends runs
  that were enqueued but never claimed, once their arq job is gone (a Redis flush, or arq
  dropping the job after its last try), as `error`/`job_lost`. Age cannot tell a lost job from
  one waiting behind a busy pool, so Redis decides: `job_is_known` (`services/jobs.py`) asks arq
  for the job's status, and only `not_found` counts. A run whose job is still queued, deferred
  or in progress waits however long it takes. A Redis error skips the run, so an outage never
  looks like a loss. `RUN_UNCLAIMED_GRACE_SECONDS` (120) skips young runs, which covers the gap
  between committing `enqueued_at` and the enqueue itself. `fail_active_run(..., unclaimed=True)`
  re-checks `claimed_at IS NULL`, so a claim racing the reaper wins. Counted in
  `reaper_lost_jobs_total{kind}`, apart from worker deaths.
- **State machine** — `AgentService._transition` is the single conditional status write
  (`… WHERE status IN ('thinking','executing')`). Active statuses move freely, terminal statuses
  are final, and ending a run clears its lease. `stop_run`, `_mark_stopped`, `_mark_error`,
  provider `status` events and the final completion all go through it. A write that loses the
  race persists and publishes nothing, and a provider still streaming for a run that was ended
  elsewhere is abandoned. `stop_run` on a run that has already ended is a no-op that returns the
  run. `CommandService._mark_finished` is conditional in the same way.
- **Cross-replica lock** — `create_run` takes `pg_advisory_xact_lock(hashtextextended(
  'agent_run:workspace:{id}', 0))` inside the existing in-process lock, and holds it until its
  commit. This uses Postgres rather than Redis, so it works the same in inline and queue mode
  and adds no new failure dependency. If the lock call errors, the request fails before any
  insert, so it fails closed. The workspace UUID is the namespace; a hash collision between two
  workspaces would only serialise them. The unique index remains the guarantee.
- **Frontend** — `interruptionMessage()` (`lib/api.ts`) maps `interrupted_by_restart` and
  `interrupted_worker_died` to a retry prompt in `TurnView` and `CustomCommandsPanel`. A
  `stopped` turn now shows a neutral "This run was stopped." notice instead of no notice.
- **Tests** — `tests/test_run_leases.py` covers:
  - idempotent stop;
  - a late provider `completed` after a stop on another replica;
  - `_mark_error` and a command finish not overwriting a terminal run;
  - lease stamping and owner-only renewal;
  - the heartbeat loop, and a worker that loses its lease stopping its run;
  - the reaper on agent and command runs, ignoring live and unleased runs, never taking over a
    renewed lease, and two concurrent reapers on separate connections;
  - the workspace being reusable after a reap;
  - two "replicas" racing `create_run` (exactly one wins) and `create_run` blocking on the
    advisory lock.

  Frontend coverage is in `TurnView.test.tsx` and `CustomCommandsPanel.test.tsx`.

### Not covered

- **The `kill -9` acceptance test is simulated, not run against a live worker.** The tests
  expire a lease and reap it. Nothing kills an arq process.
- **Command creation is not locked.** Command runs have no single-active invariant, so there is
  nothing to serialise.
- **Inline-mode runs have no lease.** Inline is single-replica, and its startup sweep still
  covers it.
- **The API does not run a reaper.** If every worker is dead, nothing reaps until a worker
  starts, and that worker reaps on startup.
- **A cancelled command job does not kill its sandbox process.** The job stops streaming and
  writing, but the command itself runs on in the sandbox until its own timeout.
- **Per-run event sequence numbers**, which epic 03 deferred to this epic, are not implemented.
- ~~**A run that is never claimed is never reaped.**~~ Fixed: lost jobs are reaped (see above).
  What remains is a queue-mode run whose API died between inserting the row and stamping
  `enqueued_at`. It has no `enqueued_at`, so the reaper cannot tell it from an inline run.
