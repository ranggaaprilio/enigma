# Epic 09 — Observability, Load Testing & Capacity Validation

## Goal

Prove the platform actually scales — and stay able to see why when it doesn't. Add
distributed-system observability (metrics, tracing, correlation) on top of
`improvement-epics/07`, then validate the whole series under realistic multi-team load with
defined SLOs. This epic is the **acceptance gate** for horizontal scale.

## User Value

Operators can see per-tenant load, cost, and latency across the fleet, diagnose issues that
span replicas and workers, and trust capacity numbers instead of guessing.

## Current State

- Structured logging exists (`core/logging.py`, `structlog`) but there is **no** `/metrics`,
  request-id correlation, or tracing (per `improvement-epics/07`, which this builds on).
- Cost per run is tracked (`agent_runs.tokens_used`, `core/pricing.py`, `estimate_cost` used in
  `agents.py:11`) but not exposed as fleet-wide, per-tenant operational metrics.
- No load/soak/chaos testing exists; there are unit/integration tests only.
- With execution now spanning API replicas → queue → workers → Daytona (epics 02–04), a single
  request's story crosses process boundaries and **must** be traceable end to end.

## Scope

- Metrics, distributed tracing, and cross-service correlation IDs.
- Per-tenant operational dashboards (load, latency, cost, quota usage).
- SLOs and alerting.
- A repeatable load/soak/chaos test suite that exercises the multi-replica path.
- A written capacity model.

## Backend / Observability Tasks

- **Correlation IDs**: request-id middleware (epic `improvement-epics/07`) that also
  **propagates the ID through the job payload** (epic 02) and onto published events (epic 03),
  so an agent run is traceable API → queue → worker → stream.
- **`/metrics`** (Prometheus): request rate/latency/errors, WS connection counts, queue depth
  and job wait/exec time, active runs, DB pool utilisation, Redis latency, per-provider LLM
  latency/tokens/cost.
- **Distributed tracing** (OpenTelemetry): spans across API → queue → worker → Daytona calls;
  export to a tracing backend.
- **Per-tenant labels** on metrics (project/team/workspace) so load and cost can be attributed
  — without high-cardinality blowups (bounded label sets, tenant IDs bucketed as needed).
- Bind everything into the existing `structlog` context (`core/logging.py`).

## Load / Chaos Testing Tasks

- A load harness (k6 / Locust) that simulates **many teams**: concurrent run creation,
  long-lived agent + terminal + preview WebSockets, commits/pushes, dashboard reads.
- Scenarios: steady-state throughput, burst (thundering-herd run creation), soak (hours, to
  catch leaks and connection creep), and **replica-rollover during load** (deploy mid-test).
- **Chaos**: kill an API replica, kill a worker mid-run, fail over Redis, saturate the DB pool
  — assert the acceptance criteria from epics 02–05 hold under load, not just in isolation.
- Run the multi-replica suite in CI (or a scheduled pipeline) as the gate for the series.

## Capacity Model Tasks

- From load-test data, document: runs/sec per worker, WS connections per API replica, DB
  connections per replica/worker (validate against epic 05 pooling), Redis throughput headroom,
  and the cost per active workspace-hour (tying execution cost to the hybrid-runtime plan).
- Publish scaling guidance: "for X concurrent teams / Y concurrent runs, run N API replicas,
  M workers, this DB/Redis size."

## SLO / Alerting Tasks

- Define SLOs: API p99 latency, run-start latency (enqueue → first event), stream delivery
  lag, error rate, queue wait time.
- Alerts on SLO burn, queue-depth saturation, DB pool exhaustion, reaper activity spikes
  (mass worker deaths), and Redis unavailability.

## Acceptance Criteria

- A run's full lifecycle is traceable end to end by a single correlation ID across API,
  queue, worker, and stream.
- `/metrics` exposes the fleet + per-tenant signals above; dashboards show load and cost by
  team.
- The load suite demonstrates the target concurrency (e.g. N teams / Y concurrent runs)
  meeting SLOs, **and** the chaos scenarios (replica kill, worker kill, Redis failover, deploy
  under load) pass without data loss or stuck runs.
- A capacity model document exists with concrete scaling guidance and is linked from
  `00-index.md`.
- SLO-based alerting is live.

## Implementation Notes (shipped: correlation, metrics, tracing)

The "Current State" above predates `improvement-epics/07`: request-id middleware
(`core/middleware.py`), the API `/metrics` (`prometheus-fastapi-instrumentator` in `app.main`,
custom series in `core/metrics.py`), the proxy's hand-rolled `/metrics` and the
Prometheus-gated NetworkPolicy already existed. This pass shipped the three observability
items; dashboards/alert rules, the load/chaos harness (**k6** is the chosen tool: single binary,
native WebSockets, thresholds map onto SLOs) and the capacity model are the open follow-ups.

