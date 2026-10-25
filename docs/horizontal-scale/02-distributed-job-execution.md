# Epic 02 — Distributed Job & Run Execution

## Goal

Move agent runs, custom-command runs, and Spec-Kit phases off the API event loop and onto a
**dedicated worker fleet** driven by a shared job queue. Execution stops being tied to the
request/WebSocket that started it, survives API deploys, and scales independently of the API.

**This is the keystone of horizontal scale.** Until runs are jobs, no number of API replicas
helps.

## User Value

Agent work keeps running when the API restarts. Teams don't contend for one process's CPU.
Operators scale "how many runs at once" (workers) separately from "how much web traffic"
(API replicas).

## Current State

- Runs are launched **in the API process**: `asyncio.create_task(self._execute_run(...))`
  (`agents.py:203`), custom commands (`commands.py:216`), Spec-Kit phase finalisation
  (`speckit.py:336`).
- The heavy loop runs in a thread inside that process: `asyncio.create_task(asyncio.to_thread(worker))`
  (`openhands_provider.py:226`, `agent_providers.py:155`). Concurrency is bounded by the
  default thread pool and competes with request handling.
- `agent_runtime` (`agents.py:94`) tracks the `asyncio.Task` handles so it can cancel/stop —
  purely in memory.
- A per-workspace "one active run" rule is enforced by a DB check (`agents.py:158-162`), which
  is a good invariant but is currently guarded only within one process.

## Scope

- Introduce a job queue and worker process type.
- Convert run creation into **enqueue + return**; workers execute.
- Preserve today's guarantees: single active run per workspace, budget check before spend,
  audit logging, event/file-change persistence.
- Keep the API able to *observe* and *signal* runs it did not start (ties into epic 03 for
  streaming and epic 04 for stop/leases).

## Design

- **Queue technology**: prefer **arq** (async-native, Redis-backed, minimal deps — fits the
  existing `redis.asyncio` usage in `services/redis.py`). Celery/Dramatiq are acceptable if a
  broader task ecosystem is wanted. Decision recorded in the epic PR.
- **Worker process**: a new entrypoint (`app.worker`) sharing the same models/services. Runs
  the same `_execute_run` logic, but as a job handler, not an `asyncio.create_task`.
- **Job payload**: `run_id` only. The worker loads the run + workspace from Postgres — the DB
  is the source of truth, the queue just carries the trigger. This keeps jobs idempotent and
  replay-safe.
- **Provider execution**: OpenHands/OpenAI loops run on the worker. Since the worker's whole
  job *is* the run, the `to_thread` hop can stay or become the worker's main coroutine.
- **Backpressure**: bounded worker concurrency per process; queue depth becomes the
  autoscaling signal (epic 08). Enforce a global/per-tenant cap here (epic 06).

## Backend Tasks

- Add the queue client + settings (`REDIS_URL` reuse or a dedicated broker URL, concurrency,
  visibility timeout, max retries).
- `AgentService.create_run`: keep the validation, budget check (`agents.py:165`), row insert,
  and audit log **synchronously** in the request; replace the `create_task` (`agents.py:203`)
  with `enqueue(run_id)`. Return the run in `thinking` immediately.
- Move `_execute_run` into a worker job handler; ensure all DB access uses the worker's own
  `async_sessionmaker`, not a request-scoped session.
- Do the same for custom commands (`commands.py:216`) and Spec-Kit phase runs
  (`speckit.py:336`).
- Make jobs **idempotent**: a job that finds its run already terminal exits cleanly (handles
  at-least-once delivery).
- Emit the same events/file-changes as today, but publish them via the epic 03 `EventBus`
  rather than the in-process `subscribers` set.
- Dead-letter handling: a job that crashes N times marks the run `error` with a reason and
  publishes a terminal event.

## Frontend Tasks

- None structural — the API contract for "create run" is unchanged (still returns a run in
  `thinking`). Streaming changes are covered by epic 03.
- Optional: surface "queued" vs "running" if queue waits become visible under load.

## Database Tasks

