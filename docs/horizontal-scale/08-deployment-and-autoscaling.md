# Epic 08 — Deployment, Autoscaling & Zero-Downtime Ops

## Goal

Turn the now-stateless components (API, workers, preview proxy) into an orchestrated,
autoscaling deployment with rolling/zero-downtime releases. This is where horizontal scale
becomes real and operational.

## User Value

The platform scales up under load and down when idle without manual intervention, and deploys
ship with no downtime and no interrupted runs.

## Current State

- Deployment today is `docker-compose` (`docker-compose.yml`) with **single instances** of
  backend, preview-proxy, postgres, redis, frontend — a dev/POC topology, not a scalable one.
- Backend runs with `--reload` and a bind-mounted volume (dev mode).
- No orchestrator, no autoscaling, no health-gated rollout, no separate worker process type
  (that arrives in epic 02).

## Scope

- Production container images and an orchestrator deployment (Kubernetes assumed; equivalent
  ECS/Nomad acceptable).
- Separate scalable deployments for **API**, **workers**, and **preview proxy**.
- Autoscaling driven by the right signals (CPU/RPS for API, **queue depth** for workers).
- Zero-downtime rolling deploys with health gating and graceful drain.

## Backend / Packaging Tasks

- Production Dockerfiles: no `--reload`, no source bind-mount; run under a production ASGI
  setup (uvicorn workers behind the process manager, or gunicorn+uvicorn workers) sized per
  container.
- A distinct **worker** image/command (`app.worker`, epic 02) deployable and scalable on its
  own.
- Wire `/health` (liveness) and `/ready` (readiness, epic 01) into the orchestrator probes;
  readiness gates traffic during startup and drain.
- Ensure `SIGTERM` → graceful drain (epic 01) is honoured within the orchestrator's
  termination grace period; set `terminationGracePeriodSeconds` to cover WS/run drain.

## Infra / Ops Tasks

- **Deployments**: API (stateless, HPA on CPU/RPS), workers (HPA/KEDA on Redis queue depth),
  preview proxy (HPA on connections/CPU). Postgres + Redis as managed/HA services (epic 05).
- **Load balancer / ingress**: HTTP + WSS, no sticky sessions, timeouts tuned for long-lived
  agent/terminal/preview WebSockets.
- **Rolling deploys**: `maxUnavailable=0` style rollout; new pods must pass `/ready` before old
  pods drain. Confirm in-flight runs survive (they're jobs on workers now — epic 02) and
  in-flight streams reconnect (epic 03).
- **Migrations in the release pipeline**: run Alembic `upgrade head` as a pre-deploy step
  (`README` migration step), with backward-compatible migrations so old and new replicas
  coexist during a rollout (expand/contract pattern).
- **Autoscaling policies**: min/max replicas, scale-up/down thresholds and stabilization
  windows; scale workers to zero-ish when idle if cold-start budget allows.
- Secrets via the orchestrator's secret store (not env files, not compose) — enforces epic 01's
  secret hygiene.

## Frontend Tasks

- Serve the built frontend from a CDN/static host (not the Vite dev server in
  `docker-compose.yml`); cache-bust on release.
- Ensure the client's reconnect logic (epics 03/07) makes rolling deploys invisible to users.

## Security Requirements

- Least-privilege network policies between tiers (API/worker → Postgres/Redis/Daytona only).
- Images scanned; no secrets baked into layers (contrast current `docker-compose.yml`).
- HTTPS/WSS enforced at the edge; secure cookies (epic 01).

## Acceptance Criteria

- API, workers, and preview proxy each scale horizontally via autoscaling policies; workers
  scale on queue depth.
- A rolling deploy completes with **zero failed requests**, no interrupted runs, and streams
  that reconnect automatically.
- Migrations apply cleanly with old + new replicas briefly coexisting (expand/contract
  verified).
- Killing any single pod of any tier causes no user-visible outage.
