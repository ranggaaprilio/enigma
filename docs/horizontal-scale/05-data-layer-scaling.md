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

## Implementation Notes (shipped)

The audit found the WebSocket concern already handled. All six handlers (`agents`, `commands`,
`notifications`, `terminal`, `presence`, `workspace_services`) authenticate in a short
`async with async_session_maker()` block, close it before `websocket.accept()`, and stream
reads open their own short sessions. The hot-path indexes from epics 03 and 04 also existed.
This epic added:

- **Pool sizing**: `engine_options(settings)` (`db/session.py`) builds the engine from
  `DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=10`, `DB_POOL_TIMEOUT=30` and `DB_POOL_RECYCLE=1800`, with
  `pool_pre_ping` kept. The formula is per process:

  ```
  peak connections = Σ over processes (DB_POOL_SIZE + DB_MAX_OVERFLOW)
                   = api_replicas × 15 + workers × 5 + preview_proxy × 15 + 1 (migrations)
  keep peak ≤ max_connections − superuser_reserved_connections (3) − headroom (~10%)
  ```

  Postgres' default `max_connections=100` fits 5 API replicas (75) plus 4 workers (20).
  Beyond that, use PgBouncer instead of raising `max_connections`.
- **Worker pool**: compose gives `worker` its own pool, `DB_POOL_SIZE=3` and
  `DB_MAX_OVERFLOW=2` (via `WORKER_DB_POOL_SIZE` and `WORKER_DB_MAX_OVERFLOW`). A surge in runs
  therefore waits on the worker's own pool (up to `DB_POOL_TIMEOUT`) and cannot take connections
  the API needs. If runs on a busy worker start hitting pool timeouts, raise the worker pool
  together with `WORKER_MAX_JOBS` rather than separately.
- **PgBouncer**: an opt-in `pgbouncer` compose profile (`edoburu/pgbouncer:v1.25.2-p0`):
  - transaction pooling and `scram-sha-256` client auth;
  - no host port, so only the compose network reaches it;
  - `SERVER_RESET_QUERY=DISCARD ALL` with `SERVER_RESET_QUERY_ALWAYS=1`.

  With `DB_PGBOUNCER=true` the app follows SQLAlchemy's asyncpg-behind-PgBouncer guidance. It
  uses `NullPool` (PgBouncer is the pool, so the app keeps none of its own), sets
  `statement_cache_size=0` and `prepared_statement_cache_size=0`, and gives prepared
  statements unique `__asyncpg_<uuid>__` names.

  Verified against a live PgBouncer with a 5-connection server pool: 100 concurrent sessions ×
  20 repeated parameterized queries, the `pg_advisory_xact_lock` from `create_run`, and a
  retention pass. PgBouncer ≥1.22 also tracks protocol-level prepared statements
  (`max_prepared_statements`), so the default settings survived the same load. The flag still
  matters: it stops double pooling and keeps older or managed poolers safe.

  The app uses only transaction-scoped Postgres state: no session advisory locks, `SET`,
  `LISTEN` or temp tables. Run Alembic against Postgres directly.
- **Retention**: `services/retention.py`, configured in days (`0` keeps rows forever, the
  default):
  - `RETENTION_AGENT_EVENTS_DAYS` deletes the events of finished runs and stamps
    `agent_runs.events_pruned_at`. The run row stays, including `tokens_used`, `input_tokens`,
    `output_tokens` and `estimated_cost`, which are all that usage and budgets read
    (`services/budgets.py`), so cost analytics still reconciles.
  - `RETENTION_COMMAND_OUTPUT_DAYS` empties `command_runs.output` of finished runs and stamps
    `output_pruned_at`. Status, exit code and timings stay.
  - `RETENTION_READ_NOTIFICATIONS_DAYS` deletes notifications read longer ago than the window.
    `RETENTION_NOTIFICATIONS_DAYS` deletes any notification older than the window, read or
    not. Neither window ever deletes `budget_warning` notifications. `_already_warned`
    de-duplicates against them (for the whole period, or forever for a `total` budget), so
    pruning one would send the same warning again.

  Active runs are never touched. Each batch (`RETENTION_BATCH_SIZE=50` runs or notifications;
  kept small because a long run can have thousands of token-streamed events)
  is one transaction, scanned with `FOR UPDATE SKIP LOCKED`. Passes on several workers
  therefore split the work, and each run is pruned exactly once. This is tested with two
  engines racing. No lock outlives a transaction, so retention is PgBouncer-safe.

  The loop (`RETENTION_INTERVAL_SECONDS=3600`) runs where runs run: on every worker in queue
  mode, in the API lifespan in inline mode. It is never started when every window is `0`.