- The single-active-run check (`agents.py:158-162`) must become race-safe across enqueuers:
  enforce with a **partial unique index** (e.g. unique on `workspace_id` where
  `status in active`) or a Postgres advisory lock, so two concurrent API replicas can't both
  enqueue. (Distributed locking detail lives in epic 04; the index is the durable backstop.)
- Add columns for job bookkeeping if needed: `enqueued_at`, `started_at`, `worker_id`,
  `attempt`.

## Security Requirements

- Workers decrypt workspace secrets/PATs via `WorkspaceSecretCipher` exactly as the API does;
  the encryption key reaches workers via env, never the queue payload.
- Job payloads carry **no secrets** — only `run_id`.
- Workers honour the same command denylist/allowlist and path confinement
  (`agent_tools.py`) as today.

## Acceptance Criteria

- Creating a run returns immediately; the run executes on a worker, not the API.
- Restarting **the API** does not interrupt a running job.
- Running with **zero API replicas serving that user** (e.g. traffic on replica A, worker on
  host C) still executes the run to completion.
- Two API replicas cannot both start a run for the same workspace (the partial unique index /
  lock holds).
- Worker count can be scaled up/down independently and throughput tracks worker count.
- At-least-once delivery causes no double-execution (idempotency verified by test).

## Implementation Notes (shipped)

Shipped together with the smallest slice of epic 03 that queue mode needs to work end to end (a Redis event bus and a stop control channel). Everything is behind **`RUN_EXECUTION_MODE=inline|queue`**, default `inline`, which behaves exactly as before. Where the code differed from "Current State" above: epic 01 had already replaced `agent_runtime`'s dicts with the `runtime.py` seam; Spec Kit phases are ordinary agent runs (only the phase *finalizer* is separate work); custom commands have no one-active-run rule; and `started_at` on both run tables is really the creation time.

- **Queue: arq 0.28** (Redis-backed, async). arq caps `redis<6`, so `redis` is pinned `>=5.1,<6` (it was resolving 7.4; our code only uses 5.x APIs). arq is in maintenance-only mode — all enqueueing goes through one seam (`services/jobs.py`: `RunDispatcher` / `ArqDispatcher`), so swapping it touches one file.
- **Enqueue** — `AgentService.create_run` / `CommandService.create_run` keep validation, budget check, insert and audit in the request, stamp `enqueued_at`, then enqueue the **run id only** (job ids `agent_run:{id}`, `command_run:{id}` so arq dedupes). If the enqueue fails the run is ended `error`/`enqueue_failed` (so the workspace is not wedged) and the API answers **503 + `Retry-After`** (`RunQueueUnavailableError`, global handler).
- **Single active run across replicas** — migration `0031_run_job_queue` adds the partial unique index `uq_agent_runs_one_active_per_workspace` on `agent_runs(workspace_id) WHERE status IN ('thinking','executing')` (cleaning up any pre-existing duplicates first). The insert runs in a savepoint; a violation maps to the existing `AgentRunAlreadyActiveError` → 409. The in-process `RunLock` stays as the cheap first check.
- **Worker** — `app/worker.py`, run with `uv run arq app.worker.WorkerSettings` (compose service `worker`, profile `queue`). Jobs: `run_agent_job`, `run_command_job`, `finalize_spec_phase_job`. The worker refuses to start unless `RUN_EXECUTION_MODE=queue`.
- **At-least-once delivery, at-most-once execution** — a worker *claims* a run with a conditional `UPDATE … WHERE status is active AND claimed_at IS NULL` that stamps `claimed_at`, `worker_id`, `attempt` (new columns). Outcomes:
  - claimed → execute;
  - already terminal (e.g. stopped while queued) or missing → exit cleanly;
  - **active but already claimed** → a redelivery after the owning worker died: the run is ended `error`/`worker_lost`, never re-run (agent and command runs have side effects).
