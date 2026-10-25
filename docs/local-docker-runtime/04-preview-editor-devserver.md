# Epic 04 - Preview, Editor & Dev Server on Docker

## Goal

Make the human-facing surfaces work on the Docker provider: app preview URLs through the
existing capability proxy, the code-server editor, dev-server start/stop/logs, and port
detection. The key design move: sandbox containers join the compose network, so the
preview proxy dials `container:port` directly — no published host ports, no Daytona
token semantics.

## User Value

The parts a human actually sees: click "Run App" on a local-Docker workspace and get a
working preview URL; open the in-browser VS Code editor; watch dev-server logs — all
identical to the Daytona experience.

## Current State

- **Preview chain (provider-friendly and, since horizontal-scale Epic 07, stateless)**:
  `services/preview_capability.py` mints an HMAC capability (`PAYLOAD_FORMAT =
  "!B16sHI"` over `preview_session_id | port | expires_at`) encoded as a subdomain of
  `DAYTONA_PREVIEW_PROXY_BASE_URL`; `services/preview_alias.py` and
  `services/editor_alias.py` add stable per-workspace preview/editor hosts gated by a
  grant cookie (`WorkspacePreviewAlias`, migrations `0027`/`0029`). `preview_proxy.py`
  (`create_preview_proxy_app`, port 8001) resolves all three host kinds
  (`PreviewProxyResolver.resolve` 152, `resolve_alias` 183, `resolve_editor_alias` 241),
  loads `PreviewSession`/`WorkspaceService`/`EditorSession` + `Workspace` from Postgres,
  then per request calls `runtime.preview_link(workspace, port)` (1056, 1070, 1121) and
  forwards HTTP (streamed, hop-by-hop headers stripped, client `X-Forwarded-Host`
  dropped) or bridges WebSockets (`proxy_websocket` 615), attaching
  `X-Daytona-Preview-Token` / `X-Daytona-Skip-Preview-Warning` (1095, 1123).
- Guard rails already on the proxy: no module-level state (AST test,
  `tests/test_preview_proxy_scaling.py:218`), its own `AppLifecycle`
  (`app.state.lifecycle`), `/live`/`/ready`/`/health`/`/metrics` only on a non-preview
  Host, `/health` calls `proxy_runtime.health()` (411) and the runtime is injectable
  (`create_preview_proxy_app(runtime=...)`, 305) — the four `test_preview_proxy*.py`
  files all pass a `FakeRuntime`.
- Each resolver currently **requires `workspace.daytona_sandbox_id`** (173, 226, 269) —
  after Epic 01 this is the neutral `sandbox_id`, satisfied by Docker container ids too.
- **Editor**: `services/editor.py` launches code-server on `0.0.0.0:13337 --auth none`
  via exec (install fallback 227, pkill 159) and exposes it on the proxy's stable
  `editor-<alias>.<proxy-host>` origin authorised by the grant cookie — **not** a signed
  Daytona URL any more (docstring at 46–55). `editor_sessions` rows store
  `bind_host="daytona"` (124), `bind_port`, `url`, `token_expires_at`.
  `signed_preview_url` has no callers and was dropped from the interface in Epic 01.
- **Dev server / services**: the "Run App" path is `services/workspace_services.py`
  (`WorkspaceServiceManager.start` 153, `_launch` 557; N tracked `workspace_services`
  rows, each with its own log stream and preview session — improvement Epic 11).
  `services/dev_server.py` is the single-service path that delegates to it when a
  registry entry exists. Both are exec-only (mise setup/run/status/logs/pkill,
  `env=` injected from workspace env vars) plus `fs.get_file_info` config probes
  (`dev_server.py:462,470`, `workspace_services.py:1120,1129`) covered by Epic 03.
- **Port detection**: `services/preview.py:316` / `services/sandbox_probe.py` run a
  python socket probe over candidate ports (3000, 5173, 8000, 8080) via exec;
  `services/run_detection.py` infers ports from manifests — provider-neutral already.
- Docker workspaces after Epic 03: exec + fs + PTY all work, but `preview_link` raises
  `NotImplementedError`.

## Scope

**In scope**

- `DockerRuntime.preview_link` backed by container-network addressing.
- Preview proxy reachability to sandbox containers (compose network membership).
- Editor (code-server) and dev-server / multi-service flows validated end-to-end on
  Docker.
- `editor_sessions.bind_host` values for the Docker provider.

**Out of scope**

- Any change to the capability/alias formats, TTLs, grant cookies or subdomain scheme
  (shared with Daytona and owned by `horizontal-scale/07`, which has shipped).
- Any module-level state in `preview_proxy.py` — the AST guard forbids it.
- TLS/host exposure of the proxy itself (deployment concern, Epic 06 docs).
- Idle lifecycle of preview sessions (Epic 05).

## Backend Tasks

1. **`DockerRuntime.preview_link(workspace, port)`**:
   - Inspect the container, take its address on `docker_sandbox_network` — prefer the
     container **name**, which Docker's embedded DNS resolves on user-defined networks —
     and return `RuntimePreviewLink(url=f"http://{container_name}:{port}", token="")`.
   - Empty token ⇒ the proxy's token header becomes a no-op; rename the header constant
     to a provider-neutral name (`X-Sandbox-Preview-Token`) while keeping the Daytona
     value on the Daytona path.
