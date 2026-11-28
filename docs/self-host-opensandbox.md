# Self-hosting Enigma with OpenSandbox

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) runs each workspace in a container managed by its own server (`opensandbox-server`). Enigma talks to it as `runtime="opensandbox"`.

**Status:** the self-host default (Plan 7b, `docs/superpowers/plans/2026-11-27-enigma-opensandbox-cutover.md`). The security suite in this guide passed against the reference config, both with the server on the host and inside compose. The custom Docker provider (`runtime="docker"`) was **removed** after `v0.1.0`, the last release with it (Plan 8, `docs/superpowers/plans/2026-11-28-enigma-remove-docker-provider.md`); see "Moving from Docker" below. Daytona stays the optional cloud provider.

## Security first

opensandbox-server 1.2.1 does not isolate tenants on its own. We verified this live (plan `docs/superpowers/plans/2026-11-27-enigma-opensandbox-hardening.md`, Task 1):
- All sandboxes share one Docker bridge.
- Each sandbox runs `execd`, the agent that runs commands, files and terminals. Without an access token, execd accepts **any** caller, including a sibling sandbox.

Enigma closes this, but only together with the server config below:

| Control | Where | What it does |
|---|---|---|
| **Per-sandbox execd token** | Enigma, always | Each sandbox is created with `EXECD_ACCESS_TOKEN` = HMAC(`OPENSANDBOX_EXECD_TOKEN_KEY`, workspace id). Every SDK call, terminal and preview sends it. A sibling without it gets 401. |
| **Egress policy** | Enigma sends it; the server enforces it | Private (RFC 1918), CGNAT, link-local and IPv4-mapped ranges are always denied. `SANDBOX_NETWORK_BLOCK_ALL` / `SANDBOX_NETWORK_ALLOW_LIST` (CIDRs) apply as on the other providers. **Needs `[egress] mode = "dns+nft"`.** In `dns` mode, a connection by IP is not blocked. |
| **Container floor** | The server sets it (`[docker]`); Enigma checks it | `drop_capabilities = ["ALL"]`, `no_new_privileges`, `pids_limit`, non-root image. Enigma probes every sandbox (`CapEff=0`, `NoNewPrivs=1`, uid ≠ 0). A new sandbox below that is destroyed; an existing one is refused (recreate the workspace). |
| **Safe create request** | Enigma | Only image, resources, metadata, the token and the policy are sent. `extensions` (one grants `SYS_ADMIN`), `volumes` (any named Docker volume), `platform` and `entrypoint` are refused. |
| **API key** | Server and Enigma | Without `OPENSANDBOX_API_KEY`, or when the server answers an unauthenticated request, Enigma reports OpenSandbox as `insecure` (`/health` degraded) and offers it to no one. |
| **Preview paths** | Enigma's preview proxy | A preview's URL shares an origin with execd's API, so paths with dot segments (also encoded) are refused before they are forwarded. |

What the server itself requires:
- **The Docker socket.** The server holds it, and that is root-equivalent on the host. Run it on a dedicated machine or VM.
- **Private networking.** Bind `[server] host` and `[docker] publish_host` to private addresses that only Enigma's backend and worker reach. The server has no TLS.
- **Never** `OPENSANDBOX_INSECURE_SERVER=YES` outside a laptop.

## Quick start: compose

The server runs next to the stack, from `docker-compose.opensandbox.yml`:

```bash
./sandbox/build-local-image.sh            # once: enigma-sandbox:latest
# .env: OPENSANDBOX_API_KEY, OPENSANDBOX_EXECD_TOKEN_KEY and METRICS_TOKEN (long random values)
# macOS / Windows (Podman machine, Docker Desktop) also:
#   OPENSANDBOX_SERVER_CONFIG=sandbox.compose-desktop.toml
npm run dev:opensandbox                   # podman compose -f docker-compose.yml -f docker-compose.opensandbox.yml up --build
```

New workspaces then go to OpenSandbox; `/health` shows `runtimes.opensandbox.status = "ok"` and `sandbox_runtime_default = "opensandbox"`. Pre-pull `opensandbox/execd:v1.1.0` and `opensandbox/egress:v1.1.7` once, or the first create can time out.

