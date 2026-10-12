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

## Implementation Notes (shipped)

Decisions: **Kustomize** (base + `overlays/production`), not Helm; **one server process per
pod** (Kubernetes is the process manager, so every process owns its own drain); workers drain
with a **long grace** so a rollout or scale-in never interrupts a run. Runbook:
[`deploy/README.md`](../../deploy/README.md).

- **Images**: `backend/Dockerfile` is multi-stage. `dev` is the old image (compose builds it
  with `target: dev`, keeping `uv sync` + `--reload`); the default `runtime` target is
  `python:3.12-slim` with the locked venv and the app only, uid 10001, `HOME=/tmp`, and uvicorn
  as PID 1 in exec form (no wrapper, so SIGTERM reaches it). One image serves four commands:
  API, `arq app.worker.WorkerSettings`, the preview proxy, and `alembic upgrade head`. The backend
  runs no local `git`, so none ships. `frontend/Dockerfile` adds `build` (frozen lockfile,
  `VITE_API_BASE_URL` as a **required** build arg: unset, it would inline `""`, which is not
  nullish, so the bundle would call its own origin) and an `nginx-unprivileged:1.30` runtime
  (`frontend/nginx.conf`): `/assets/*` `immutable` (2xx only, so a mid-rollout 404 is never
  cached), `index.html` `no-cache` (the cache-bust), SPA fallback, `/healthz`. Both
  `.dockerignore`s exclude `.env*`.
- **Workloads** (`deploy/k8s/base`): API, worker, preview proxy, frontend Deployments, all
  non-root with a read-only root filesystem (writable `/tmp` emptyDir), `drop: [ALL]`, no
  privilege escalation, no service-account token, RuntimeDefault seccomp; the namespace enforces
  Pod Security `restricted`. The cluster runs `RUN_EXECUTION_MODE=queue`: inline start-up
  reconciliation would end the other API replicas' runs.
- **Probes and drain**: `startupProbe`/`livenessProbe` on `/live` (no dependencies), readiness on
  `/ready`. `SHUTDOWN_DRAIN_DELAY_SECONDS=10` exceeds readiness period × failures (3s × 2), so
  ingress drops the endpoint before the listener closes. No preStop sleep is needed because the app
  delays its own SIGTERM hand-off. `terminationGracePeriodSeconds`: API 45 (10 + 15 run grace +
  slack), proxy 45 (10 + `--timeout-graceful-shutdown 20`), frontend 15 (with a 5s preStop).
- **Worker drain**: arq's `job_completion_wait` is `SHUTDOWN_RUN_GRACE_SECONDS`, which used to be
  15s, after which in-flight runs were swept to `error`. Worker pods now set it to 7500 (≥ the
  agent job timeout) with `terminationGracePeriodSeconds: 7620` and `maxSurge: 100%`: new
  workers take new jobs while old ones finish theirs. Cost: a busy draining pod lingers up to ~2h.
  No code change; it is config.
- **Autoscaling**: API and proxy use HPAs on CPU (min 2, max 10, 5-minute scale-down
  stabilisation, one pod at a time, since a scale-in closes WebSockets). Workers use a KEDA
  `ScaledObject` with a **PostgreSQL** trigger, because Postgres is authoritative for the queue and
  arq's queue is a Redis sorted set that KEDA's Redis scaler cannot count. It counts queued runs
  still active (`agent_runs` thinking/executing + `command_runs` running, `enqueued_at IS NOT
  NULL`) ÷ 8 per worker (`WORKER_MAX_JOBS=10`), min 1, max 20. It authenticates with a separate
  read-only secret. The test imports `ACTIVE_RUN_STATUSES` / `ACTIVE_COMMAND_RUN_STATUS` and the
  table names, so the query cannot drift from `services/capacity.py`. A `fallback` of 4 workers
  covers a broken trigger. Scale-to-zero is an overlay change (`minReplicaCount: 0`). Draining
  workers keep their DB pools, so the connection budget counts workers twice.
