# Epic 07 — Stateless Preview Proxy & Sandbox Traffic Routing

## Goal

Make the preview proxy horizontally scalable — multiple replicas, no shared in-process state,
any replica able to serve any workspace's preview and WebSocket traffic. Preview is a separate
service today and must scale alongside the API.

## User Value

Live previews and in-preview WebSockets stay reliable as many teams run apps at once, and the
proxy tier can scale independently of the API and workers.

## Current State

- The preview proxy is its own service (`app.preview_proxy:app`, port 8001;
  `docker-compose.yml`, `preview_proxy.py`), proxying HTTP and bridging WebSockets
  (`preview_proxy.py:198-210`) to Daytona sandbox preview URLs.
- Access is via short-lived **signed** URLs (`daytona_runtime.py` `signed_preview_url` /
  proxy TTL settings `daytona_preview_proxy_*` in `config.py:29-30`) — signature-based, so
  no server-side session is inherently required. This is the right foundation.
- It shares the DB engine module (`db/session.py`) and settings; needs the same pooling
  discipline as epic 05.

## Scope

- Confirm and enforce **zero process-local state** in the proxy request/WS path.
- Run the proxy as N replicas behind the load balancer with no stickiness.
- Harden signed-URL verification and per-tenant isolation of proxied traffic.
- Align pooling, health, and shutdown with epics 01 and 05.

## Backend Tasks

- Audit `preview_proxy.py` for any in-memory maps (connection registries, per-target caches);
  move anything required to Redis or make it request-scoped. The upstream `aclose` background
  task (`preview_proxy.py:164`) and the duplex pump (`preview_proxy.py:198-210`) are per-connection
  and fine — verify nothing outlives the connection.
- Ensure signed-URL/token verification depends only on the shared secret + Redis (for TTL/
  revocation if added), so any replica validates identically.
- Add `/health` and `/ready` to the proxy (mirror epic 01) and graceful shutdown that drains
  in-flight proxied streams.
- Apply the epic 05 DB pool tuning to the proxy process (it imports the same engine).
- Optional: a short-TTL Redis cache of resolved sandbox preview endpoints to cut repeated
  Daytona lookups under load — shared across replicas, invalidated on sandbox stop/restart.

## Frontend Tasks

- Handle preview-proxy reconnects (sandbox resumed from auto-stop, replica rollover) with a
  brief "waking preview…" state rather than a hard failure.
- Refresh signed preview URLs before expiry.

## Infra / Ops Tasks

- Load-balance the proxy tier; **no sticky sessions** required (verify).
- Wildcard TLS for the production preview domain (`DAYTONA_PREVIEW_PROXY_BASE_URL` →
  `*.preview.example.com`, per `.env.example`).
- Set connection/idle timeouts appropriate for long-lived preview WebSockets at the LB.

## Security Requirements

- Every proxied request re-verifies the signed URL/token server-side; expired or tampered
  signatures are rejected.
- Proxied traffic is scoped to the authorized workspace's sandbox only — no path/host that
  lets one tenant reach another's sandbox.
- Preserve the existing egress-policy posture (`DAYTONA_NETWORK_*`) — the proxy must not become
  an SSRF pivot into internal networks.

## Acceptance Criteria

- The preview proxy runs as ≥2 replicas with no stickiness; preview HTTP and WebSocket traffic
  works regardless of which replica serves it.
- Killing a proxy replica drops only its in-flight connections; clients reconnect through
  another replica seamlessly.
- Signed-URL verification is identical across replicas; expired/tampered URLs are rejected.
- Proxy connection count and error rate are visible in metrics (epic 09).