- **Correlation.** Migration `0035_run_correlation` adds nullable `request_id` and
  `traceparent` to `agent_runs` and `command_runs`. `create_run` stamps both from the current
  context (`current_request_id()` reads the structlog contextvars that the HTTP middleware,
  `websocket_context` and the worker bind). The job payload still carries only the run id: the
  worker reads the pair off the row after claiming (`worker._correlation`) and binds
  `request_id` into its log context for the job, unbinding on exit. Agent event frames and
  command status/output frames carry `request_id` on both the live and the backfill path, so a
  frame stays re-derivable from Postgres (gotcha 20); the key is present only when the run has
  one, so the existing exact-frame tests held. A caller-supplied `X-Request-ID` must match
  `[A-Za-z0-9._-]{1,64}` or is regenerated (it lands in logs, rows and frames). WebSocket
  handlers are wrapped in `websocket_context(kind)` (handshake `X-Request-ID`, per-connection log
  context, the `ws_connections{kind}` gauge); the preview proxy is untouched.
- **Metrics, worker side** (`core/metrics.py`, observed where runs execute): queue wait
  (`enqueued_at → claimed_at`), run-start latency (enqueue → first *content* event or output
  chunk; the provider's opening "thinking" status does not count, nor does a command that
  prints nothing), run duration,
  `runs_finished_total{kind,status,reason}`, LLM time-to-first-token (first content event)
  and whole-stream wall time (which includes the tool and sandbox calls the model makes),
  `llm_tokens_total` and `llm_cost_usd_total` by provider/model (from `_record_run_finished`,
  i.e. `core/pricing`), `run_lease_lost_total`, `reaper_runs_ended_total{kind}`,
  `reaper_sweeps_total{result}`. Workers now serve `/metrics` on `METRICS_PORT` (9102) from a
  token-gated thread-per-connection WSGI listener (`app/worker_metrics.py`, 3s request
  timeout and at most 8 connections in flight, so a stalled client never blocks the next
  scrape and a flood never grows the thread count; a flood of slow connections can still
  cost a few seconds of scrapes, which is why the NetworkPolicy, not this code, is the
  boundary) started in the arq startup hook;
  it stays off the event loop, never touches signals (arq owns them), and is best-effort: a
  port that cannot be bound is a warning, not a failed worker. It is stopped last at shutdown,
  off the loop, so the drained runs' final values are still scrapable. It binds IPv4
  (`0.0.0.0`); an IPv6-only cluster needs a host knob. `worker.yaml` exposes
  the port and the worker NetworkPolicy admits only the Prometheus pods on it.
- **Metrics, fleet side** (`services/fleet_metrics.py`): every API replica runs a sampler task
  (`METRICS_SAMPLE_INTERVAL_SECONDS`, default 15) that reads Postgres aggregates and rebuilds
  `fleet_queue_depth{kind}`, `fleet_active_runs{team}`, `fleet_tokens_last_hour{team}` and
  `fleet_cost_usd_last_hour{team}` from scratch, plus `fleet_sample_timestamp_seconds` for
  staleness alerts. A scrape never queries the database. Tenant cardinality is bounded: the
  busiest `METRICS_TENANT_LABEL_LIMIT` (20) teams are labelled by id, the rest are summed into
  `team="other"`, team-less workspaces are `team="none"`, and a team that goes quiet disappears
  rather than lingering at zero. Every replica reports the same numbers: **aggregate with
  `max()`/`avg()`, never `sum()`** (it is in each HELP string). `capacity.waiting_run_counts()`
  is the one queue-depth definition shared by admission, the sampler and (via its test) the
  KEDA trigger. `DbPoolCollector` reports pool occupancy at scrape time from the engine's own
  counters (nothing under `NullPool`/PgBouncer); `ping_redis(role)` times a PING from the API
  sampler and a worker probe loop.
- **Tracing** (`core/tracing.py`): off until `OTEL_EXPORTER_OTLP_ENDPOINT` is set, then an SDK
  provider (`service.name` `enigma-api`/`enigma-worker`, `service.instance.id`,
  `ParentBased(TraceIdRatioBased(OTEL_TRACES_SAMPLE_RATIO=0.1))`, OTLP/HTTP batch exporter
  flushed on shutdown) and FastAPI, SQLAlchemy (`sync_engine`, no SQL commenter), redis, httpx
  and aiohttp-client (the Daytona SDK) instrumentation. It is configured before any middleware
  and before anything imports OpenHands: the Daytona SDK and Laminar each install a global
  provider only when their own env flags are set, and OpenTelemetry keeps the first. Inbound trace
  context is never trusted: the global propagator is inject-only (`InjectOnlyPropagator`), so
  a `traceparent` header from a browser or proxy can neither choose our trace ids nor force
  sampling, and every API server span is a root sampled by our ratio. The run row's
  `traceparent` is captured under that span; the worker extracts it explicitly and executes
  each job inside a `CONSUMER` span (`run_span`) that is its child and binds `trace_id` into the log
  context next to `request_id`, so logs, metrics and spans share ids. API and worker
  NetworkPolicies allow 4318 to `app.kubernetes.io/name: opentelemetry-collector` pods in
  `monitoring`; relabel in the overlay. The instrumentation packages are pinned to `0.60b1`,
  matching the otel 1.39.1 stack `openhands-sdk` already locks.
- **Tests**: `test_correlation.py`, `test_metrics_runs.py`, `test_fleet_metrics.py`,
  `test_worker_metrics.py`, `test_tracing.py`; `test_deployment.py` now requires the worker's
  only ingress to be the scrape port it exposes and the OTLP egress to name collector pods.