- **Migration `0034_data_retention`**:
  - `agent_runs.events_pruned_at` and `command_runs.output_pruned_at`.
  - Partial indexes `ix_agent_runs_events_retention` and `ix_command_runs_output_retention` on
    `completed_at`, covering only finished, not-yet-pruned rows. They stay small, and a pass
    never re-reads history it already pruned.
  - `ix_notifications_user_created (user_id, created_at)`, which replaces
    `ix_notifications_user_id`.
  - Drops `ix_agent_events_agent_run_id`. It is a prefix of `ix_agent_events_run_created_id`
    (0032), so every event insert, on the fastest-growing table, maintained two indexes. With
    both present the planner also picked the narrower one and added a `Sort`.
- **Redis "error, don't hang"**: `redis_client_options()` (`services/redis.py`) applies to
  `get_redis_client()` and the `RedisEventBus` client:
  - `REDIS_SOCKET_TIMEOUT_SECONDS=5` and `REDIS_CONNECT_TIMEOUT_SECONDS=5`;
  - TCP keepalive and a 30s health check.

  Long-lived subscribers (`get_redis_client(subscriber=True)`) get no read timeout. Presence uses
  two clients: commands (join, heartbeat, leave, viewers) on a normal client, and only its
  `pubsub.listen()` on the subscriber client. This was checked against real Redis: a 0.5s `socket_timeout` makes an idle
  `pubsub.listen()` raise `TimeoutError`. The event bus keeps its read timeout because it polls
  with an explicit `get_message(timeout=1.0)`; it delivered normally after idling past the
  timeout. arq's pools take `conn_timeout` from the connect timeout (`arq_redis_settings`).
- **Backup and restore**:
  - `scripts/backup_db.sh` writes `pg_dump -Fc` to `backups/` (gitignored, mode 600, and only
    renamed from `.partial` once complete).
  - `scripts/restore_db.sh <dump>` restores into a scratch `<db>_restore_check` by default. It
    prints `alembic_version` and row counts, and `--drop-after` cleans up. Replacing the live
    database needs `--target <db> --yes` with backend and worker stopped.
  - Tested: a backup of the local database, restored to `0034_data_retention` with matching
    row counts.

  A dump is useless without the same `WORKSPACE_ENV_ENCRYPTION_KEY`. Store the key separately,
  with equal access control.

### Hot-path indexes (`EXPLAIN`, `enable_seqscan=off`; `tests/test_retention.py` asserts each)

Test tables are tiny, so the planner would pick a sequential scan. Turning sequential scans off
shows which index it *can* use.

| Query | Index | Plan |
|---|---|---|
| Stream backfill from a high-water mark (epic 03) | `ix_agent_events_run_created_id` | `Index Scan`, `Index Cond: agent_run_id = … AND created_at >= …`, no sort |
| One active run per workspace (`create_run`) | `uq_agent_runs_one_active_per_workspace` | `Index Scan`, `Index Cond: workspace_id = …` |
| Notification bell, newest 50 for a user | `ix_notifications_user_created` | `Index Scan Backward`, `Index Cond: user_id = …`, no sort |
| Agent event retention scan | `ix_agent_runs_events_retention` | `Limit → LockRows → Index Scan`, `Index Cond: completed_at < …` |
| Command output retention scan | `ix_command_runs_output_retention` | `Limit → LockRows → Index Scan`, `Index Cond: completed_at < …` |
| Notification retention scan | `ix_notifications_created_at` | `Limit → LockRows → Index Scan`, `Index Cond: created_at < …` |

