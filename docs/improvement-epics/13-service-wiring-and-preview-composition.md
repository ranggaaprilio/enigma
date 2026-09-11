# Epic 13 - Service Wiring & Preview Composition

## Goal

Make a multi-service preview actually *work together*. Once a frontend and a backend both run
(Epic 11), the frontend still has to reach the backend **from the user's browser** — where
`localhost:8000` does not exist. This epic defines how services address each other, gives preview
URLs a stable identity, and handles the cross-origin consequences.

## User Value

The user starts both services, opens the web preview, and the app's API calls simply work — no
hand-edited base URLs, no CORS debugging, and no link that dies an hour later.

## User Stories

- As a full-stack developer, my React app calls `/api/...` in the preview and reaches my FastAPI
  service without configuration.
- As a developer whose frontend needs an absolute API URL at build time, Enigma gives me a stable
  one I can put in an environment variable.
- As a developer, I get a clear explanation instead of an opaque CORS error when my API rejects
  the preview origin.
- As a developer, I see which services are healthy — not merely listening — before I open the app.
- As a reviewer, I open one link and get the running app, whatever it is composed of.

## Current State

**Preview hostnames are capability tokens, and they rotate.**

- `create_preview_proxy_url` encodes `(preview_session_id, port, expiry)` into a signed base32
  capability and places it in the **subdomain**: `https://<capability>.<proxy-host>/`
  (`services/preview_capability.py`).
- `PreviewService._proxy_url` (`preview.py:142`) re-issues that capability whenever the remaining
  lifetime drops below the renewal window (`min(60, ttl // 10)` seconds), so **the hostname
  changes** — by default within the hour (`daytona_preview_url_ttl_seconds`,
  `daytona_preview_proxy_ttl_seconds`, `config.py:38-40`).

This has a direct consequence for wiring: an API base URL injected into a frontend at start-up is
**guaranteed to go stale**. Any design that hands a service its sibling's current preview URL must
solve stability too, or the app breaks mid-session.

**The browser cannot reach the sandbox directly.**

- A frontend bundle executes in the user's browser, outside the sandbox. `VITE_API_URL` /
  `NEXT_PUBLIC_API_URL` pointing at `http://localhost:8000` resolves to the *user's own machine*.
  Only the signed proxy origin is reachable.

**The proxy is capable enough for both roles.**

- All HTTP methods and WebSockets are proxied (`preview_proxy.py:30`, `:176`), hop-by-hop headers
  are stripped, and bodies stream — so it can serve an API, not just static assets.

**Nothing exists yet for:** a stable per-service URL, template substitution in environment values,
CORS guidance, or readiness (as opposed to "the port is bound") checks — `_port_is_open`
(`preview.py:237`) is a TCP connect test only.

## Design: Two Wiring Strategies

Both are supported; the UI recommends the first.

### 1. Same-origin proxy (default, recommended)

The frontend's own dev server proxies API paths to `localhost:8000` **inside** the sandbox:

```
browser ──▶ signed preview URL ──▶ sandbox :5173 (Vite)
                                      └── /api/* proxied ──▶ sandbox :8000 (API)
```

- One preview URL, one origin. No CORS, no cookie/`SameSite` problems, nothing to rotate.
- Requires a few lines in the repository's dev-server config (`server.proxy` in Vite, `rewrites`
  in Next.js, `devServer.proxy` in webpack).
- Enigma's job is to **detect the absence of a proxy and say so**, plus offer a copyable snippet —
  not to silently rewrite the user's config.

### 2. Injected sibling URL (for absolute base URLs)

For apps that must know an absolute API origin, expose a **stable alias** per service and inject it
as an environment value:

```
API_BASE_URL = https://<workspace>-<service>.<proxy-host>/     # stable
```

- Requires a **stable alias route** in the preview proxy: an alias (workspace slug + service name)
  resolved server-side to the current session and port, rather than a capability embedded in the
  hostname. Authorisation then moves from "the URL is the secret" to a signed cookie set on first
  visit — a deliberate trade-off, documented below.
- Environment values gain templates: `${ENIGMA_PREVIEW_URL:api}`, resolved by Epic 12's resolver at
  service start.

## Scope

- Stable per-service preview aliases in the proxy (opt-in per workspace).
- Template substitution for service URLs and ports in environment values.
- Proxy-configuration detection with actionable guidance and copyable snippets.
- CORS diagnosis: detect a rejected preview origin and explain it.
- Health/readiness probes so status means "serving", not "bound".
- A composed preview surface: one place listing every service with its role and link.

### Non-Goals

- Running the services themselves — **Epic 11**.
- Storing the variables — **Epic 12** (this epic adds the template syntax it resolves).
- Rewriting a user's repository configuration automatically.
- Sharing previews with people who have no Enigma account (Future Enhancements).

## Backend Tasks

- **Stable alias routing** (`app/preview_proxy.py`, `services/preview_capability.py`)
  - Add an alias host form alongside the capability host: resolve
    `<workspace-slug>-<service-name>.<proxy-host>` to the workspace's active `PreviewSession` and
    port through a database lookup instead of a self-describing token.
  - Because the alias carries no secret, gate it: the first request must present a valid signed
    capability (query parameter, or an authenticated platform session); the proxy then sets a
    short-lived, `HttpOnly`, host-scoped cookie for that alias. Reject requests with neither.
  - Leave the existing capability-host behaviour untouched and default-on; aliases are opt-in per
    workspace so the weaker posture is always an explicit choice.
