# Enigma — OpenSandbox as the Primary Runtime, Step 2: Cutover (Plan 7b)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** OpenSandbox becomes the **self-host default** runtime and replaces the custom Docker provider in the compose stack. Docker stays selectable but is marked deprecated; its code is not deleted in this plan.

**Precondition (met):** Plan 7a (`2026-11-27-enigma-opensandbox-hardening.md`, PR #34, merged) — the live security suite passed 9/9 against `deploy/opensandbox/sandbox.toml`.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §9.2: the Docker provider is replaced by OpenSandbox; Daytona stays as the optional cloud provider.

## Current state (verified)

- **Defaults.** `SANDBOX_RUNTIME_DEFAULT` defaults to `daytona` (`core/config.py:176`). Self-host today means `docker-compose.sandbox.yml`, which sets the default to `docker`, mounts the daemon socket into backend + worker, and adds the egress proxy.
- **Compose does not forward any `OPENSANDBOX_*` setting.** The base file lists env explicitly (no `env_file`), so OpenSandbox only works today with the backend running on the host (how 7a's suite and E2E ran).
- **The server has an official image:** `docker.io/opensandbox/server:release-1.2.1` (published 2026-11-13, same version 7a verified).
- **Server in a container** (opensandbox-server 1.2.1, `config.py`): bridge-mode sandbox ports are published on `[docker] publish_host` (an IPv4 address; the docs name "the Docker bridge gateway … when the server runs in a container"), and `[docker] host_ip` is the address it puts in endpoint URLs when it detects a container (`/.dockerenv`, `/run/.containerenv`). So backend, worker and preview proxy must reach `host_ip:<published port>`; `127.0.0.1` no longer works.
- **This machine:** Podman machine, rootful, netavark. Rootless Podman publishes ports differently (pasta/slirp4netns); out of scope here, documented as unsupported for the compose path.
- **k8s** (`deploy/k8s`) is Daytona-only, guarded by `test_deployment.py` (`docker.sock` never referenced). OpenSandbox's own Kubernetes runtime (controller + CRDs) was never verified and is **out of scope**.
- **Frontend copy** (`lib/runtime.ts`, `CreateWorkspaceWizard.tsx`) still says "configure Daytona or Docker"; `runtimeErrorHint` knows only Docker.

## Design

### 1. Compose: `docker-compose.opensandbox.yml` (the new self-host path)

```bash
./sandbox/build-local-image.sh
podman compose -f docker-compose.yml -f docker-compose.opensandbox.yml up
```

- **`opensandbox-server` service:** `opensandbox/server:release-1.2.1` pinned by digest. It is the only service holding the daemon socket (in the Docker override, backend + worker hold it). Config `deploy/opensandbox/sandbox.compose.toml` mounted read-only; `OPENSANDBOX_SERVER_API_KEY` from `.env` (`:?` required); no host port published; sqlite store on a named volume. It holds the socket, so it is root-equivalent on the host; documented, not hidden.
- **Where sandbox ports are published: two config variants** (Task 1 settled this live). The server publishes every sandbox port on `[docker] publish_host` and puts `[docker] host_ip` in endpoint URLs; backend, worker and proxy must reach `host_ip:<port>`.
  - **Linux (default), `sandbox.compose.toml`:** a compose network `opensandbox` with a **fixed subnet** (`10.214.0.0/24`, gateway `.1`), `publish_host = host_ip = 10.214.0.1`. Only `opensandbox-server`, `backend`, `worker` and `preview-proxy` join it. Not verifiable on a Podman machine (below); proven by the weekly CI workflow on a Linux runner (Task 4b).
  - **Desktop (Podman machine, Docker Desktop), `sandbox.compose-desktop.toml`:** `publish_host = 127.0.0.1`, `host_ip = host.docker.internal` (Podman and Docker Desktop both resolve it). Picked with `OPENSANDBOX_SERVER_CONFIG=sandbox.compose-desktop.toml`. Verified live (Task 1).
  - Both keep every security key of the reference `sandbox.toml`; a test pins that.
  - Sandboxes sit on the daemon's default bridge, and their egress policy denies all private ranges (`dns+nft`), so they cannot reach the published ports at either address. Task 1 proved this for the desktop variant (`000`), and a new suite test pins it (Task 2).
- **Backend, worker, preview-proxy env:** `SANDBOX_RUNTIME_DEFAULT=opensandbox`, `OPENSANDBOX_API_URL=http://opensandbox-server:8080`, the API key, `OPENSANDBOX_EXECD_TOKEN_KEY` (`:?` required), `OPENSANDBOX_UPKEEP=true`, image and resources. `METRICS_TOKEN` stays required.
- **Base compose** forwards the `OPENSANDBOX_*` settings with empty defaults, so an install pointing at an external server (the host-run setup from 7a) works without the override.
- `npm run dev:opensandbox` (and a `docker compose` variant) wrap the command.

### 2. Docker is deprecated, not removed

- `/health` `runtimes.docker` gets `deprecated: true`; the API logs `deprecated_runtime provider=docker` once at startup when Docker is configured and allowed.
- Frontend: the Docker option reads "Local Docker (deprecated)"; the default selection already follows `sandbox_runtime_default`.
- `docker-compose.sandbox.yml` and `docs/self-host-docker.md` open with a deprecation note pointing at the OpenSandbox guide. Existing Docker workspaces keep working. There is no in-place migration: git is the source of truth (push, recreate the workspace on OpenSandbox, re-clone).
- Removing `docker.py`, `docker_fs.py`, `docker_pty.py`, `port_relay.py` and `egress_proxy.py` is a later plan, after a release with OpenSandbox as the default.

### 3. k8s: an external OpenSandbox server, optionally

`deploy/k8s` stays Daytona by default. An example overlay `deploy/k8s/overlays/opensandbox-external` points the API, worker and proxy at an OpenSandbox server on a dedicated VM (the guide's recommended placement), and adds a NetworkPolicy egress rule to that one private CIDR on the server port and the published port range. No socket in the cluster; `test_deployment.py` keeps guarding that.

### 4. Copy and docs

- `runtime.ts`: `runtimeErrorHint("opensandbox")` → "Is the OpenSandbox server running?"; the wizard's "no runtime" text names Daytona or OpenSandbox.
- `docs/self-host-opensandbox.md`: status → **default for self-host**; a compose section; the migration note; k8s placement.
- `.env.example`, `AGENTS.md` (note 32 updated, Docker paragraph marked deprecated), `deploy/README.md`.

## Non-goals

- Deleting the Docker provider (a later plan).
- OpenSandbox's Kubernetes runtime (controller/CRDs), or running the server in-cluster.
- Rootless Podman for the compose path.
- Migrating live Docker sandboxes' state.

## Tasks

### Task 1: Spike — the server inside compose, live

**Files:** scratchpad only.

- [x] Started `opensandbox/server:release-1.2.1` (image digest `sha256:8782b93fbf01c27bb0a54584d06f4810ad5abf979727b9e0c39564cfde6ad4cf`, reports opensandbox-server 1.2.1) with the socket, on a `10.214.0.0/24` network, plus Postgres, Redis and a backend dev container on the same network.
- [x] **Gateway publish fails on a Podman machine.** Every create failed: `listen tcp 10.214.0.1:41121: bind: can't assign requested address`. The address exists in the VM (`podman8`), but Podman's port forwarder (gvproxy) binds published ports on the macOS side, where it does not. Docker Desktop forwards the same way. A Linux daemon binds in the host's own namespace, where the gateway exists.
- [x] **Desktop fallback passes:** `publish_host = 127.0.0.1`, `host_ip = host.containers.internal`, server still in a container. The 7a security suite ran from inside the backend container: 9/9 twice. A first run right after a server restart failed one test with `RuntimeUnavailableError`, hence a healthcheck on the server and `depends_on: service_healthy` (Task 2).
- [x] From sandbox A, sandbox B's execd at its published endpoint (`http://host.containers.internal:<port>/…`) is unreachable (`000`). Sandboxes resolve `host.docker.internal` (`192.168.127.254`), but the egress policy denies `192.168/16`.
- [x] Podman gives every container both `host.containers.internal` and `host.docker.internal`; Docker Desktop gives `host.docker.internal`. The server image answers `GET /health` with 200.
- **Decision (user):** two config variants: Linux gateway (default) and desktop loopback, with the Linux variant proven in CI.

### Task 2: Compose override + forwarding

**Files:** `docker-compose.opensandbox.yml`, `docker-compose.yml`, `deploy/opensandbox/sandbox.compose.toml`, `deploy/opensandbox/sandbox.compose-desktop.toml`, `package.json`, `backend/tests/test_deployment.py`, `backend/tests/test_opensandbox_integration.py`.

- [x] Failing tests:
  - the server is the only service with the socket, and its image is pinned by digest;
  - the API key and execd key are required (`:?`);
  - backend, worker and proxy get the `OPENSANDBOX_*` set and `SANDBOX_RUNTIME_DEFAULT=opensandbox`;
  - the fixed-subnet network is joined by exactly those four services;
  - both compose configs keep every security key of the reference `sandbox.toml` and differ only in `host`, `publish_host`, `host_ip` and the store path;
  - the server has a healthcheck, and backend, worker and proxy wait for it;
  - live suite: sandbox A cannot reach sandbox B's execd at its **published** endpoint;
  - the base file forwards `OPENSANDBOX_*`.
- [x] Implemented. `OPENSANDBOX_UPKEEP` / `OPENSANDBOX_REQUIRE_IMAGE_DIGEST` now read an empty value as unset, because compose forwards them as `${NAME:-}`.

### Task 3: Deprecate Docker

**Files:** `services/sandbox/availability.py`, `api/health.py`, `main.py`, `frontend/src/lib/runtime.ts`, `CreateWorkspaceWizard.tsx`, tests (backend + vitest).

- [x] Failing tests:
  - `/health` `runtimes.docker.deprecated` is true (the others false);
  - one `deprecated_runtime` log at startup when Docker is allowed and configured, none otherwise;
  - the wizard labels Docker deprecated;
  - `runtimeErrorHint("opensandbox")` and the "no runtime" copy.
- [x] Implemented. The frontend `RuntimeHealth` type also gains the `insecure` status from 7a (the backend already sent it); the overview shows it like `error`.

### Task 4: k8s example overlay

**Files:** `deploy/k8s/overlays/opensandbox-external/*`, `backend/tests/test_deployment.py`, `.github/workflows/ci.yml`, `deploy/README.md`.

- [x] Failing tests:
  - the overlay builds on base and sets `SANDBOX_RUNTIME_DEFAULT=opensandbox`;
  - the API key and execd key come from the Secret, never a ConfigMap;
  - its NetworkPolicy allows egress only to the one server CIDR, on the server port and the published range;
  - no `docker.sock` (the existing guard covers it).
- [x] Implemented; CI renders it next to production and kubeconform validates both. Rendering it (kubectl 1.30, kubeconform 0.6.7: 26/26 valid) caught a real bug the file tests missed: production's `namespace` covers only its own resources, so the new NetworkPolicy landed in `default` and selected nothing. The overlay now sets `namespace: enigma`, pinned by a test.

### Task 4b: The Linux variant in CI

**Files:** `.github/workflows/opensandbox-integration.yml`.

- [x] A second weekly job, `opensandbox-compose`, starts the server from `docker-compose.opensandbox.yml` (default config: the Linux gateway) and runs the suite with `docker compose run backend`, the path operators use.
- [x] The same steps ran locally with `OPENSANDBOX_SERVER_CONFIG=sandbox.compose-desktop.toml`, in a separate compose project (`-p enigma7b`, so `down -v` never touches the dev database): **10/10**, the server with `cap_drop: ALL` and `no-new-privileges`.

**Security review (automated, on the commit):** the overlay lets the API pod reach the server, and the API pod also sends owner-configured requests (the LLM base URL), so an SSRF could aim there. Checked live from the `opensandbox` network: per sandbox the server publishes execd (every path `401` without the token), the egress sidecar (`/policy` `401`, other paths `404`) and one port with no listener; the server API is `401` without its key. Reachable, not usable; noted in the NetworkPolicy.

### Task 5: Docs and agent notes

**Files:** `docs/self-host-opensandbox.md`, `docs/self-host-docker.md`, the `docker-compose.sandbox.yml` header, `.env.example`, `AGENTS.md`, `deploy/README.md`, this plan.

- [x] Status, compose path, migration note, k8s placement, deprecation banners.

### Task 6: Live E2E on the compose stack

- [x] Ran `docker-compose.yml` + `docker-compose.opensandbox.yml` with the desktop config, as a separate compose project (`-p enigma7b`) with throwaway secrets, on Podman machine. Results:
  - `/health`: `opensandbox` ok and `sandbox_runtime_default = "opensandbox"`; `docker` reports `deprecated: true`.
  - Upkeep: at startup the backend removed a labelled sandbox with no workspace row (`sandbox_orphan_removed`), the probe left over from the security-review check below.
  - A workspace created with no runtime landed on OpenSandbox and reached `ready`; file write and read through execd worked.
  - A server in the sandbox on port 3000 was detected as a preview, and fetching it through the compose **preview-proxy container** returned 200 with the page. So the proxy reaches execd at `host.docker.internal` with the sandbox's token.
  - One agent run (deepseek-v4.1-flash, set in the shell; `.env` unchanged) wrote the requested file and completed in about 8 s.
  - The 7a security suite, now 10 tests, passed from inside the compose backend container (Task 4b).
- [x] Cleaned up: the workspace was deleted through the API, no sandbox was left, and `down -v` ran on `enigma7b` only.

## Exit criteria

- The compose override starts the full stack with OpenSandbox as the default, and the 7a security suite passes from inside it.
- Docker is still selectable, and marked deprecated everywhere a user or operator sees it.
- Full backend suite and frontend lint/test/build are green (known unrelated failure: the `test_retention` index test).