2. **Config naming**: rename `daytona_preview_proxy_base_url` / `_ttl_seconds` to neutral
   `preview_proxy_base_url` / `preview_proxy_ttl_seconds` with back-compat env aliases
   (`AliasChoices`); keep the https-unless-loopback validator (`core/config.py:~320`).
   The editor needs nothing provider-specific: its URL is the alias host, minted by
   `editor_alias.py` from this base URL.
3. **Network reachability**: `docker-compose.yml` — `preview-proxy`, `backend` and
   `worker` join `enigma-sandboxes` (declared in Epic 02) in addition to the default
   network. Document the non-compose deployment requirement (the proxy must share a
   Docker network with sandboxes).
4. **Proxy resolver**: replace the three `daytona_sandbox_id` requirements (173, 226,
   269) with the neutral `sandbox_id` (Epic 01 may already have done this); make the
   proxy's `/health` runtime probe (411) factory-aware so a Docker-only install reports
   the right provider; no other resolver changes — capability/alias decode, DB lookup,
   streaming, and WS bridging are provider-blind.
5. **Editor on Docker**: reuse `editor.py` exec launch verbatim (code-server is baked
   into the image at the same path); `editor_sessions.bind_host` gets `"docker"`; the
   session URL is the existing `editor-<alias>` origin. Verify pkill/relaunch, the
   grant-cookie flow and `token_expires_at` refresh behave the same.
6. **Dev server / services on Docker**: run `WorkspaceServiceManager` start/stop/
   restart/logs and the single-service `dev_server.py` flows end-to-end, including
   `env=` injection of workspace variables; fix only environment drift (PATH/home), not
   logic. Log streaming uses `stream_command` from Epic 02; service log WebSockets must
   keep obeying the "never hold a session across `accept()`" rule
   (`tests/test_data_layer.py`).
7. **WebSocket preview**: verify HMR (Vite :5173) works through the proxy → container
   path (the WS bridge at `preview_proxy.py:615` should carry it; test it explicitly).
8. **Tests**: unit tests for `preview_link` (name-based URL, empty token) and resolver
   neutrality, written the way the existing `test_preview_proxy*.py` files inject a
   `FakeRuntime`; integration (`DOCKER_INTEGRATION_TESTS=1`): start a static server in a
   container, mint a capability, fetch through a locally-instantiated proxy app, assert
   body + a WS echo; editor launch → alias URL 200s with the grant cookie; service
   start/logs/stop for a Vite fixture plus a two-service (api + web) fixture.

## Frontend Tasks

- None functionally — Preview panel, "Run App", and the editor open URLs the backend
  hands them. One check: any UI copy that says "Daytona" around preview/editor states
  becomes provider-neutral ("sandbox"). (Full provider labelling lands in Epic 06.)

## Database Tasks

- None required. `editor_sessions.bind_host` is a plain string column — `"docker"` needs
  no migration. The `DAYTONA_PREVIEW_PROXY_*` env names stay accepted as aliases
  (task 2) until Epic 06's cleanup.

## Security Requirements

- Sandbox containers must be reachable **only** from the proxy/backend on the shared
  bridge network — never publish sandbox ports to the host (`ports:` stays empty on
  sandbox containers); the capability HMAC remains the sole authorization gate, with the
  same TTL enforcement as today.
- Keep hop-by-hop header stripping and the streamed-body behaviour of the proxy
  unchanged; no new response buffering.
- The proxy must not follow redirects to non-sandbox hosts; preview targets are always
  the resolved container address (assert host == container name/IP before dialing).
- code-server keeps its existing auth posture behind the capability URL; no
  `--auth none` exposure beyond the proxied path.

## Acceptance Criteria

- On a Docker workspace: "Run App" detects the port, creates a `workspace_services`
  row + preview session, and both the capability URL and the stable alias URL serve
  the app — including Vite HMR over WebSocket. A two-service fixture (api + web)
  previews both ports.
- The editor opens (code-server via the `editor-<alias>` origin + grant cookie), edits
  a file, and the change is visible via the Files panel — on Docker.
- Service start/status/restart/logs/stop work; logs stream live over the existing WS.
- The same flows on a Daytona workspace are unchanged (regression suite + one manual
  smoke: the preview URL still carries the real Daytona token).
- No sandbox container has host-published ports (`docker inspect` assertion in the
  integration suite).

## Dependencies

- **Epic 03** (fs for dev-server config detection; exec parity) — hard dependency.
- **`horizontal-scale/07` (Stateless Preview Proxy) has shipped**: implement against
  its resolver and keep its guarantees (no module-level state, per-app `AppLifecycle`,
  host-gated ops endpoints, HMAC + Postgres as the only sources of truth). This epic
  only changes *what `preview_link` returns* for one provider.

## Future Enhancements

- Per-preview-session network aliases (unique DNS name per session) if container name
  collisions ever matter.
- Local HTTPS (mkcert) profile for the proxy in self-host setups.
- Port auto-detection beyond the fixed candidate list (parse dev-server stdout).
