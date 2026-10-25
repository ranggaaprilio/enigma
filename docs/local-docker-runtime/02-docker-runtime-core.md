# Epic 02 - Docker Runtime Core (Lifecycle + Exec)

## Goal

Implement `DockerRuntime`, the second `SandboxRuntime` provider: provision, start, stop,
delete, status, `exec`, and `stream_command` against a local Docker (or Podman) daemon,
using a local build of the existing sandbox image. At the end of this epic an agent run
(`run_command` tool) completes end-to-end inside a local container for a workspace with
`runtime="docker"`.

## User Value

The first working slice of "Enigma without a Daytona subscription": create a workspace on
the local provider and have the AI execute commands in an isolated container on your own
machine — the OpenHands-style sandbox container, but behind Enigma's own runtime
interface.

## Current State

- Epic 01 delivered `services/sandbox/base.py` (interface), `daytona.py`, `factory.py`.
  The factory raises `RuntimeUnavailableError` for `runtime="docker"`.
- No `docker` SDK dependency exists in `backend/pyproject.toml`. Docker/Podman is only
  used for the dev stack (`docker-compose.yml`: postgres, redis, pgbouncer, backend,
  worker, preview-proxy, frontend — no `networks:` section) and the snapshot build
  (`daytona/Dockerfile`, `daytona/build-snapshot.sh` → `daytona snapshot create`).
