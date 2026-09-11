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
