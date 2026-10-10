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

## Implementation Notes (shipped)

Decisions: probes answer only on a **non-preview Host** (no reserved path); a page load that
would 503 gets a self-retrying **"Waking preview…" page** served by the proxy (an iframe cannot
see a status code); the **Redis preview-link cache is deferred**. No load balancer is added to
compose; ≥2 replicas without stickiness is proven at test level (multi-replica deploy is epic 08).

- **Audit result**: the request/WS path was already stateless — every check is HMAC over
  `AUTH_SECRET_KEY` plus Postgres, and the WS bridge tasks and upstream socket end with the
  connection. `tests/test_preview_proxy_scaling.py` keeps it that way: an AST guard fails any
  function in `preview_proxy.py` that writes a module-level binding (or uses `global`), and two
  independently built apps must accept the same capability / alias grant and reject the same
  expired or tampered one. The one leak was `ServiceDiagnosticsRecorder._last_enqueued`, a
  per-key throttle map that never shrank; the consumer loop now prunes keys whose window has
  passed. Per-replica throttling means up to N upserts per key per window, which is harmless
  (they are idempotent). `stop()` now flushes what is still queued (best-effort).
- **Probes** (`create_preview_proxy_app`): `/live` (dependency-free), `/ready` (Postgres only;
  503 `draining` after SIGTERM), `/health` (Postgres + Daytona) and `/metrics` (Prometheus).
  The last two need the `METRICS_TOKEN` bearer, because `/health` makes a live Daytona call and the bare
  host is reachable from the internet. They answer only when the Host is *not* a label under
  `DAYTONA_PREVIEW_PROXY_BASE_URL` (the bare proxy host or the pod IP); on a preview host every
  path, `/health` included, still goes to the previewed app.
- **Drain**: each proxy app owns an `AppLifecycle` (never the API's `lifecycle` singleton, which
  a proxy app's shutdown would otherwise flip for the whole process) wired to
  `install_sigterm_drain`. Shutdown: diagnostics flush → HTTP client → Daytona client →
  `engine.dispose()` (only when the app uses the shared engine). Compose runs uvicorn with
  `--timeout-graceful-shutdown 20` and `stop_grace_period: 30s`; uvicorn closes open WebSockets
  with 1012 and the browser reconnects through another replica.
- **Pooling**: the proxy already built its engine from `engine_options()`; compose now passes
  `DB_PGBOUNCER` so it can sit behind PgBouncer like the API. Pool sizing is shared via `DB_POOL_*`.
- **Metrics** (`core/metrics.py`): `preview_proxy_requests_total{protocol}`,
  `preview_proxy_upstream_errors_total{protocol}` (Daytona error or upstream connect failure;
  error rate = errors / requests), `preview_proxy_active_websockets`. The bearer check is now
  shared with the API as `metrics_token_authorized`.
- **Waking page**: when a page load (`Sec-Fetch-Mode: navigate` or `Accept: text/html`) would be
  a 503 — sandbox waking (Daytona error), upstream unreachable, alias service not running, editor
  not running — the proxy serves a small HTML page that reloads itself with backoff (2s doubling
  to 30s) and sends `Retry-After` and `Cache-Control: no-store`. XHR/fetch keep the JSON 503. The
  editor 503 still sets its grant cookie, and an upstream connect failure on the editor now does
  too (it used to be a cookieless 503). Unauthorised requests still get the uniform 404.
- **Signed-URL refresh** needed no code: `PreviewService._proxy_url` re-mints in the last
  ≤60s of a URL's life and the panel polls every 15s, so the iframe `src` rotates before
  expiry. A test now pins the re-mint.
- **Security**: the client's `X-Forwarded-Host` was forwarded upstream alongside the proxy's own
  (the app saw `evil.example, <real host>`); it is now stripped. Because the raw Host is what
  gets forwarded, `proxy_host_label` now rejects a Host whose port is not numeric
  (`<label>.<base>:x.evil.com`). The metrics bearer is compared as bytes, so a non-ASCII token
  is a 401, not a 500. Client `X-Forwarded-For`/`-Proto`/`Forwarded` still pass through; they
  only reach the tenant's own sandbox. Tests pin that no request path
  (`//host`, `@host`, encoded `?`, `..%2F`, `\\`) changes the upstream host, that a spoofed
  Daytona token never wins, and that upstream redirects are not followed and a foreign
  `Location` is not rewritten.
- **Deferred**: the Redis cache of Daytona preview links. Today every proxied request makes about
  3 Daytona calls (`get`, `refresh_data`/start, `get_preview_link`). A cached link would not wake
  an auto-stopped sandbox, and it would put Daytona tokens in Redis. Revisit with epic 09 load data.
- **Ops**: put the proxy tier behind an L7 LB with **no** sticky sessions, a wildcard cert for
  `*.<DAYTONA_PREVIEW_PROXY_BASE_URL host>`, WebSocket upgrade enabled, an idle timeout ≥ the
  longest expected quiet period of a preview socket (HMR sockets idle for minutes; ≥ 3600s
  is safe), and health checks on `/ready` with a non-preview Host.