What the override does:
- **The server** is `opensandbox/server:release-1.2.1`, pinned by digest. It is the only service that gets the daemon socket, and it publishes no host port. It starts with `cap_drop: ALL` and `no-new-privileges` (little help while it holds the socket, but free).
- **Enigma's services** (backend, worker, preview proxy) get `SANDBOX_RUNTIME_DEFAULT=opensandbox`, the server's address and both keys, and wait for the server's healthcheck. Backend and worker run the upkeep loop.
- **Where sandbox ports go.** The server publishes every sandbox's execd and preview ports on a host address that Enigma's services reach. Sandboxes cannot reach it, because their egress policy denies private ranges (the suite checks this).
  - Linux (`sandbox.compose.toml`, the default): the gateway of the `opensandbox` compose network, `10.214.0.1`.
  - Podman machine / Docker Desktop (`sandbox.compose-desktop.toml`): the host's loopback, reached through `host.docker.internal`. Their port forwarder binds published ports on the desktop side, where the compose gateway does not exist.
  - Other processes or containers on the same host *can* reach these ports; then each sandbox's execd token is the only barrier. Run it on a host that serves nothing else, or firewall `40000-60000` to the `opensandbox` bridge.
- Both configs keep every security key of `deploy/opensandbox/sandbox.toml`; `backend/tests/test_deployment.py` fails if they drift.
- Rootless Podman is not supported on this path (it publishes ports through pasta/slirp4netns).

## Server elsewhere (a VM)

For a server on its own machine, the placement this guide recommends for production:

1. **Image.** Build the sandbox image once: `./sandbox/build-local-image.sh` (`enigma-sandbox:latest`, built from the same `sandbox/Dockerfile` as the Daytona snapshot). For production, push it to a registry and pin it by digest. `OPENSANDBOX_SANDBOX_IMAGE=registry/enigma-sandbox@sha256:…` is required outside `local`.
2. **Server.** Copy `deploy/opensandbox/sandbox.toml` and adjust `host`, `publish_host` and the store path. Pre-pull `opensandbox/execd:v1.1.0` and `opensandbox/egress:v1.1.7`; without them, the first create can time out. Then start it:

   ```bash
   OPENSANDBOX_SERVER_API_KEY=<long random> DOCKER_HOST=unix:///var/run/docker.sock \
     uvx --from 'opensandbox-server==1.2.1' opensandbox-server --config sandbox.toml
   ```

3. **Enigma.** Set these in `.env`:
   - `OPENSANDBOX_API_URL` (the private address)
   - `OPENSANDBOX_API_KEY` (the same value)
   - `OPENSANDBOX_EXECD_TOKEN_KEY` (`python -c 'import secrets; print(secrets.token_urlsafe(48))'`)
   - optionally `OPENSANDBOX_SANDBOX_CPU` / `_MEMORY` / `_DISK` (defaults 2 / 4Gi / none)

   Set `SANDBOX_RUNTIME_DEFAULT=opensandbox` to make it the default. The base `docker-compose.yml` forwards every `OPENSANDBOX_*` setting, so this works without the override.
4. **Check.** `/health` should show `runtimes.opensandbox.status = "ok"`. If it shows `insecure`, the key is missing or the server does not enforce it.

## Kubernetes

`deploy/k8s/overlays/opensandbox-external` layers on the production overlay. The server runs **outside** the cluster, on a dedicated VM set up as in "Server elsewhere"; nothing in the cluster gets a daemon socket.
- Replace the placeholders: the server's private address (`config-patch.yaml` and `network-policy.yaml`) and the sandbox image digest.
- Add `OPENSANDBOX_API_KEY` and `OPENSANDBOX_EXECD_TOKEN_KEY` to `enigma-secrets` (`deploy/README.md`).
- On the server, set `[server] host` and `[docker] publish_host` to that private address. The overlay's NetworkPolicy opens egress from the API, workers and preview proxy to that one host only: the API port and the published port range `40000-60000`.

OpenSandbox's own Kubernetes runtime (controller and CRDs) is not used and was not verified.

