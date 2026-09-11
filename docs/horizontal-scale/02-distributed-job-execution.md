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
