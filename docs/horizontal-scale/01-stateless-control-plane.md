# Epic 01 — Stateless Control Plane Foundation

## Goal

Make a single backend replica safe to start, stop, and roll without corrupting state, and
prove the API carries **no request-path state that assumes a single process**. This is the
groundwork every later epic stands on — and several fixes here pay off immediately, even
before a second replica exists.

## User Value

Deploys stop wedging workspaces. Operators get real readiness signals and clean shutdowns.
Shared multi-team instances stop shipping insecure defaults.

## Current State

- `main.py:13` — the lifespan handler only closes the Daytona client on shutdown. There is
  **no startup reconciliation** and **no graceful drain** of in-flight work.
- Runs stuck in `thinking`/`executing` after a crash/deploy are never reset, and the
  per-workspace guard (`agents.py:158-162`) then **permanently blocks new runs** in that
  workspace.
- Only `/health` exists (`api/health.py`) — no `/ready` that reflects Postgres/Redis health.
- Insecure defaults ship in `config.py:12-18` (`auth_secret_key`,
  `workspace_env_encryption_key`, `auth_cookie_secure=False`) and `docker-compose.yml` bakes a
  real Fernet key. Unacceptable for a shared instance.
- State audit needed: `agent_runtime` (`agents.py:94`), command runtime (`commands.py:42-43`),
  `NotificationService.subscribers` (`notifications.py:18`), and `_BRIDGES`
  (`openhands_tools.py:64`) all hold process-local state. This epic *inventories and
  isolates* them behind interfaces; epics 02–04 replace the implementations.

## Scope

- Startup reconciliation of orphaned runs and sandboxes.
- Graceful shutdown: stop accepting work, drain or hand off in-flight streams, close pools.
- `/ready` and a config self-check that fails fast on insecure defaults.
- A **state-isolation refactor**: hide every process-local registry behind an interface with
  a single in-memory implementation today, so 02–04 can swap in distributed backends without
  touching call sites.

## Backend Tasks

- **Orphan reconciliation on startup** (`lifespan`): in a short transaction, find all
  `AgentRun`/command runs in `ACTIVE_RUN_STATUSES` (`agents.py:22`) with no live owner and
  mark them `error` with a reason like `interrupted_by_restart`. Emit a terminal event so any
  reconnecting UI closes cleanly. (Once epic 04 adds leases, key this off expired leases
  instead of "all active".)
- **Graceful shutdown**: on `SIGTERM`, flip a "draining" flag so `/ready` returns 503 and the
  LB stops routing; stop creating new runs; give in-flight WS a bounded window to flush; then
  close the DB engine and Redis pools. Wire to the app's shutdown path.
- **`/ready` endpoint**: check a Postgres `SELECT 1` and a Redis `PING`; return 200 only when
  both pass and the app is not draining. Keep `/health` as a cheap liveness probe.
- **Config self-check**: on boot when `environment != "local"`, refuse to start if
  `auth_secret_key` or `workspace_env_encryption_key` are still the defaults; force
  `auth_cookie_secure=True`. Validate `AUTH_SECRET_KEY`/Fernet key are present and well-formed.
- **State-isolation refactor** (no behaviour change yet): introduce interfaces —
  `RunRegistry`, `EventBus`, `RunLock` — and route `agents.py`, `commands.py`, `speckit.py`,
  and `notifications.py` through them. Ship in-memory implementations. This is the seam epics
  02–04 plug into.
- Remove the baked Fernet key and dev secret from `docker-compose.yml`; require them via env.

## Frontend Tasks

- Handle a terminal `interrupted_by_restart` event on the agent/command/notification streams:
  show "run interrupted, please retry" instead of a spinner that never resolves.
- Treat a `/ready` 503 (during deploys) as "reconnecting", not "error".

## Database Tasks

- Add an index supporting the reconciliation query (status + workspace) on `agent_runs` and
  the command-run table if not already covered.
- Add a nullable `error_reason` / `interrupted_at` column if the run model lacks a place to
  record why a run ended.

## Security Requirements

- Fail closed on insecure config outside `local`.
- Secure cookies + HTTPS/WSS assumptions documented for production.
- No secrets in compose files or images.

## Acceptance Criteria

- Killing the backend mid-run and restarting leaves **zero** runs stuck in an active status,
  and the affected workspace can immediately start a new run.
- `SIGTERM` drains cleanly: `/ready` flips to 503, no run is left half-written, the process
  exits within the grace window.
- `/ready` reflects real Postgres + Redis health; `/health` stays a pure liveness check.
- Booting with default secrets and `environment=production` **refuses to start** with a clear
  message.
