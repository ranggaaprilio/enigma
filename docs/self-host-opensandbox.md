# Self-hosting Enigma with OpenSandbox

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) runs each workspace in a container managed by its own server (`opensandbox-server`). Enigma talks to it as `runtime="opensandbox"`.

**Status:** opt-in. Docker (`docs/self-host-docker.md`) stays the self-host default until the security suite in this guide passes against your server. That suite is the gate for making OpenSandbox the default (Plan 7b).

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

## Setup

1. **Image.** Build the sandbox image once: `./sandbox/build-local-image.sh` (`enigma-sandbox:latest`, shared with Docker and Daytona). For production, push it to a registry and pin it by digest. `OPENSANDBOX_SANDBOX_IMAGE=registry/enigma-sandbox@sha256:…` is required outside `local`.
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

   Users can then pick OpenSandbox when they create a workspace. `SANDBOX_RUNTIME_DEFAULT=opensandbox` makes it the default, but **only after the security suite passes**.
4. **Check.** `/health` should show `runtimes.opensandbox.status = "ok"`. If it shows `insecure`, the key is missing or the server does not enforce it.

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

It checks:
- the container floor;
- that sandbox A cannot reach sandbox B's execd, by token and by network;
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
