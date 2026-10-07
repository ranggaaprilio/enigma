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

## Implementation Notes (shipped)

Landed in one pass; migration head is now `0027_service_wiring`.

### Corrections to this document, found while implementing

1. **`health_path` lives on `custom_commands`, not `workspace_services` as this document
   originally specified.** `workspace_services.id` is not a stable identity —
   `WorkspaceServiceManager._sync_definitions` deletes and re-creates the idle built-in row
   whenever service commands appear or disappear (the same reason `preview_sessions.service_id`
   is `ON DELETE SET NULL`) — and there is no PATCH on the services API to configure it there.
   `custom_commands` already owns every other per-service user setting, its own CRUD, form, and
   import/export, so `health_path`/`is_entry_point` live there instead; only the runtime
   `health_status`/`health_checked_at` live on the service row. Consequence: the built-in mise
   row (no backing `CustomCommand`) has no `health_path` and always falls back to a TCP check.
2. **Preview aliases key on `custom_command_id` (nullable; `NULL` = the built-in row), not on
   `workspace_services.id`**, for the same identity reason as above — an alias bound to a service
   row's id would silently detach the moment `_sync_definitions` recreated that row.
3. **CORS preflights bypass the alias cookie gate entirely.** Browsers never send a `Cookie` and
   never append a query string to an `OPTIONS` preflight, so gating it the way this document
   originally implied would have made every alias API call fail before the real request was ever
   sent. `OPTIONS` + `Access-Control-Request-Method` is detected up front and forwarded verbatim,
   with no `Set-Cookie` issued.
4. **The alias grant cookie is `SameSite=None; Secure`, not `Lax`.** An alias exists specifically
   so a page on one preview origin can call another; `Lax` cookies are not sent on a cross-site
   fetch/XHR/WS at all. This means aliases cannot work over plain `http://` on a non-loopback
   host — `enforce_secure_defaults` now refuses to boot outside the `local` environment if
   `daytona_preview_proxy_base_url` is `http://` on a non-loopback host.
5. **A separate "priming" step was required and is not mentioned in the original design.**
   Cookies are host-scoped, so serving the `web` alias cannot set the `api` alias's cookie — the
   frontend's first cross-origin call would have no cookie and no capability to fall back to. The
   composition response exposes a per-service `alias_prime_url`
   (`https://<alias>.<proxy-host>/?__enigma_cap=<cap>`); the preview pane fetches every
   `alias_prime_url` (`credentials: "include"`, `mode: "no-cors"`) before opening the app. Without
   this, cross-service aliasing is broken end to end. The injected
   `${ENIGMA_PREVIEW_URL:x}` value is always the bare alias URL — it never carries a capability.
6. **Health is a wholly separate signal from `status`; `status` never becomes `"unhealthy"`.**
   Folding health into `WORKSPACE_SERVICE_STATUSES` would have rippled into
   `WORKSPACE_SERVICE_ACTIVE_STATUSES`, `_check_limit`, `_check_port_conflict`,
   `affected_running_services`, `_reconcile_previews`, and the frontend's status union. A
   service that answers `health_path` with a 500 stays `status="running"` — its preview URL and
   `_should_relaunch`'s no-restart-loop guard are both keyed on `status` alone — with a distinct
   `health_status` the UI renders as a second, smaller badge next to (never instead of) the
   status pill.
7. **`${ENIGMA_*}` substitution happens only in `prepare_launch_env`, never inside `resolve()`.**
   `resolve()` is also called from `terminal.py` and `commands.py`'s non-launch paths;
   substituting there would have silently changed what an interactive terminal or an
   already-running command saw, and would have put the "unresolved template is a hard error"
   path behind a function two other callers cannot surface as a launch failure.
8. **Advisories (`dev_proxy_missing`, `entry_point_unset`, `template_unresolved`) are computed
   on every `GET .../preview/composition` call, never stored.** A stored advisory goes stale the
   moment a file is edited, a worktree is switched, or the registry changes, with no hook to
   invalidate it. `service_diagnostics` (CORS events) is the only thing this epic persists;
   everything else in `advisories[]` is a pure derivation.