- `daytona/Dockerfile` (`ARG BASE_SNAPSHOT=daytonaio/sandbox:0.8.0`) bakes in mise,
  standalone code-server, uv + Spec Kit `specify`, with `PATH=/home/daytona/.local/bin` —
  exactly the toolchain the rest of the app assumes (`editor.py`, `dev_server.py`,
  `workspace_services.py`, `speckit.py`). Its header notes SSH deploy keys need an
  OpenSSH client on PATH (`repositories.py::_run_ssh_git` reports "SSH client is not
  available in this sandbox image" otherwise).
- Config (`core/config.py:92-105`) is Daytona-only: `daytona_api_key`, `daytona_api_url`,
  `daytona_target`, `daytona_default_snapshot`, `daytona_default_language`,
  `daytona_workspace_path`, `daytona_sandbox_home` (`/home/daytona`),
  `daytona_auto_stop_minutes`, `daytona_provision_timeout_seconds`,
  `daytona_preview_url_ttl_seconds`, `daytona_preview_proxy_base_url` (+ a validator at
  ~320 that requires https unless loopback), `daytona_preview_proxy_ttl_seconds`,
  `daytona_network_block_all`, `daytona_network_allow_list`. Mise bootstrap is already
  configurable (`mise_auto_install`, `mise_install_url`), as is the code-server binary
  (`editor_code_server_binary`).
- Daytona semantics to match: provision is idempotent by name `execute-<uuid.hex>` with
  labels `execute-workspace-id` / `execute-owner-id` (ex `daytona_runtime.py:112-152`);
  `status` maps provider states to `ready|stopped|creating|error` (`:165`); `exec`
  returns `RuntimeCommandResult` with combined output in `stdout` and `stderr=""`
  (`:188` — Docker must mirror this quirk so consumers behave identically);
  auto-start-on-access via `_ensure_started` (`:324`).
- **Who talks to the daemon**: with `RUN_EXECUTION_MODE=queue` (the compose default for
  the `worker` service), agent and command runs execute in `app/worker.py`, not the API.
  The API still provisions/deletes sandboxes, serves terminals, files, editor and
  dev-server/service control; the preview proxy dials sandboxes per request. All three
  processes therefore need daemon access.

## Scope

**In scope**

- `services/sandbox/docker.py` — `DockerRuntime` implementing lifecycle + exec +
  stream_command + health. Uses the official `docker` Python SDK, every blocking call
  wrapped in `asyncio.to_thread` (the codebase's established pattern, cf.
  `agent_providers.py:155`, `worker.py:402-403`).
- Local sandbox image: generalize `daytona/Dockerfile` into `sandbox/` with a
  `build-local-image.sh` (plain `docker build`), keeping `daytona/build-snapshot.sh`
  consuming the same Dockerfile so the two images cannot drift.
- New config keys + compose wiring for daemon access.
- Docker fake + conformance fixture; gated integration tests.

**Out of scope**

- Filesystem/PTY/git parity (Epic 03) — agent file tools still fail politely on Docker
  until then.
- Preview/editor/dev-server (Epic 04).
- Idle reaper, resource limits, egress policy (Epic 05) — this epic runs containers with
  conservative hardcoded defaults, tuning comes later.
- Multi-node scheduling (out of scope for the series). Concretely: a worker fleet on
  hosts other than the one owning the daemon is unsupported — Epic 06 gates on it.
- Any change to `deploy/k8s/` (Daytona-only; see index Dependency Notes).

## Backend Tasks

1. **Dependency**: add `docker>=7` to `backend/pyproject.toml` (uv). Client from
   `docker_host` config (default `unix:///var/run/docker.sock`; Podman users point it at
   their socket — document `podman system service`).
2. **`DockerRuntime.provision(workspace_id, owner_id, snapshot)`**:
   - Image = `snapshot or settings.docker_sandbox_image` (default `enigma-sandbox:latest`).
     Missing image → `ProvisioningError` telling the operator to run
     `sandbox/build-local-image.sh` (no implicit multi-minute builds inside API requests).
   - Idempotent by container name `execute-<uuid.hex>` (same convention as Daytona):
     if a container with that name exists, adopt it.
   - `containers.run(image, detach=True, name=..., labels={"enigma.managed": "1",
     "enigma.workspace-id": ..., "enigma.owner-id": ...}, command=<long-sleep init>,
     working_dir=<home>, network=settings.docker_sandbox_network,
     mem_limit/nano_cpus from config)`. The container's PID 1 is a `sleep infinity`
     equivalent; all real work happens via exec (OpenHands-style).
   - Create `workspace_path` inside the container (parity with
     `fs.create_folder(workspace_path, "755")` in the Daytona provision).
   - Return the container id (stored in `workspace.sandbox_id` by `workspaces.py`).
3. **Lifecycle**: `status` maps Docker states — `running→ready`, `created/restarting→
   creating`, `paused/exited→stopped`, `dead/not found→error` (404 →
   `SandboxNotFoundError`). `start` = `container.start()` (used by the shared
   ensure-started decorator so any op on a stopped container auto-starts it, matching
   `_ensure_started`). `delete` = `container.remove(force=True)`. `health` =
   `client.ping()` + daemon version string. `configured` = socket reachable OR
   `docker_host` explicitly set (surfacing lands in Epic 06).
4. **`exec`**: `container.exec_run(["/bin/sh","-lc", command], workdir=cwd,
   environment=env, demux=False)` in `to_thread`, with an asyncio timeout that, on
   expiry, kills the exec'd process group inside the container (spawn via `setsid`,
   `kill -TERM -<pgid>`) and raises `CommandTimeoutError`. Return
   `RuntimeCommandResult(exit_code, stdout=<combined>, stderr="")` — mirroring the
   Daytona quirk exactly.
5. **`stream_command`**: `exec_run(stream=True, demux=True)` consumed on a thread,
   chunks forwarded to `on_stdout`/`on_stderr` via `loop.call_soon_threadsafe`; exit code
   from `exec_inspect`. Honors the same timeout semantics. This powers `commands.py:697`
   (command runs, inline or in the worker) and the service log streams in
   `workspace_services.py`
   (WebSocket custom commands) unchanged.
6. **Env/home parity**: run as the image's non-root user with home matching
   `daytona_sandbox_home` (introduce provider-neutral `sandbox_home` config read by
   both). `PATH` must include `~/.local/bin` (mise/code-server/specify) — same as the
   snapshot.
7. **Config** (`core/config.py`): `docker_host`, `docker_sandbox_image`,
   `docker_sandbox_network` (default `enigma-sandboxes`), `docker_sandbox_cpus` (2),
   `docker_sandbox_memory_mb` (4096), `docker_provision_timeout_seconds` (60). Mirror in
   `.env.example` and `docker-compose.yml`.
8. **Compose wiring**: mount the Docker socket into the **backend**, **worker** and
   **preview-proxy** services (`/var/run/docker.sock:/var/run/docker.sock`); add the
   first `networks:` section to `docker-compose.yml` declaring the external
   `enigma-sandboxes` network and attach those three services to it. Document the
   Podman socket variant (`podman system service`). (Sandbox containers themselves NEVER
   get the socket.) `tests/test_deployment.py::test_compose_keeps_building_the_dev_targets`
   must stay green.