Membership lookups were already indexed (`project_members` and `team_members`). The reaper
deliberately has no `(status, lease_expires_at)` index; epic 04 explains why.

### Redis loss: what breaks and how it degrades

Postgres is authoritative for all run state, so losing Redis cannot corrupt a run. What happens
when it is down:

| Area | Behaviour while Redis is down | Recovery |
|---|---|---|
| Login and run rate limits | The request fails with a 500 within `REDIS_SOCKET_TIMEOUT_SECONDS` instead of hanging (fail-open vs fail-closed is epic 06) | Automatic |
| Queue mode `create_run` | Enqueue fails within the connect timeout, and the run ends `error`/`enqueue_failed` (epic 02) | Retry the run |
| Live streams (queue mode) | `publish` is best effort and logs `event_bus_publish_failed`; open sockets still get events from their idle-tick Postgres re-reads (epic 03), just later | Bus resubscribes when Redis returns |
| Presence | The socket's relay errors and closes; the client reconnects | Automatic |
| Workers | arq cannot fetch jobs; running jobs keep executing and persisting to Postgres | Jobs resume. Jobs lost with Redis's data leave unclaimed runs stuck (the epic 04 gap below) |
| `/ready` | Answers 503 `not_ready` (Redis is a hard dependency), and now within the timeouts instead of hanging. **Every API replica leaves the load balancer at once**, so a Redis outage is a full API outage; `/live` stays 200, so nothing restarts | Replicas return to service on the next probe |

**Production Redis**: use a managed Redis with replication and automatic failover behind one
endpoint (ElastiCache, Memorystore, Azure Cache), with AOF persistence so queued jobs survive a
failover. Require auth and TLS (`rediss://`) on a private network. To separate workloads, give
the queue its own instance or logical DB; pub/sub ignores the DB number and is shared
cluster-wide anyway.

### Not covered

- **Load-test acceptance**: the "connections within `max_connections` under the epic 09 load
  test" check is not run here. The formula above is the sizing; the load test belongs to
  epic 09.
- **Partitioning `agent_events`**: deferred. Batched retention bounds the table without
  rewriting its UUID primary key and FK. Revisit if a single retention window outgrows vacuum.
- **Read replica routing**: deferred. No replica exists, and replica lag would break
  read-after-write on the list endpoints that would move to it.
- **Redis Sentinel and Cluster client support**: not implemented. The guidance above assumes a
  single-endpoint managed failover, which needs no code. arq does support Sentinel if it is
  ever needed.
- **Showing that history was pruned**: the UI does not yet mark it. `events_pruned_at` and
  `output_pruned_at` are recorded but not exposed by the API, so a pruned run shows an empty
  timeline and a pruned command run shows empty output.
- **Archiving**: there is none; pruned data is deleted, not archived. The archive security
  requirements therefore have nothing to apply to yet.
- **Separate Redis URLs per concern**: not added. Every client still reads `REDIS_URL`.
- **Enqueue on a partitioned Redis**: arq's pool has a connect timeout but no read timeout, so
  an enqueue against a Redis that accepted the connection and then went silent can still hang.
- **Redis in the readiness gate**: unchanged. Taking Redis out of `/ready` (so an outage only
  degrades Redis-backed features) is a policy change for epic 06/08.
- **Resumed sessions after pruning**: `_build_session_context` rebuilds earlier turns' assistant
  text from `output_text` events. Once a turn's events are pruned, resuming that session gives
  the model only the user prompts of those turns. Keep the events window longer than sessions
  are realistically resumed, or exclude open sessions' runs from pruning later.
- **Failing fast on a dead Redis**: enqueueing can still take ~30s, because arq retries the
  connection (`conn_retries=5`, 1s apart) before the run ends as `enqueue_failed`.
- **Dead-peer detection for an idle subscriber**: it relies on OS TCP keepalive (Linux default
  ~2h). Presence's commands fail fast, and the heartbeat surfaces an outage within one interval.
- ~~**Not-yet-claimed runs**: a run whose job is lost before any worker claims it still blocks
  its workspace (the remaining epic 04 gap).~~ Fixed: the reaper ends it as `job_lost` once
  arq no longer holds the job (epic 04).
