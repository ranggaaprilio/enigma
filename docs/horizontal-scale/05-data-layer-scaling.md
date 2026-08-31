# Epic 05 — Data Layer Scaling

## Goal

Make PostgreSQL and Redis survive many API replicas and workers hitting them at once, and stop
the highest-growth tables from becoming a liability. The data layer is the one component **not**
made redundant by adding replicas — it must be tuned and protected deliberately.

## User Value

The platform stays fast and stable as teams, workspaces, and run history grow. No "too many
connections" outages, no unbounded table bloat, predictable query latency.

## Current State

- Engine is created with **no pool sizing**: `create_async_engine(settings.database_url,
  pool_pre_ping=True)` (`db/session.py:7`). That's SQLAlchemy's default QueuePool (~5 + 10
  overflow) **per process**. With N API replicas + M workers, total connections = N×15 + M×… —
  easy to exceed Postgres `max_connections`.
- One session per request via `get_db_session` (`db/session.py:11`); long-lived WebSockets
  hold behaviour worth auditing so a socket doesn't pin a connection for its lifetime.
- `agent_events` grows with **every** thinking/tool/token event (`event_payload`,
  `agents.py:97`) and is never pruned — the fastest-growing table under multi-team load.
- Command run logs and `notifications` also grow unbounded.
- Redis is a single instance (`services/redis.py:6`, `docker-compose.yml`) and is about to
  carry pub/sub, locks, queues, and rate limits (epics 02–04, 06) — it becomes critical infra.

## Scope

- Connection pooling strategy (app-side tuning + PgBouncer).
- Retention / rollup / partitioning for high-volume tables.
- Index review on hot paths.
- Redis capacity and availability planning.
- Optional read-replica routing for read-heavy endpoints.

## Backend Tasks

- **Tune the engine**: set `pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle` from
  config, sized against Postgres `max_connections` and the planned replica/worker counts.
  Document the formula.
- **Adopt PgBouncer** (transaction pooling) between the app and Postgres so connection count
  scales with active queries, not process count. Verify asyncpg + PgBouncer transaction-mode
  compatibility (disable server-side prepared-statement caching or use a compatible setting).
- **Audit WebSocket DB usage**: ensure long-lived streams (`api/agents.py`, `api/terminal.py`)
  do **not** hold a pooled connection while idle — acquire per query, release promptly.
- Add a separate, smaller pool (or dedicated PgBouncer db) for workers so a run surge can't
  starve the API of connections.
- Route safe read-only endpoints (dashboard aggregates in `services/dashboard.py`, list
  endpoints) to a **read replica** if/when one exists, behind a setting.

## Database Tasks

- **`agent_events` retention**: partition by time (monthly) or add a retention job that
  archives/deletes events older than a configurable window; keep a rolled-up summary
  (token totals already tracked on `agent_runs.tokens_used`) so cost analytics survives pruning.
- Same retention treatment for command logs and read/old `notifications`.
- **Index review** on high-volume / hot-path tables: `agent_events(agent_run_id, created_at,
  id)` (backfill — epic 03), `agent_runs(workspace_id, status)` (active-run check —
  `agents.py:158`), reaper index `(status, lease_expires_at)` (epic 04), membership lookups
  (`project_members`, `team_members` already indexed — `models/project.py:60-61,126-127`).
- Add `EXPLAIN`-backed evidence for each new index in the PR.

## Infra / Ops Tasks

- **Redis availability**: move from single-instance to a managed/HA Redis (replication +
  failover) before it carries queues/locks/pub-sub in production. Document what breaks on Redis
  loss and the degradation behaviour (per shared rule: return errors, don't hang).
- Consider separating concerns onto different Redis logical DBs or instances: pub/sub vs
  queue vs rate-limit/locks, so one hot workload can't starve another.
- Backup/restore runbook for Postgres (build on `scripts/clean_data.sh`), tested restore.

## Security Requirements

- PgBouncer and Redis reachable only from the app/worker network; auth enabled on both.
- No PII/secrets in event payloads that get archived; archives inherit the same access
  controls.

## Acceptance Criteria

- Under the epic 09 load test with the target replica/worker count, total Postgres connections
  stay within `max_connections` with headroom, and no request fails with a pool-timeout.
- `agent_events` (and command logs) have an enforced retention policy; table size is bounded
  and cost analytics still reconciles from `agent_runs.tokens_used`.
- Every hot-path query has a supporting index proven by `EXPLAIN`.
- Redis failover does not corrupt run state; the platform degrades to clear errors and
  recovers when Redis returns.