9. **Image**: `sandbox/Dockerfile` — start from the current `daytona/Dockerfile` content
   with a neutral base (e.g. `ubuntu:24.04`, or keep `daytonaio/sandbox:0.8.0` if it runs
   cleanly locally — decide during implementation; the requirement is only: non-root
   user, git, curl, **openssh-client** (SSH deploy keys), tmux (for Epic 03), mise,
   code-server, uv+specify, same `$HOME/.local/bin` layout).
   `sandbox/build-local-image.sh` builds/tags `enigma-sandbox:latest`;
   `daytona/build-snapshot.sh` switches to `--dockerfile ../sandbox/Dockerfile`.
10. **Factory**: register `"docker"` in `factory.py`. The worker's `ctx["sandbox_runtime"]`
    (ex `ctx["daytona_runtime"]`) and `close_all()` on worker shutdown (`worker.py:395`)
    already go through the factory after Epic 01, so no worker code changes are expected
    beyond the socket mount.
11. **Metrics**: if exec/provision timings get a counter or histogram, put it in
    `core/metrics.py` with bounded labels (`provider`, `status`), never a workspace id
    (AGENTS.md gotcha 26).
12. **Tests**: `FakeDockerClient` fixture added to the Epic-01 conformance suite; unit
    tests for state mapping, idempotent provision-by-name, timeout kill, stream chunking.
    Integration suite `tests/test_docker_integration.py` gated on
    `DOCKER_INTEGRATION_TESTS=1` (needs a real daemon + built image): provision → exec
    `echo` → stream a multi-line command → stop/start → delete.

## Frontend Tasks

None in this epic (workspace creation still defaults to Daytona; the picker ships in
Epic 06).

## Database Tasks

None. `workspace.sandbox_id` (alias of `daytona_sandbox_id`) stores the container id;
`workspace.snapshot` stores the image name; `workspace.runtime="docker"` discriminates.

## Security Requirements

- **The Docker socket is root-equivalent on the host.** Only the backend, worker and
  preview-proxy services touch it; it must never be mounted, bind-mounted, or reachable
  from a sandbox container. Add a provision-time assertion that the sandbox container spec contains no
  socket mounts and no `privileged` flag.
- Containers run as a non-root user, on the dedicated bridge network, never
  `network_mode=host`, no added capabilities (the full profile lands in Epic 05, but
  these floors apply from day one).
- All commands still flow through the untouched `agent_tools.py` safety model; the
  denylist keeps `docker`/`docker-compose` blocked *inside* the sandbox.
- Never log full container inspect payloads (they can include env values).

## Acceptance Criteria

- With a built image and `runtime="docker"`: workspace create provisions a labelled
  container; workspace status reports `ready`; stop the container out-of-band and the
  next `exec` auto-starts it (parity with `_ensure_started`).
- An agent run whose tool call is `run_command("echo hello")` streams output over the
  existing WebSocket and records success — on Docker.
- `commands.py` streaming works on a Docker workspace in **both** execution modes —
  `RUN_EXECUTION_MODE=inline` and `queue` (run executed by the compose `worker`) —
  with multi-line output, correct exit code; timeout raises `CommandTimeoutError` and the
  in-container process is dead afterwards.
- Conformance suite green for both providers' fakes; Daytona test expectations unchanged.
- Docker daemon down: workspace create on `runtime="docker"` returns a clear 503-style
  error (`RuntimeUnavailableError`); Daytona workspaces are entirely unaffected.
- Deleting the workspace removes the container; no `enigma.managed` containers leak after
  the integration suite.

## Dependencies

- **Epic 01** (interface + factory) — hard dependency.
- Coordinates with `hybrid-runtime-migration/02` only in that its ephemeral tier will map
  to auto-remove (`--rm`) containers *later*; nothing to build for it now.

## Future Enhancements

- Warm pool of pre-created containers to cut provision latency to near zero.
- Image variants per stack (node-only, python-only) selected via `workspace.snapshot`.
- Podman rootless as a first-class tested profile (currently: documented, best-effort).
