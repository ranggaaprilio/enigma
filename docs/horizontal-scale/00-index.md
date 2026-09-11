# Horizontal Scale — Making Enigma a Multi-Replica Platform

This folder plans the work to take Enigma from a **single stateful backend process** to a
**horizontally scalable, highly available control plane** that many teams can share.

It complements the other planning folders and does **not** replace them:

- `docs/implementation-plan/` — the original MVP build (Epics 0–11).
- `docs/improvement-epics/` — the post-MVP feature backlog. Epic 07 (Observability &
  Hardening) is the closest neighbour; this series **depends on and deepens it**.
- `docs/hybrid-runtime-migration/` — scales the **execution plane** (per-run ephemeral
  Daytona sandboxes, cost/lifecycle). This series scales the **control plane** (the FastAPI
  backend). They are complementary and can land in parallel.

## The Core Problem

Enigma cleanly separates two planes:

- **Execution plane** — Daytona sandboxes. Already scales horizontally by design; the backend
  never runs user code, it only proxies into per-workspace sandboxes
  (`daytona_runtime.py`). The hybrid-runtime folder tunes this further.
- **Control plane** — the FastAPI backend. **This is the bottleneck.** Today it cannot run
  more than one replica because critical state lives in process memory.

The blocking facts, all code-grounded:

| Symptom | Where | Why it blocks multi-replica |
|---|---|---|
| Global in-memory run registry | `agents.py:94` (`agent_runtime = AgentRuntime()`), fields `agents.py:52-55` | `tasks`/`stop_events`/`subscribers`/`providers` are plain dicts scoped to one process. |
| Runs execute in the API process | `agents.py:203`, `commands.py:216`, `speckit.py:336` (`asyncio.create_task(...)`) | Work is tied to the request/WS lifecycle; a deploy kills it. Can't scale API and execution independently. |
| Live streams use in-process queues | `api/agents.py:310` (`agent_runtime.subscribe`), `commands.py:42-43`, `notifications.py:18` | A client's WebSocket must land on the exact replica running the run — impossible to route with N replicas. |
| Stop is process-local | `agents.py:70` (`request_stop`) | On any other replica it silently no-ops. |
| No orphan reconciliation | `main.py:13` (lifespan only closes Daytona) | After a restart, rows stuck in `thinking`/`executing` (`agents.py:158-162`) permanently block new runs in that workspace. |
| Redis is barely used | `services/redis.py:6`, `rate_limit.py` (login only) | The one piece of shared infra that could decouple the above isn't used as a bus or broker. |
| DB pool untuned | `db/session.py:7` (`create_async_engine(url, pool_pre_ping=True)`) | Default 5+10 conns/process; many replicas × WS + writes exhausts Postgres. |
| Insecure shared defaults | `config.py:12-18`, `docker-compose.yml` | Hardcoded `auth_secret_key` / Fernet key / `auth_cookie_secure=False` — unsafe for shared multi-team use. |

Good news: the **stateless pieces are already right** — auth is HMAC-signed tokens with no
server-side session store (`core/security.py:54`), the data model is properly multi-tenant
(`models/project.py`: `ProjectMember` / `TeamMember` with roles, unique constraints, indexes),
and the stack is async end to end. The work is to move **run execution** and **event fan-out**
out of process memory and onto shared infrastructure we already run (Postgres + Redis).

## Target Architecture

```
                         ┌──────────────────────────────┐
        clients ───────► │  Load balancer (no stickiness)│
        (HTTP + WSS)     └───────────────┬───────────────┘
                                         │
                 ┌───────────────────────┼───────────────────────┐
                 ▼                       ▼                        ▼
          ┌────────────┐         ┌────────────┐          ┌────────────┐
          │ API replica│  …N…    │ API replica│          │ API replica│   (stateless)
          └─────┬──────┘         └─────┬──────┘          └─────┬──────┘
                │  enqueue run          │  subscribe(channel)   │
                ▼                       ▼                        ▼
        ┌───────────────┐        ┌──────────────────────────────────────┐
        │  Job queue    │        │  Redis pub/sub  +  Redis (rate/locks) │
        │ (Redis/arq)   │        └──────────────────────────────────────┘
        └──────┬────────┘                         ▲   publish(event)
               ▼                                  │
        ┌────────────┐  …M…   ┌────────────┐      │
        │  Worker     │ ─────► │  Worker     │ ─────┘   (scale independently)
        │ runs agent  │        │ runs agent  │
        └─────┬───────┘        └─────────────┘
              │ proxy exec / fs / PTY
              ▼
        ┌──────────────────────────────┐
        │  Daytona execution plane      │  (per-workspace / per-run sandboxes)
        └──────────────────────────────┘

        Durable state: PostgreSQL (+ PgBouncer, optional read replicas)
```

