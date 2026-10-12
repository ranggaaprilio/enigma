# Deploying Enigma (Kubernetes)

Production topology for the horizontally scaled control plane (Epic 08). The compose file at
the repo root stays the dev setup; this directory is what runs in a cluster.

```
deploy/k8s/
  base/                     every tier, environment-neutral (example.com hosts)
    config.yaml             ConfigMap enigma-config (non-secret settings)
    api.yaml                Deployment + Service + HPA (CPU) + PDB
    worker.yaml             Deployment + KEDA ScaledObject (queue depth) + TriggerAuthentication
    preview-proxy.yaml      Deployment + Service + HPA (CPU) + PDB
    frontend.yaml           Deployment (nginx static) + Service + PDB
    migrate-job.yaml        Job: alembic upgrade head (pre-deploy)
    ingress.yaml            ingress-nginx: app+API Ingress, separate preview Ingress; TLS, no affinity
    network-policies.yaml   default-deny + per-tier allow lists
  overlays/production/      namespace (Pod Security "restricted") + image pins
```

`backend/tests/test_deployment.py` guards the properties below; CI renders the overlay,
validates it with kubeconform, then builds and Trivy-scans both images.

## Cluster prerequisites

- Kubernetes ≥ 1.27, a CNI that enforces NetworkPolicy (Calico, Cilium, …), metrics-server.
- [KEDA](https://keda.sh) ≥ 2.12 (worker autoscaling).
- ingress-nginx in namespace `ingress-nginx`. The login rate limiter keys on the first
  `X-Forwarded-For` entry. Keep the controller's default `use-forwarded-headers: false`, so a
  client cannot spoof that entry. Also make the controller see **real client IPs**: set
  `externalTrafficPolicy: Local` on its LoadBalancer Service, or enable PROXY protocol on the
  cloud LB and in the controller. Otherwise every user shares the LB's IP and a handful of
  failed logins locks everyone out. Do **not** turn on `use-forwarded-headers` to "fix" this,
  because the entry then becomes client-controlled.
- Two TLS certificates (cert-manager): `enigma-tls` for the app and API hosts, and
  `enigma-preview-tls`, a wildcard for the preview domain.
- A **separate registrable domain for previews** (e.g. `enigma-preview.example.net`, never
  `*.preview.<your app domain>`). Preview pages are tenant code. On the app's own site, the
  `SameSite=Lax` session cookie would ride along on their requests to the API, so a preview
  could CSRF the API or hijack a visitor's terminal/agent WebSocket.
  `test_previews_are_cross_site_from_the_app` enforces this for the base hosts; keep it true
  when an overlay sets real ones.
- Managed **Postgres** (+ optional PgBouncer, epic 05) and **Redis**, outside the cluster.
- Prometheus in namespace `monitoring` if you scrape `/metrics` (bearer `METRICS_TOKEN`): the
  API and proxy on their HTTP ports, workers on `METRICS_PORT` (9102). Optionally an
  OpenTelemetry Collector there too (`OTEL_EXPORTER_OTLP_ENDPOINT`, port 4318); see Epic 09.

## Secrets

Nothing secret is in git or in an image layer. Provide these through your secret store
(External Secrets, Sealed Secrets, or `kubectl create secret generic`):

| Secret | Key | Used by |
|---|---|---|
| `enigma-secrets` | `DATABASE_URL` | API, worker, preview proxy (`postgresql+asyncpg://…`, may be PgBouncer) |
| | `MIGRATION_DATABASE_URL` | migrate Job: **direct** Postgres, never PgBouncer |
| | `REDIS_URL` | API, worker |
| | `AUTH_SECRET_KEY` | all (≥ 32 chars; the proxy verifies what the API signs) |
| | `WORKSPACE_ENV_ENCRYPTION_KEY` | all (Fernet; changing it orphans stored credentials) |
| | `METRICS_TOKEN` | all (required outside `local`) |
| | `DAYTONA_API_KEY` | API, worker, preview proxy |
| | `OPENAI_API_KEY`, `OPENHANDS_LLM_API_KEY` | optional LLM fallbacks (DB settings win) |
| `enigma-keda-postgres` | `connection` | KEDA only: libpq URL of a read-only role with `SELECT` on `agent_runs`, `command_runs` |

The preview proxy mounts only the keys it reads; the API, worker and Job get `enigma-secrets`.

## Release sequence

Every release is **migrate, then roll**. Old and new replicas share the migrated schema for the
length of the rollout, so a migration must be an *expand* step (add nullable or defaulted
columns, tables, indexes). Drops, renames, type changes and `NOT NULL` tightening ship one
release later, once no deployed code reads the old shape, marked `# contract: <why>`.
`backend/tests/test_migration_safety.py` fails the build otherwise.

Build the images from the repo root. The frontend build **fails without** the API origin,
which Vite inlines into the bundle:

```bash
docker build -t <registry>/enigma-backend:<sha> backend
docker build -t <registry>/enigma-frontend:<sha> \
  --build-arg VITE_API_BASE_URL=https://api.<your-host> frontend
```

```bash
cd deploy/k8s/overlays/production
kustomize edit set image enigma-backend=<registry>/enigma-backend:<sha>
kustomize edit set image enigma-frontend=<registry>/enigma-frontend:<sha>

# 1. migrate: the namespace, config, migrate egress policy and Job carry
#    enigma/phase=pre-deploy; a Job is immutable, so delete the previous one first.
kubectl -n enigma delete job enigma-migrate --ignore-not-found
kubectl apply -k . -l enigma/phase=pre-deploy
kubectl -n enigma wait --for=condition=complete job/enigma-migrate --timeout=15m

# 2. roll everything else
kubectl apply -k .
kubectl -n enigma rollout status deploy/enigma-api
kubectl -n enigma rollout status deploy/enigma-preview-proxy
kubectl -n enigma rollout status deploy/enigma-frontend
```

Argo CD / Flux: make the Job a `PreSync` hook (or an earlier sync wave) instead of step 1.

**Rollback**: `kubectl -n enigma rollout undo deploy/<name>`, or re-apply the previous image
tags. Leave the schema alone: an expand migration is compatible with the previous release by
construction.

## What a rollout looks like

- **API / preview proxy**: `maxUnavailable: 0`, `maxSurge: 1`; a new pod takes traffic only
  after `/ready` passes. On SIGTERM the old pod answers `/ready` with 503 for
  `SHUTDOWN_DRAIN_DELAY_SECONDS` (10s; readiness runs every 3s and fails after 2, so the endpoint
  is removed well inside it), then closes its listener, closes WebSockets with 1012 and drains.
  Clients reconnect to another pod and backfill from Postgres (epics 03/07). Liveness is `/live`,
  which checks nothing external, so a database blip never restarts the fleet.
- **Workers**: `maxSurge: 100%`. New workers take new jobs at once. An old worker stops polling
  and waits up to `SHUTDOWN_RUN_GRACE_SECONDS=7500` (≥ the 2h05m agent job timeout) for its runs;
  `terminationGracePeriodSeconds: 7620` covers that plus the shutdown sweep. A rollout or
  scale-in therefore **never interrupts a run**, but a busy old worker pod can linger for up to
  about 2h. A worker that dies anyway is covered by leases and the reaper (epic 04).
- **Frontend**: Vite fingerprints `/assets/*` (served `immutable`); `index.html` is `no-cache`,
  so a reload picks up the release. An asset 404 is never cached, so the brief window where a
  new `index.html` asks an old pod for a new asset heals on the next load. Behind a CDN, keep
  the previous release's assets reachable (e.g. upload `dist/` to object storage) to close it.

## Autoscaling knobs

| Tier | Signal | Bounds | Notes |
|---|---|---|---|
| API | CPU 70% of request | 2–10 | scale-down stabilised 5 min, 1 pod / 2 min (each scale-in closes WebSockets) |
| Preview proxy | CPU 70% | 2–10 | switch to `preview_proxy_active_websockets` once a Prometheus adapter exists |
| Workers | Postgres: active queued runs ÷ 8 | 1–20 | `WORKER_MAX_JOBS=10`; scale-down stabilised 10 min, 1 pod / 5 min |

Worker scale-to-zero: set `minReplicaCount: 0` in an overlay if a cold start (image pull + boot)
on the first run after idle is acceptable; KEDA's activation threshold wakes the fleet on the
first enqueued run. If the trigger itself fails (missing `enigma-keda-postgres`, revoked
`SELECT`), KEDA's `fallback` holds 4 workers. Alert on `keda_scaler_errors_total` so this does
not go unnoticed.

A fresh install starts the API and proxy at one pod each until the HPA's first evaluation
(seconds) raises them to `minReplicas: 2`.

Size Postgres connections for the maxima with the default pools: `api_max × 15 + proxy_max × 5
+ 2 × worker_max × 5`. Workers count **twice**: during a rollout or scale-in, old workers keep
their pools for up to ~2h while they drain, next to their replacements. Or put PgBouncer in
front (`DB_PGBOUNCER=true`).

## Resilience checklist

- Two replicas minimum for every served tier, spread across nodes, `PodDisruptionBudget`
  `maxUnavailable: 1`: losing any single pod leaves the tier serving.
- Every container: non-root, read-only root filesystem (writable `/tmp` emptyDir), all
  capabilities dropped, no privilege escalation, no service-account token. The namespace
  enforces Pod Security `restricted`.
- NetworkPolicy: default deny. API and worker egress goes only to kube-dns, Postgres
  (5432/6432) and Redis (6379) on **private** addresses only, and **public** HTTPS (Daytona,
  LLM, GitHub). Private, CGNAT and
  link-local ranges are excluded, so the cluster API server and cloud metadata are unreachable.
  A self-hosted Daytona or LLM on a private IP needs an overlay rule. The proxy has no Redis;
  `/metrics` is open only to `app.kubernetes.io/name: prometheus` pods in `monitoring`, and
  that scrape (port 9102, bearer `METRICS_TOKEN`) is the only ingress workers accept. Every
  egress rule names its destinations. Narrow the Postgres/Redis `to:` from "any private
  address" to your providers' CIDRs in the overlay.
- Edge: two Ingresses. `enigma` (app + API) caps uploads at 12m and retries 502/503 on another
  pod. `enigma-preview` streams requests and responses, allows 100m bodies, and never retries a
  503 (that is the proxy's self-retrying "Waking preview…" page).
- The live proof (a rolling deploy under load with zero failed requests and no interrupted
  runs) is Epic 09's load test.
