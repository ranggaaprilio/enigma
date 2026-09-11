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

- **Preview chain (provider-friendly by design)**: `services/preview_capability.py`
  mints an HMAC capability (`PAYLOAD_FORMAT = "!B16sHI"` over
  `preview_session_id | port | expires_at`) encoded as a subdomain of
  `DAYTONA_PREVIEW_PROXY_BASE_URL`. `preview_proxy.py` (`create_preview_proxy_app`,
  port 8001) resolves it (`PreviewProxyResolver.resolve`, `:70`), loads
  `PreviewSession` + `Workspace` from Postgres, then per request calls
  `runtime.preview_link(workspace, port)` (`:247,263,317`) and forwards HTTP (streamed,
  hop-by-hop headers stripped) or bridges WebSockets (`:198-210`), attaching
  `X-Daytona-Preview-Token` / `X-Daytona-Skip-Preview-Warning`.
- The resolver currently **requires `workspace.daytona_sandbox_id`** — after Epic 01
  this is the neutral `sandbox_id`, satisfied by Docker container ids too.
- **Editor**: `services/editor.py` launches code-server on port 13337 via exec
  (`:56,73,126,147` — launch/pkill) and exposes it with `signed_preview_url`.
  `editor_sessions` rows store `bind_host="daytona"`, `bind_port`, `url`,
  `token_expires_at`.
- **Dev server**: `services/dev_server.py` is exec-only (mise setup/run/status/logs/
  pkill; `:128-536`) plus two `fs.get_file_info` calls (`:549,559`) covered by Epic 03.
- **Port detection**: `services/preview.py:243` runs a python socket probe over
  candidate ports (3000, 5173, 8000, 8080) via exec — provider-neutral already.
- Docker workspaces after Epic 03: exec + fs + PTY all work, but `preview_link` /
  `signed_preview_url` raise `NotImplementedError`.

## Scope

**In scope**

- `DockerRuntime.preview_link` / `signed_preview_url` backed by container-network
  addressing.
- Preview proxy reachability to sandbox containers (compose network membership).
- Editor (code-server) and dev-server flows validated end-to-end on Docker.
- `editor_sessions.bind_host` values for the Docker provider.

**Out of scope**

- Any change to the capability format, TTLs, or subdomain scheme (shared with Daytona,
  and with `horizontal-scale/07` which redesigns proxy statelessness — see Dependencies).
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
2. **`DockerRuntime.signed_preview_url(workspace, port)`**: route through the existing
   capability proxy — mint/reuse a `PreviewSession` and return the capability subdomain
   URL (the same helper the preview API uses), so editor links look identical across
   providers. Rename `daytona_preview_proxy_base_url` config to a neutral
   `preview_proxy_base_url` with a back-compat env alias.
3. **Network reachability**: `docker-compose.yml` — `preview-proxy` (and backend, for
   health checks) joins `enigma-sandboxes` in addition to the default network. Document
   the non-compose deployment requirement (the proxy must share a Docker network with
   sandboxes).
4. **Proxy resolver**: replace the `daytona_sandbox_id` requirement with the neutral
   `sandbox_id`; no other resolver changes — capability decode, DB lookup, streaming,
   and WS bridging are provider-blind.
5. **Editor on Docker**: reuse `editor.py` exec launch verbatim (code-server is baked
   into the image at the same path); `editor_sessions.bind_host` gets `"docker"`; the
   session URL is the `signed_preview_url` from task 2. Verify pkill/relaunch and
   `token_expires_at` refresh behave the same.
6. **Dev server on Docker**: run the existing mise setup/run/status/logs flows
   end-to-end; fix only environment drift (PATH/home), not logic. Log streaming uses
   `stream_command` from Epic 02.
7. **WebSocket preview**: verify HMR (Vite :5173) works through the proxy → container
   path (the WS bridge at `preview_proxy.py:198-210` should carry it; test it
   explicitly).
8. **Tests**: unit tests for `preview_link` (name-based URL, empty token) and resolver
   neutrality; integration (`DOCKER_INTEGRATION_TESTS=1`): start a static server in a
   container, mint a capability, fetch through a locally-instantiated proxy app, assert
   body + a WS echo; editor launch → URL 200s; dev-server start/logs/stop for a Vite
   fixture.

## Frontend Tasks

- None functionally — Preview panel, "Run App", and the editor open URLs the backend
  hands them. One check: any UI copy that says "Daytona" around preview/editor states
  becomes provider-neutral ("sandbox"). (Full provider labelling lands in Epic 06.)

## Database Tasks

- None required. `editor_sessions.bind_host` is a plain string column — `"docker"` needs
  no migration. Optional cleanup (defer to Epic 06): rename the
  `DAYTONA_PREVIEW_PROXY_*` settings/env names to neutral ones with aliases.

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

- On a Docker workspace: "Run App" detects the port, creates a preview session, and the
  capability URL serves the app — including Vite HMR over WebSocket.
- The editor opens (code-server via capability URL), edits a file, and the change is
  visible via the Files panel — on Docker.
- Dev server start/status/logs/stop work; logs stream live over the existing WS.
- The same flows on a Daytona workspace are unchanged (regression suite + one manual
  smoke: the preview URL still carries the real Daytona token).
- No sandbox container has host-published ports (`docker inspect` assertion in the
  integration suite).

## Dependencies

- **Epic 03** (fs for dev-server config detection; exec parity) — hard dependency.
- **Non-overlap with `horizontal-scale/07` (Stateless Preview Proxy)**: that epic
  re-architects proxy *statefulness and routing*; this epic only changes *what
  `preview_link` returns* for one provider. Both touch `preview_proxy.py` — if 07 lands
  first, implement against its resolver; if this lands first, 07 inherits a
  provider-neutral resolver. Flag it in both PRs.

## Future Enhancements

- Per-preview-session network aliases (unique DNS name per session) if container name
  collisions ever matter.
- Local HTTPS (mkcert) profile for the proxy in self-host setups.
- Port auto-detection beyond the fixed candidate list (parse dev-server stdout).