**Principles**

1. **No process-local state on the request path.** Any replica can serve any request or WS.
2. **Runs are jobs, not tasks.** Execution is decoupled from the HTTP/WS lifecycle and
   survives deploys.
3. **Events flow over a bus.** Streaming is publish/subscribe, not an in-memory queue.
4. **Coordinate through shared infra.** Locks, leases, quotas, and rate limits live in Redis
   or Postgres — never a Python dict.
5. **Everything is 12-factor.** Config from env, no baked secrets, graceful shutdown.

## The Epics

| # | Epic | Priority | Theme |
|---|------|----------|-------|
| 01 | [Stateless Control Plane Foundation](./01-stateless-control-plane.md) | **P1** | Foundation |
| 02 | [Distributed Job & Run Execution](./02-distributed-job-execution.md) | **P1** | Keystone |
| 03 | [Distributed Event Streaming & Pub/Sub](./03-distributed-event-streaming.md) | **P1** | Keystone |
| 04 | [Run Lifecycle Durability & Coordination](./04-run-lifecycle-and-coordination.md) | **P1** | Correctness |
| 05 | [Data Layer Scaling](./05-data-layer-scaling.md) | P1 | Persistence |
| 06 | [Fair-Use: Rate Limits, Quotas & Backpressure](./06-rate-limits-quotas-backpressure.md) | P2 | Multi-tenant safety |
| 07 | [Stateless Preview Proxy & Sandbox Routing](./07-stateless-preview-proxy.md) | P2 | Traffic |
| 08 | [Deployment, Autoscaling & Zero-Downtime Ops](./08-deployment-and-autoscaling.md) | P2 | Operations |
| 09 | [Observability, Load Testing & Capacity Validation](./09-observability-and-load-testing.md) | P2 | Proof |

## Recommended Build Order

The first four epics are the critical path to running two replicas at all; do them in order.
The rest harden and prove it, and can overlap.

1. **01 — Stateless Foundation.** Startup orphan reconciliation, graceful shutdown,
   `/ready`, secret hygiene. Small, unblocks safe rolling deploys **even on one replica**.
2. **02 — Distributed Job Execution.** Move runs to a worker fleet. The keystone.
3. **03 — Distributed Event Streaming.** Move WS fan-out and `stop` to a bus. Pairs with 02.
4. **04 — Lifecycle & Coordination.** Leases/heartbeats, distributed locks, idempotency —
   makes 02+03 correct under crashes and races.
5. **05 — Data Layer.** PgBouncer, pool tuning, `agent_events` partitioning/retention. Needed
   before real concurrency.
6. **06 — Fair-Use** and **07 — Preview Proxy** in parallel.
7. **08 — Deployment/Autoscaling**, then **09 — Load Testing** to validate the SLOs.

## Dependency Notes

- **02 (jobs)** and **03 (streaming)** are two halves of one change: a run runs on a worker
  (02) and its events reach the browser via a bus (03). Land them together behind a flag.
- **04** depends on both — leases and orphan recovery only make sense once runs are jobs.
- **06 (rate limits)** builds directly on `improvement-epics/07` and reuses `rate_limit.py`.
- **07 (preview proxy)** is independent of the run/streaming work and can start any time.
- **08/09** validate the whole thing; **09**'s load tests are the acceptance gate for the
  series.
- This series assumes the **hybrid-runtime-migration** may run concurrently; where a run
  executes (persistent vs ephemeral sandbox) is orthogonal to *where the orchestration code
  runs* (API replica vs worker). Epic 02 is written to work with either.

## Shared Rules For Every Epic

- **No new process-local mutable state on the request path.** If you reach for a
  module-level dict, that's a design smell — put it in Redis or Postgres.
- Keep the API **stateless**: any replica must be able to serve any request or WS with no
  affinity. No sticky sessions.
- A database migration for every schema change (Alembic, async engine).
- Backend tests (`pytest`) for service logic + routes; add **multi-replica tests** (two app
  instances against shared Postgres/Redis) for anything in 02–04.
- Never store secrets in plain text — reuse `WorkspaceSecretCipher` (Fernet). Refuse insecure
  defaults when `environment != "local"`.
- Land behind a feature flag; keep the single-process path working until the distributed path
  is proven, then delete the old path.
- Everything must degrade cleanly: if Redis or a worker is briefly unavailable, the API
  returns a sensible error, not a hang.