## Moving from Docker (upgrading to v0.2.0)

v0.2.0 removes the Docker provider; `v0.1.0` is the last release with it. There is no in-place migration of a sandbox: git is the source of truth. After the upgrade:
- `SANDBOX_RUNTIME_DEFAULT=docker`, or docker in `SANDBOX_RUNTIMES_ALLOWED`, stops the API at startup with a message naming `v0.1.0`. Use `opensandbox` or `daytona`.
- Leftover `DOCKER_*` variables are ignored and logged once as `removed_setting_name`. `DOCKER_REAPER_INTERVAL_SECONDS` is now `SANDBOX_UPKEEP_INTERVAL_SECONDS` (the old name still works for one release). `docker-compose.sandbox.yml` is gone; use `docker-compose.opensandbox.yml`.
- A workspace with `runtime='docker'` has no provider: every call to it fails with 503, and it cannot be deleted through the API. The create form and `/health` no longer list Docker.

Remove Docker workspaces in one of two ways:
- **Before upgrading** (on v0.1.0): push each workspace's branches, create a new workspace on OpenSandbox from the same repository, and delete the Docker one through the UI. That also removes its container.
- **After upgrading**, in SQL: `DELETE FROM workspaces WHERE runtime = 'docker';`. Every foreign key to `workspaces` cascades, except `audit_logs`, which is set null so the audit trail stays. Then remove the leftover containers by hand (`docker ps -a --filter label=enigma.managed`).

## Upkeep

The server keeps sandboxes until they are deleted (Enigma creates them without a TTL). Enigma's upkeep loop does the rest. It is `OPENSANDBOX_UPKEEP`; unset means on when the default runtime is opensandbox. Set it to `true` on a mixed install.

- **Idle pause.** A workspace idle past `SANDBOX_AUTO_STOP_MINUTES` is paused, with its files kept. The next exec, terminal or preview resumes it. A sandbox counts as busy while Enigma runs something in it: a running command renews its activity every minute, and terminal output renews it as it flows.
- **Orphans.** This installation's sandboxes (`enigma.installation` metadata) whose workspace row is gone are destroyed. Unlabelled sandboxes, and other installations' sandboxes, are never touched.

## The security suite

`backend/tests/test_opensandbox_integration.py` is opt-in. It runs against a real server started from the reference config:

```bash
OPENSANDBOX_INTEGRATION_TESTS=1 OPENSANDBOX_API_URL=http://127.0.0.1:8080 \
  OPENSANDBOX_API_KEY=<key> uv run pytest tests/test_opensandbox_integration.py
```

For the compose path, run it from the backend container, as the weekly CI job `opensandbox-compose` does (use a separate project name so `down -v` never touches your dev database):

```bash
podman compose -p enigma-suite -f docker-compose.yml -f docker-compose.opensandbox.yml up -d --wait opensandbox-server postgres redis
podman compose -p enigma-suite -f docker-compose.yml -f docker-compose.opensandbox.yml run --rm \
  -e OPENSANDBOX_INTEGRATION_TESTS=1 -e ENVIRONMENT=local backend \
  sh -lc "uv sync --locked && uv run alembic upgrade head && uv run pytest tests/test_opensandbox_integration.py"
```

It checks:
- the container floor;
- that sandbox A cannot reach sandbox B's execd, by token, by network, and at its published endpoint;
- unauthenticated API and execd calls are refused;
- the egress allow-list;
- a terminal that survives a reconnect;
- the upkeep;
- that an unsafe create request is refused.

## Known limits (opensandbox-server 1.2.1)

- **Server proxy.** Its `/sandboxes/{id}/proxy/{port}` route skips the API key in single-tenant mode. Enigma does not use it; keep the server unreachable by users.
- **Container profile.** It is server-wide only. There is no read-only root filesystem and no forced user; the image's `USER` applies (`daytona`, non-root).
- **Expiry.** It runs on in-process timers. With no restart policy, a daemon restart leaves containers stopped; Enigma resumes them on use.
- **Rotation.** Rotating `OPENSANDBOX_EXECD_TOKEN_KEY` locks Enigma out of existing sandboxes until they are recreated.