- All process-local registries are reached only through `RunRegistry`/`EventBus`/`RunLock`
  interfaces (grep shows no direct dict access from API/service call sites).

## Implementation Notes (shipped)

What the code looked like when this epic started differed from "Current State" above in a few places: `/ready` (Postgres + Redis) and the default-secret self-check (`Settings.enforce_secure_defaults`, which already forced `auth_cookie_secure`) both existed. The work below fills the remaining gaps.

- **State seam** — `backend/app/services/runtime.py`: `RunRegistry`, `EventBus`, `RunLock` protocols with in-memory implementations. `AgentRuntime`, `CommandRuntime` and `NotificationRuntime` are thin views over them (shared `run_registry` / `event_bus`, namespaced topics). `AgentService.create_run` holds `RunLock` per workspace across its check-then-insert. `openhands_tools._BRIDGES` stays process-local on purpose: each bridge holds its run's event loop for the OpenHands worker thread and is never shared.
- **Reconciliation** — `backend/app/services/reconciliation.py`. In one transaction, active agent runs, `running` command runs and `running` spec phases (plus the workflows of those phases only — a workflow's own `running` also means "idle between phases") become `error`; runs get `error_reason = "interrupted_by_restart"`, agent runs an `error` event (`metadata.reason`), command runs a status frame with `reason`. Migration `0030_run_interruption` adds `error_reason` to `agent_runs`/`command_runs` and `(status, workspace_id)` indexes.
- **Lifecycle** — `backend/app/core/lifecycle.py` + `main.py` lifespan:
  1. startup: reconcile, then install the SIGTERM interposer;
  2. SIGTERM: `draining = True` → `/ready` 503 `draining`, agent/command/spec entry points raise `ServiceDrainingError` → 503 + `Retry-After: 5`; after `SHUTDOWN_DRAIN_DELAY_SECONDS` (5) the signal is forwarded to uvicorn (a second SIGTERM forwards immediately);
  3. lifespan shutdown: wait `SHUTDOWN_RUN_GRACE_SECONDS` (15) for in-flight runs and Spec Kit finalizers (all in `run_registry`), cancel the rest (agent runs cancelled while draining are *not* marked `stopped`), reconcile **only the ids this process owned**, then — in a `finally` — close Daytona, dispose the DB engine and restore the signal handler. There is no Redis pool to close — every caller opens and closes its own client.
  The SIGTERM handler itself only flips the flag; logging, the timer and the hand-off run on the loop via `call_soon_threadsafe`. An auto-chained `specify` refused by a drain stays `pending` (deferred, not failed).
- **Probes** — `/live` (new, dependency-free liveness), `/ready` (traffic gate, 503 while draining), `/health` unchanged (DB/Redis/Daytona diagnostics, still used by the dashboard's API status label).
- **Config** — `workspace_env_encryption_key` must be a valid Fernet key in every environment; `auth_secret_key` needs ≥ 32 characters outside `local`. `docker-compose.yml` no longer has secret fallbacks (`${VAR:?}`) and sets `stop_grace_period: 30s`.
- **Frontend** — `useReconnectingWebSocket` takes an optional `readinessCheck` (`isServerReady` → `/ready`): while it is false a reconnect stays "reconnecting" without spending retries (re-probed every 2 s, up to 60 s). Wired into the agent, command and notification streams. `TurnView` and the command panel show "Run interrupted by a server restart — please retry." for `interrupted_by_restart`.

### Production notes

- **One replica, `Recreate` deploys, until epic 04.** Startup reconciliation treats every active run as orphaned; a rolling deploy that overlaps two replicas would let the new one kill the old one's live runs (the shutdown sweep is already scoped to its own runs). Epic 04 (leases) must switch the startup sweep to expired leases. `RunLock` is also process-local; the durable cross-replica guard is a partial unique index on `agent_runs(workspace_id) WHERE status IN ('thinking','executing')`.
- **Stop timeout ≥ 30 s** (drain delay + run grace + pool close), e.g. Kubernetes `terminationGracePeriodSeconds: 30`. Point the readiness probe at `/ready` and the liveness probe at `/live`; never at `/health`, which fails when Daytona is down.
- **HTTPS/WSS only.** Any `ENVIRONMENT` other than `local` forces `Secure` cookies, so the API and its WebSockets must be served over TLS; it also refuses to boot without real `AUTH_SECRET_KEY`, `WORKSPACE_ENV_ENCRYPTION_KEY` and `METRICS_TOKEN`.
- **No secrets in images or compose files** — provide them through the environment / secret store.