- **URL templates in environment values** (extends Epic 12's resolver)
  - Support `${ENIGMA_PREVIEW_URL:<service>}`, `${ENIGMA_SERVICE_PORT:<service>}`, and
    `${ENIGMA_PREVIEW_HOST:<service>}`, resolved at service start against current state.
  - A resolution failure (target service not running) is a surfaced, first-class error — never a
    silently empty string, which produces requests to `/undefined/...` and a confusing debug
    session.
  - Also export `ENIGMA_WORKSPACE_ID`, `ENIGMA_BRANCH`, and each sibling's `localhost` port, so
    **server-side** calls between services keep using `localhost` (faster, no proxy hop).
- **Proxy-configuration detection** (extends Epic 10's `run_detection`)
  - Parse `vite.config.*`, `next.config.*`, `webpack.config.*`, `nuxt.config.*`, `angular.json`
    for an existing dev proxy and report present/absent per service.
  - When a workspace has both a web service and an API service and no proxy is configured, raise a
    workspace advisory carrying the exact snippet for that framework.
- **CORS diagnosis**
  - In the proxy, detect an upstream response missing `Access-Control-Allow-Origin` for a
    cross-origin preview request, or a preflight answered with a non-2xx status, and record it as a
    diagnosable event on the service.
  - Never inject CORS headers on the app's behalf — that masks a real misconfiguration and changes
    the application's security behaviour. Report, explain, link to the fix.
- **Health / readiness**
  - Optional `health_path` per service. When set, probe it over HTTP instead of the TCP connect
    test; keep the TCP check as the fallback.
  - Distinguish `starting` / `healthy` / `unhealthy` so the UI can say "listening but returning
    500" rather than a green pill.
- **Composed preview descriptor**
  - `GET /workspaces/{id}/preview/composition` → services, roles, ports, URLs (capability and
    alias), health, and the branch each is serving, so the frontend renders one coherent view.

## Frontend Tasks

- **Composed preview surface**: a primary "Open app" action for the service marked as the entry
  point, plus a secondary list of the remaining services with copyable URLs.
- Per-service health badge, visually distinct from "running".
- **Wiring advisor** panel: when a web + API pair has no dev proxy, show the detected framework, a
  copyable snippet, and one line on why `localhost:8000` cannot work from the browser.
- Show the CORS diagnosis when detected, naming the offending origin and the header the API must
  send.
- The env editor (Epic 12) autocompletes `${ENIGMA_PREVIEW_URL:...}` from the workspace's services
  and warns when a template references a service that does not exist.
- Make rotation visible: label capability URLs "temporary link — rotates hourly" so nobody pastes
  one into a config file.

## Database Tasks

- Migration (reversible): `workspace_services.health_path` and `health_status`
  (CHECK in `unknown`, `starting`, `healthy`, `unhealthy`) if Epic 11 did not add them;
  `custom_commands.is_entry_point` (boolean, at most one true per workspace — partial unique
  index).
- `workspace_preview_aliases` (`workspace_id`, `service_id`, `alias` unique, `enabled`,
  `created_at`) for opt-in stable hostnames.
- `workspaces.preview_alias_enabled` (default false).

## Security Requirements

- The capability-host scheme stays the default. Aliases weaken "the URL is the secret", so they are
  opt-in per workspace and gated by a signed, `HttpOnly`, host-scoped cookie issued only after a
  valid capability is presented.
- Alias names are validated to a strict charset, cannot shadow the proxy's own hostname, and are
  unique across workspaces (unique constraint + reserved-name list).
- Reuse the host-parsing hardening already in `capability_from_host` — reject multi-label
  subdomains, trailing dots, and case tricks — for alias resolution too.
- Never inject `Access-Control-Allow-Origin`, `Access-Control-Allow-Credentials`, or auth headers
  on the application's behalf.
- Template resolution must not become an SSRF or exfiltration primitive: substitute only values
  derived from the workspace's own services, never anything taken from the request.
- Health probes target `127.0.0.1` inside the sandbox only, with a bounded timeout and no redirect
  following.

## Acceptance Criteria

- A Vite + FastAPI workspace with a `server.proxy` entry works end to end through a single preview
  URL, with no CORS configuration.
- A workspace with an entry-point web service and a separate API service shows one "Open app"
  button and lists the API URL separately.
- With aliases enabled, a frontend given `API_BASE_URL=${ENIGMA_PREVIEW_URL:api}` keeps working
  after the underlying capability rotates.
- A web + API pair with no dev proxy produces an advisory naming the framework and the exact
  snippet — before the user hits a failed request.
- An API that rejects the preview origin produces a diagnosis identifying the origin and the
  missing header, instead of an unexplained network error in the console.
- A service that binds its port but returns 500 on `health_path` shows as `unhealthy`, not
  `running`.
- A template referencing a stopped service produces a clear startup error, never an empty value.

## Dependencies

- **Epic 11** (services and multi-port preview) — required.
- **Epic 12** (env resolver) — required for template substitution.
- **Epic 10** for `run_detection` (proxy-config detection extends it) and `is_entry_point`.

## Future Enhancements

- Share a preview with someone who has no Enigma account, via a scoped expiring link (pairs with
  Epic 08).
- Path-based composition (`/` → web, `/api` → API) so one origin serves several services with no
  repository configuration at all.
- Automatic preview environments per pull request (pairs with Epic 02).
- A request log / inspector per preview, using the proxy as the observation point.