9. **`GET .../preview/composition` costs zero sandbox round-trips by default.** The existing
   `PreviewPanel` poll loop runs every 4 seconds; the epic's design did not account for this, so
   proxy-config detection (a sandbox exec) sits behind `?check_proxy=true`, called only on first
   mount per (workspace, worktree) and on an explicit "Re-check" — never from the poll loop.

### Architecture

- **Host classification is by shape, decided before any DB access, with no fallback between
  paths.** A capability is exactly 63 chars of `[a-z2-7]` (base32 of a 23-byte payload + 16-byte
  HMAC); an alias matches `^[a-z0-9][a-z0-9-]{1,60}[a-z0-9]$` and must contain a `-`. Since `-`
  is outside the base32 alphabet and the alias length cap is 62, the two predicates are mutually
  exclusive by construction. `preview_capability.py`'s existing host-parsing hardening (strip
  `:port`, `rstrip('.')`, lowercase, enforce the `.<proxy-host>` suffix) was extracted into
  `proxy_host_label()`, with `capability_from_host` reduced to a one-line wrapper — zero
  behaviour change, pinned by running the untouched `test_preview_capability.py` unmodified.
- **The proxy's dispatch rule only routes to the new alias path when the host label is
  alias-shaped** (`is_alias_label`); everything else — including any capability, and any
  non-shape-compliant string — falls through to the exact same `resolver.resolve(host)` call the
  proxy used before this epic, with no new pre-validation ahead of it. This was necessary to keep
  `test_preview_proxy.py` passing byte-for-byte unmodified, the deliberate regression guard for
  the capability path.
- **Alias grants are domain-separated from platform session tokens**, not reused from
  `create_session_token`/`verify_session_token`: `HMAC-SHA256(auth_secret_key,
  b"enigma-preview-alias-grant-v1|" + payload)` over `{"alias","exp","iat","typ","v","ws"}`.
  Binding `alias` makes a grant worthless at another alias; binding `ws` invalidates old grants
  if a freed alias string is later re-minted for a different workspace.