- **Edge**: two ingress-nginx Ingresses. Both force TLS, set no affinity and use 3600s read/send
  timeouts for WebSockets. `enigma` (app + API) caps bodies at 12m and retries the next upstream
  on error/502/503 (nginx never retries a non-idempotent request that reached an upstream).
  `enigma-preview` serves previews on a **separate registrable domain** (`*.enigma-preview.example.net`)
  with its own wildcard cert. On the app's site, the `SameSite=Lax` session cookie would reach the
  API from tenant preview pages: CSRF, and hijacking of the terminal/agent WebSockets, which do
  not check `Origin` (an Origin check in those handlers is a recommended follow-up). It
  streams requests and responses, allows 100m bodies, and never retries a 503, because that is the proxy's self-retrying "Waking preview…" page. The bare preview
  host is not exposed. The runbook requires real client IPs at the controller
  (`externalTrafficPolicy: Local` or PROXY protocol), since the login limiter keys on them.
- **Network policy**: default deny. Every egress rule names its destinations. DNS goes only to
  kube-dns. API and worker egress is 5432/6432 and 6379 to **private** addresses only (managed
  datastores over private networking; the overlay narrows them to the provider CIDRs), and 443
  to **public** addresses only (RFC1918, CGNAT and link-local excluded, so the
  cluster API server and cloud metadata are out of SSRF reach). The proxy gets the same minus
  Redis; the frontend gets none; the migrate Job gets DNS + 5432. Ingress comes only from the
  `ingress-nginx` namespace (plus Prometheus pods in `monitoring` for the API and proxy
  `/metrics`). Workers accept no ingress at all.
- **Secrets**: no `Secret` object in git. Pods read `enigma-secrets` (the proxy reads five named keys
  only) and KEDA reads `enigma-keda-postgres`. The migrate Job overrides `DATABASE_URL` with
  `MIGRATION_DATABASE_URL`, because Alembic must bypass PgBouncer.
- **Migrations / expand-contract**: `enigma-migrate` Job, applied first with
  `-l enigma/phase=pre-deploy` (namespace, ConfigMap, egress policy, Job), then the Deployments roll.
  `tests/test_migration_safety.py` AST-checks every migration after `0034`. It fails any of these
  in `upgrade()`, unless the file carries `# contract: <why>`:
  - a drop, rename or `drop_constraint`;
  - a type change or `nullable=False` tightening;
  - a `NOT NULL` column without a `server_default`;
  - a new unique, foreign-key or check constraint, or a unique index;
  - the same done through literal `op.execute` SQL. Non-literal SQL also counts as contract.

  `drop_index` is allowed, since it costs speed, not compatibility.
- **CI**: a new `deploy` job renders the overlay (`kubectl kustomize`), validates it with
  kubeconform (strict, incl. the KEDA CRD schemas), builds both images, and fails on Trivy
  HIGH/CRITICAL fixable vulnerabilities plus secrets in our own layers (`/app/.venv` is skipped
  for secrets, since third-party PEM-marker constants are false positives). Nothing is pushed or
  deployed.
- **Dependency CVEs**: the first scan found 51 HIGH/CRITICAL fixable vulnerabilities in 12
  locked Python packages that predate this epic. They were bumped with `uv lock --upgrade-package`
  (PyJWT, aiohttp, anyio, cryptography, fsspec, httplib2, mcp, pillow, pyasn1, pypdf, starlette,
  urllib3). The frontend base moved from nginx 1.27 to 1.30 for OpenSSL/c-ares.
- **Guards**: `tests/test_deployment.py` pins the properties above without a cluster.
  Rendering and schema validation are CI's job.
- **Not proven here**: "zero failed requests during a rollout under load" and "kill any pod" need
  a live cluster. The configuration for both is in place and tested; Epic 09's load test is the
  proof.
