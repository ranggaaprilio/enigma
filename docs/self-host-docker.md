# Self-hosting Enigma with local Docker sandboxes

This guide covers running Enigma without a Daytona subscription. Every workspace runs in a
container on your own Docker or Podman daemon. The full Enigma experience works here:
- the agent
- terminal
- editor (code-server)
- previews
- services
- worktrees
- Spec Kit

You can also keep Daytona configured and offer both. The create form then lets users pick.
A mixed install that keeps `SANDBOX_RUNTIME_DEFAULT=daytona` still needs the Docker upkeep
loop. The compose override turns it on with `DOCKER_UPKEEP=true`; set it yourself outside
compose. Without upkeep, Docker workspaces are refused.

This profile is **one host**. Kubernetes (`deploy/k8s`, `deploy/README.md`) stays
Daytona-only. Running Docker sandboxes on several nodes is not supported (see
[Limits](#limits)).

## Security first

- **The Docker socket is root-equivalent on the host.** The backend and the worker get it,
  because they create and drive the sandboxes. Anyone who controls those processes
  controls the machine.
  - For more than one trusted user, run Enigma on a **dedicated machine or VM** that holds
    nothing else of value.
  - Never mount the socket into anything else.
- Sandboxes never get the socket:
  - The provider refuses any container spec with mounts, `privileged` mode, added
    capabilities, host namespaces or a root user.
  - Sandboxes run with `cap_drop=ALL`, `no-new-privileges`, a pids limit, CPU and memory
    limits, and a `/tmp` tmpfs.
- **Isolation between sandboxes.** Each sandbox gets its own bridge network, so sandboxes
  cannot reach each other.
- **The preview proxy.**
  - The proxy is attached to every sandbox network and holds `AUTH_SECRET_KEY` and
    `DATABASE_URL`. That makes it the one component worth hardening.
  - It never gets the socket, and it runs with `ip_forward=0`.
  - Its `/health` and `/metrics` require `METRICS_TOKEN`. The backend and the proxy refuse
    to start on a Docker default without one.
- **Residual exposure.**
  - A sandbox can reach its network gateway, meaning host services bound to `0.0.0.0`.
  - With egress open, a sandbox can reach anything on the internet, including a cloud
    metadata address.
  - Bind host services to `127.0.0.1`, or set `SANDBOX_NETWORK_BLOCK_ALL=true`.

## Prerequisites

- Docker Engine 24+ or Podman 5+ with a compose provider (`docker compose` or
  `podman compose`).
- About 10 GB of disk for the sandbox image, plus the per-workspace files.
- About 4 GB of RAM per concurrently active sandbox at the default limits.
- Network access from the host at **build time**, for the image toolchain. Sandboxes need
  outbound network only for what your projects do: package registries, git hosts, and
  template clones from github.com.

### Socket path

| Platform | `DOCKER_SOCKET` |
|---|---|
| Docker on Linux / Docker Desktop | `/var/run/docker.sock` (default) |
| Podman machine (macOS/Windows) | The VM's socket (`podman machine inspect`); on a rootful machine, `/var/run/docker.sock` is a symlink to it |
| Rootless Podman on Linux | `$XDG_RUNTIME_DIR/podman/podman.sock`, after `systemctl --user enable --now podman.socket` |

On SELinux hosts (the Podman machine, Fedora/RHEL), the compose override sets
`label=disable` on the services that get the socket. Without it they get `Permission denied`.

## Install

1. **Build the sandbox image** once. Rebuild it after `sandbox/Dockerfile` changes.

   ```bash
   ./sandbox/build-local-image.sh                 # tags enigma-sandbox:latest
   ```

   This is the same image the Daytona snapshot uses, so both providers run an identical
   toolchain: `mise`, `code-server`, `uv`, git and tmux.

2. **Write `.env`.** Copy `.env.example` and set at least:

   ```bash
   AUTH_SECRET_KEY=...                  # python -c 'import secrets; print(secrets.token_urlsafe(48))'
   WORKSPACE_ENV_ENCRYPTION_KEY=...     # python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   METRICS_TOKEN=...                    # required with Docker sandboxes
   DAYTONA_API_KEY=                     # empty: no subscription
   SANDBOX_RUNTIME_DEFAULT=docker       # the compose override's default too
   ```

   Never change `WORKSPACE_ENV_ENCRYPTION_KEY` afterwards: stored credentials would become
   unreadable.

3. **Start the stack** with the sandbox override:

   ```bash
   podman compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d --build
   # docker: docker compose -f docker-compose.yml -f docker-compose.sandbox.yml up -d --build
   ```

   The override:
   - mounts the socket into `backend` and `worker` only;
   - labels the `preview-proxy` so the backend can attach it to sandbox networks;
   - sets `SANDBOX_RUNTIME_DEFAULT=docker`.

4. **Check** `http://localhost:8000/health`. It reports each provider:

   ```json
   {"status": "ok", "sandbox_runtime_default": "docker",
    "runtimes": {"daytona": {"configured": false, "status": "unconfigured", ...},
                 "docker":  {"configured": true,  "status": "ok", "managed_containers": 0,
                             "upkeep_last_pass_at": "..."}}}
   ```

   An unconfigured provider is not a fault. `/health` is `degraded` (503) only when a
   configured provider is down, or when no provider is available at all.

5. Open the app at `http://localhost:5173`, register the first user, and create a
   workspace. The create form offers only the providers that are available, and says why
   one is not (for example, "No Daytona API key is configured").

## How a Docker workspace behaves

- **Provisioning.** A workspace gets one long-lived container, `execute-<workspace hex>`,
  from `DOCKER_SANDBOX_IMAGE`, on its own network `enigma-sbx-<workspace hex>`.
  `workspace.snapshot` is ignored, because Docker always uses the image.
- **Availability check.** Creating a Docker workspace while the daemon is down fails
  immediately with a 422. Nothing is half-created.
- **Idle stop.** After `SANDBOX_AUTO_STOP_MINUTES` without activity (default 30; 0 means
  never), the upkeep loop stops the container but keeps it.
  - Any use wakes it again: a terminal, a command, a preview or the editor.
  - A sandbox with a live command or attached terminal is spared, but only for up to
    `SANDBOX_BUSY_MAX_MINUTES`.
- **Upkeep.** At startup, the loop removes this installation's containers and networks
  whose workspace row is gone. Two stacks on one daemon never touch each other's
  sandboxes.
- **Previews and the editor.**
  - Both go through the preview proxy.
  - A dev server listening only on `127.0.0.1` (Vite's default) is relayed to the proxy,
    and only to the proxy.
- **Out of memory.** The kernel kills the biggest process in the sandbox, and the
  workspace header shows that detail.

## Tuning

| Setting | Default | Notes |
|---|---|---|
| `DOCKER_SANDBOX_CPUS` | `2` | Per sandbox |
| `DOCKER_SANDBOX_MEMORY_MB` | `4096` | Swap is capped at the same value |
| `DOCKER_SANDBOX_PIDS_LIMIT` | `4096` | Fork-bomb floor |
| `DOCKER_SANDBOX_TMP_MB` | `1024` | `/tmp` is a tmpfs and counts against memory |
| `DOCKER_SANDBOX_SUBNET_POOL` | `10.213.0.0/16` | One /28 per sandbox (about 4096 sandboxes); pick a range your LAN does not use |
| `SANDBOX_AUTO_STOP_MINUTES` | `30` | `0` never stops idle sandboxes |
| `DOCKER_REAPER_INTERVAL_SECONDS` | `60` | Upkeep pass: proxy re-attach and idle stop |

### Egress

- **Open by default.** Sandboxes can reach the internet, which projects usually need for
  `npm install`, `pip`, git and so on.
- **`SANDBOX_NETWORK_BLOCK_ALL=true`** makes every sandbox network `internal`: no outbound
  traffic and no outside DNS.
  - Previews still work.
  - The agent's file, search, run and git tools, and the terminal, keep working.
  - Anything that needs the network fails as an ordinary command error.
  - Existing networks are rebuilt on the next upkeep pass.
- **`SANDBOX_NETWORK_ALLOW_LIST`** is honoured by Daytona only. On Docker, egress is either
  open or blocked.

## Limits

- **One host.** Every service that drives sandboxes must reach the same daemon: the API,
  the workers and the preview proxy.
  - In the default inline mode this is automatic.
  - In queue mode (`RUN_EXECUTION_MODE=queue`, `--profile queue`), set
    `DOCKER_SINGLE_HOST=true` to state that they share one daemon. Without it, creating a
    Docker workspace is refused (422).
  - Docker workspaces are pinned to that host. `docs/horizontal-scale/` and
    `deploy/README.md` describe the multi-replica Kubernetes deployment, which this profile
    does **not** cover. Kubernetes stays Daytona-only.
- **No provider migration.** A workspace stays on the provider it was created with.
- **No per-sandbox disk quota.** Watch the daemon's disk.

## Backup and reset

- **Postgres** holds everything that matters: workspaces, runs, credentials and settings.
  - `./scripts/backup_db.sh` writes `backups/<db>-<timestamp>.dump`.
  - `./scripts/restore_db.sh <dump> --drop-after` test-restores it into a scratch database.
- **A sandbox is disposable working state.** Commit and push the work that matters.
  - To reset one, remove its container (`docker rm -f execute-<workspace hex>`).
  - Then re-provision it with `POST /workspaces/{id}/provision`, the call behind saving the
    workspace settings when the workspace is in `error`. That gives you a fresh container,
    and you clone or pull the repository again.
  - A workspace whose container vanished is marked `error` at the next backend start.
- **Restoring a database onto another daemon.** That daemon starts with no sandboxes.
  Re-provision each workspace the same way.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/health` docker `unconfigured` | No socket at `DOCKER_SOCKET` inside the backend container |
| docker `error` / "The Docker daemon is not reachable" | Daemon down, or a SELinux denial (`label=disable` missing) |
| Compose fails: `set METRICS_TOKEN in .env` | Set `METRICS_TOKEN`; it is required with the sandbox override |
| Preview shows "Waking preview…" forever | The app must listen on the configured port; `podman network inspect enigma-sbx-<hex>` should list the proxy |
| Image pull error at provision | Build the image first; `DOCKER_SANDBOX_IMAGE` must match its tag |
| Startup log `sandbox_runtime_default_unconfigured` | The default provider is unavailable; new workspaces fall back to the other one |
| Startup log `deprecated_setting_name` | Rename the `DAYTONA_*` setting it names; the old name works for one more release. Under compose the old names in `.env` are mapped to the new ones before they reach the container, so this log only appears for a backend run outside compose |