- **Alias minting happens on enable, not on first service start**, and is idempotent
  (`ensure_for_workspace`) — otherwise a template referencing a not-yet-started service would
  have no alias to resolve against at env-build time. Names are
  `slugify(f"{workspace.slug}-{name}-{workspace.id.hex[:6]}")`, always workspace-discriminated
  (per this epic's design decision), with collision retry inside a `session.begin_nested()`
  savepoint rather than select-then-insert. Aliases are immutable — renaming a command never
  changes its alias, since the whole point is stability; an explicit "regenerate" mints a new one.
- **`ProbeRow`/`ProbeResult` gained a 5th field (`health_path`/`health`) on the same
  single-round-trip probe script**, parsed tolerantly (5 fields → with health, 4 → `"unknown"`),
  using `http.client.HTTPConnection` rather than `urllib` specifically to avoid following a
  redirect. An unhealthy service inside its start grace window reports `starting`, not
  `unhealthy`, mirroring the existing TCP-bind grace logic (`_within_start_grace`, factored out
  of `_grace_status` so the two cannot drift).
- **`env_templates.py` is a pure module** (no DB, no sandbox) resolving `${ENIGMA_PREVIEW_URL:x}`,
  `${ENIGMA_PREVIEW_HOST:x}`, `${ENIGMA_SERVICE_PORT:x}`, `${ENIGMA_SERVICE_URL:x}` (added beyond
  the original design — server-side same-sandbox calls need a `http://127.0.0.1:<port>` form that
  isn't the browser-facing alias/capability URL), `${ENIGMA_WORKSPACE_ID}`, `${ENIGMA_BRANCH}`.
  Only `ENIGMA_`-prefixed names are touched — `${DATABASE_URL}` and bare `$HOME` pass through
  untouched — and an unknown `ENIGMA_*` function is always a hard error, never silently passed
  through. Launch sites and command runs resolve strictly (a raise becomes `status="error"`); a
  terminal session resolves non-strictly (unresolved keys are omitted, with one notice pushed
  before the PTY attaches) since a terminal is where you go to debug a broken template.
- **CORS diagnosis never injects a CORS header itself** — `_maybe_record_cors_diagnosis` is a
  pure read of the already-built upstream response, and `test_preview_proxy_cors.py` pins that a
  diagnosed response's `raw_headers` are byte-identical to what upstream sent. Detections write
  through a throttled, non-blocking recorder (`ServiceDiagnosticsRecorder`): `record()` is sync,
  O(1) and never raises, backed by a bounded `asyncio.Queue` that silently drops under
  `QueueFull` rather than apply backpressure to the hot path; a single consumer coroutine batches
  and upserts (`ON CONFLICT ... DO UPDATE`, bumping `occurrences`) once per flush interval.

### Frontend

No new route or tab — everything lands in the existing `preview` view. New
`components/preview/` components (`HealthBadge`, `PreviewUrlLabel`, `EntryPointCard`,
`WiringAdvisor`, `CorsDiagnosis`, `CopyButton`) are composed into `PreviewPanel`/`ServiceRow`
without a second poll timer: composition state follows the existing service-run polling via a
derived key, with `check_proxy=true` only on first fetch per (workspace, worktree) and on
"Re-check". `CustomCommandsPanel`'s command form gained a health-check-path field and an
"Entry point" checkbox, shown only while "Long-running service" is checked (the database CHECK
rejects `is_entry_point` on a non-service row, so the form never sends one for a task).
`EnvVarEditor` gained `EnvVarEditor/TemplateSuggestions.tsx`: a `<datalist>` (via a new optional
`listId` prop on `ui/TextInput.tsx`, wired to the single-value "Replace value" input) plus
click-to-insert chips for the bulk-edit `<textarea>`, which cannot carry a `list` attribute at
all. The suggestion is a plausible starting guess (`${ENIGMA_PREVIEW_URL:<slugified-name>}`), not
a reproduction of the backend's full selector-derivation chain — the backend's write-time check
is what actually validates a selector and surfaces an `unknown_service`/`ambiguous_service`
warning in the existing bullet list if the guess doesn't match.

### Verification

`ruff` clean; **852 backend tests pass** (up from 618 before this epic — net new: capability/alias
classification, alias grant security, CORS diagnosis, the diagnostics recorder, env-template
substitution, proxy-config detection, health probing, and the composition endpoint).
`test_settings.py::test_command_deny_dangerous_defaults_to_true` fails only as part of the full
suite (pre-existing order-dependent env-var pollution, unrelated to this epic) and passes in
isolation. Migration `0027_service_wiring` verified reversible (`upgrade → downgrade -1 →
upgrade`) against local Postgres; a text-based parity test asserts every constraint/index name in
the new models' `__table_args__` appears literally in the migration, since `Base` has no
SQLAlchemy naming convention and every name here is hand-written. Frontend: `tsc -b` and
`vite build` succeed, eslint has the same 4 pre-existing warnings plus one new one
(`TemplateSuggestions.tsx` exports both a component and a pure helper from one file, the same
accepted pattern already used by three other files in this codebase), **232 tests pass** (1
pre-existing skip, up from 205).

Not covered by automated tests, and still worth a manual pass against a live Daytona sandbox
(the acceptance criteria above are where this epic is actually proven): a Vite + FastAPI
workspace with a `server.proxy` entry working end to end through one preview URL with no CORS
configuration; an alias continuing to resolve after its underlying capability rotates (shorten
`daytona_preview_proxy_ttl_seconds` to force it quickly), including the priming step making the
first cross-origin call succeed; a missing dev proxy producing the advisory and snippet before
any failed request; a CORS-disabled API producing a diagnosis naming the origin and header with
no CORS header actually injected by the proxy; a `health_path` returning 500 showing `unhealthy`
without being restart-looped and without losing its preview URL; and a template referencing a
stopped service producing a clear `last_error` rather than an empty value.
