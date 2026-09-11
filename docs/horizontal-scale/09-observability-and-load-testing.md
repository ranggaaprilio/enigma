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