- **Failures** — arq only retries a job that raises `Retry` (or is cancelled); any other exception drops it. So:
  - a failed claim raises `Retry`, and on the last try the run is ended `error`/`dead_letter`;
  - a run that exceeds `AGENT_RUN_TIMEOUT_SECONDS` is ended `error`/`job_timeout` by the handler itself (arq's `job_timeout` sits above it as a backstop); a command gets the same treatment past `COMMAND_RUN_MAX_TIMEOUT_SECONDS` + 60 s;
  - anything else escaping a command run ends it `error`/`worker_error`;
  - on a worker, cancellation never marks a run `stopped` (`leave_active_on_cancel`).
- **Spec Kit finalizer** — in queue mode `run_phase` enqueues `finalize_spec_phase_job(phase_id, run_id)` instead of an in-process task. The job checks the run once and `Retry`s itself every `SPEC_FINALIZE_POLL_SECONDS` (no worker slot is held while a phase waits); after the old 2 h budget it fails the phase. It is a no-op if the phase is no longer `running` for that run. The constitution → specify auto-chain enqueues through the same path.
- **Events and stop (epic 03 slice)** — `RedisEventBus` (`services/runtime.py`) implements the `EventBus` protocol over Redis pub/sub: one pub/sub connection and one reader task per process, fanning out to local queues; `wait_ready(topic)` returns once Redis confirms the subscription. It is selected by the flag, so in queue mode notifications also cross processes. `stop_run` publishes `{"action": "stop"}` on `agent_run_control:{id}` after its commit; the worker watches that topic for the run it executes and applies it to its local registry (stop event + `provider.pause()`). Because the bus is at-most-once, the worker also re-reads the run's status every 5 s and stops it if it is no longer active; it also checks once right after subscribing, so a stop that landed before then is not missed. `RedisEventBus.publish` is best-effort: it logs and swallows Redis errors, so a blip never fails the run that is publishing. A failed SUBSCRIBE is retried while anyone still listens.
- **Streams** — the agent WebSocket waits for its subscription to be live, then sends any persisted events it has not sent yet (deduplicated by event id), and repeats on every idle ping tick. It closes once the run has ended. The command WebSocket waits for its subscription before its existing re-check.
- **Restarts** — in queue mode the API owns no runs and skips the startup sweep. A worker reconciles **only rows with its own `worker_id`** at startup (left by a crash) and at shutdown. On SIGTERM arq stops taking jobs and gives running ones `job_completion_wait` (= `SHUTDOWN_RUN_GRACE_SECONDS`) to finish.

### Production notes

- **Redis is now a trust boundary.**
  - Jobs are serialized as JSON, not arq's default pickle, so write access to Redis does not give code execution on a worker.
  - Write access to Redis can still stop runs or inject fake frames into live streams. Postgres stays authoritative, and the end-of-run catch-up replays from the database.
  - Run production Redis with auth and TLS (`rediss://user:pass@…` in `REDIS_URL`), on a private network only.
  - The dev compose file publishes an unauthenticated Redis on the host port; do not reuse it outside a workstation.
- **Switch modes only with no active runs.** Inline-mode rows have no `worker_id`, and the queue-mode API does not sweep at startup.
- **`WORKER_ID` must be unique per live worker and stable across its restarts** (default: hostname). A worker that never comes back leaves its claimed runs active until arq redelivers the job (bounded by `WORKER_JOB_TIMEOUT_SECONDS`, which then ends them `worker_lost`), or until epic 04's lease reaper.
- **Not covered yet (epics 03/04):**
  - ~~A run whose job is lost from Redis (e.g. a flush) or never picked up has no age-based sweep, so it holds its workspace until stopped.~~ Fixed in epic 04 (the reaper ends it as `job_lost`).
  - ~~Command output written between a WebSocket's first read and its live subscription is not re-sent.~~ Fixed in epic 03 (output offsets).
  - `stop_run` vs. the worker's own terminal write is still last-writer-wins (conditional transitions are epic 04).
  - When a worker shuts down while a run is still active, the run's recorded reason can come out as `worker_lost` instead of `interrupted_by_restart`, if a peer worker picks up the re-queued job before the shutdown sweep runs.
- **Scaling** — throughput scales with `worker replicas × WORKER_MAX_JOBS`. Queue depth (`ZCARD` of `RUN_QUEUE_NAME`) is the autoscaling signal for epic 08.
