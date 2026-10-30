# Local Docker Sandbox Runtime — Plan Index

This folder plans adding a **second sandbox provider**: a local **Docker container runtime**
(in the spirit of OpenHands' sandbox container), selectable per workspace alongside Daytona.
The goal is optionality — Enigma must run fully self-hosted on a single machine with plain
Docker/Podman when a Daytona subscription is not available, with the **same feature surface**
(agent runs, files, git, terminal, editor, preview, dev server, Spec Kit).

It complements the other planning folders and does **not** replace or overlap them:

- `docs/implementation-plan/` — the original MVP build (Epics 0–11).
- `docs/improvement-epics/` — post-MVP feature backlog. Untouched by this series.
- `docs/hybrid-runtime-migration/` — a **lifecycle** redesign *on Daytona* (ephemeral vs
  persistent tiers). That series explicitly says "keep Daytona, no provider swap". This
  series is the orthogonal **provider** dimension: *which engine* hosts a sandbox, not
  *when it lives or dies*. The two meet at one seam (see Dependency Notes) and can land in
  either order.
- `docs/horizontal-scale/` — **shipped (Epics 01–09)**. It scaled the control plane
  (API replicas, arq workers in `RUN_EXECUTION_MODE=queue`, Redis bus, leases, reaper,
  retention, stateless preview proxy, Kustomize deploy). The Docker runtime is
  deliberately a **single-host** execution option (local dev, self-host, small teams);
  multi-node container scheduling is explicitly out of scope here (see Decision Log).
  Three of its invariants now bind this series: (a) in queue mode **runs execute in the
  worker process**, so whatever reaches the Docker daemon must be reachable from the
  worker, not only the API; (b) process-local state is policed by tests
  (`tests/test_run_runtime.py`, `tests/test_preview_proxy_scaling.py`); (c) migrations
  after `0034` must be expand-only (`tests/test_migration_safety.py`).
- `docs/copilot-workspace-parity/` — pure orchestration/UX above the runtime; it only
  requires that everything executes through the runtime layer, which this series preserves.

## Why This Change

Today `backend/app/services/daytona_runtime.py` is the only execution path. If the Daytona
API key is absent or the subscription lapses, **nothing runs**: no agent tools, no terminal,
no preview. Yet the codebase is one refactor away from provider-neutrality:

- `Workspace.runtime` already exists as a discriminator column (default `"daytona"`,
  `models/workspace.py:68`) — added by migration `0011_daytona_runtime.py` but currently
  hardcoded at `services/workspaces.py:180`.
- `DaytonaRuntime` already exposes provider-neutral DTOs (`RuntimeCommandResult`,
  `RuntimePreviewLink`) and is injected everywhere through a single point
  (`get_daytona_runtime()`, `daytona_runtime.py:365`) — 31 modules import it, and the
  worker keeps it in `ctx["daytona_runtime"]` (`worker.py:231,330`).
- The sandbox image is already a Dockerfile (`daytona/Dockerfile`, base
  `daytonaio/sandbox:0.8.0` + mise + code-server + uv/specify) — directly reusable as a
  local image.
- The test suite already contains hand-written fakes (`FakeSandbox`, `FakeFileSystem`,
  `FakeProcess`, `FakeFs`, `FakeRuntime`, `FakeDaytona` — ~27 classes across 15 test
  files) that are effectively a written-down spec of the interface.

## Target Architecture

```
                 Workspace.runtime = "daytona" | "docker"
                                 │
                    ┌────────────▼─────────────┐
                    │  get_runtime(workspace)   │   services/sandbox/factory.py
                    │  (provider factory)       │
                    └─────┬──────────────┬─────┘
                          │              │
        ┌─────────────────▼───┐   ┌──────▼──────────────────┐
        │  DaytonaRuntime      │   │  DockerRuntime           │
        │  (existing, moved    │   │  (new: docker SDK,       │
        │   under a shared     │   │   containers labelled    │
        │   SandboxRuntime     │   │   enigma.workspace-id)   │
        │   interface)         │   │                          │
        └─────────┬───────────┘   └──────┬──────────────────┘
                  │                       │
         Daytona cloud sandbox     Local container on the
         (unchanged behaviour)     `enigma-sandboxes` bridge network
                                          │
                            preview-proxy reaches container:port
                            directly over the shared network
```

**Principle: one interface, two providers, zero behaviour change for Daytona.** Every
consumer (`agent_tools`, `files`, `repositories`, `worktrees`, `terminal`, `editor`,
`dev_server`, `workspace_services`, `workspace_init`, `workspace_env`, `preview`,
`run_detection`, `sandbox_probe`, `speckit`, `commands`, `agents`, `preview_proxy`,
`worker`) talks to the `SandboxRuntime` interface only; nothing outside
`services/sandbox/` may import the `daytona` or `docker` packages.

> **Package name.** Earlier drafts called the package `services/runtime/`. That name is
> taken: `services/runtime.py` is the horizontal-scale module holding `RunRegistry`,
> `EventBus` and `RunLock` (AGENTS.md gotcha 16). The sandbox-provider package is
> `services/sandbox/` throughout this series (`base.py`, `daytona.py`, `docker.py`,
> `factory.py`).

## The Epics

| # | Epic | Priority | Theme |
|---|------|----------|-------|
| 01 | [Runtime Provider Abstraction](./01-runtime-provider-abstraction.md) | **P1** — shipped | Foundation (pure refactor) |
| 02 | [Docker Runtime Core (lifecycle + exec)](./02-docker-runtime-core.md) | **P1** — shipped | The new provider |
| 03 | [Filesystem, Git & PTY Parity](./03-filesystem-git-pty-parity.md) | **P1** — shipped | Feature parity |
| 04 | [Preview, Editor & Dev Server on Docker](./04-preview-editor-devserver.md) | **P1** — shipped | Human surfaces |
| 05 | [Lifecycle, Isolation & Security Hardening](./05-lifecycle-security-isolation.md) | P2 — shipped (incl. egress allow-list + disk quota follow-ups) | Safety / cost |
| 06 | [Provider Selection, Config & Rollout](./06-provider-selection-and-rollout.md) | P2 — shipped (column contract: reads switched in `0038`, old column dropped in `0039`, deployed a release apart) | Productization |

## Recommended Build Order

1. **01 — Provider abstraction** first. A near-pure refactor of the Daytona path: define
   `SandboxRuntime`, close the raw-`Sandbox` leaks (`fs.*`, `git.*`, `PtySize`), add the
   factory. Ship it alone — if anything regresses on Daytona, this epic is the culprit.
2. **02 — Docker core.** Provision/start/stop/delete/status/exec/stream against the local
   Docker daemon; reuse the existing Dockerfile as the image. At the end of 02 an agent
   run (`run_command`) works in a local container.
3. **03 — FS/Git/PTY parity.** File tools, git-over-CLI, and a reconnectable terminal
   (tmux-backed). At the end of 03 the agent + terminal are at full parity.
4. **04 — Preview/editor/dev-server.** Wire `preview_link` to the shared Docker network so
   the existing capability proxy works unchanged; code-server; port detection.
5. **05 — Hardening.** Idle reaper (auto-stop parity), resource limits, egress policy,
   orphan reconciliation, container security profile.
6. **06 — Rollout.** Runtime picker at workspace creation, health/status surfacing,
   compose/socket wiring, docs, integration test suite.

## Dependency Notes

- **01 blocks everything.** 02→03→04 are sequential (each builds on the previous provider
  surface). 05 and 06 can overlap once 04 lands.
- **Coordination with `hybrid-runtime-migration` (the one shared seam).** Its Epic 01
  proposes `services/runtime/router.py` with `sandbox_for(workspace, intent)` (tier
  routing). This series' factory (`get_runtime(workspace)` — provider routing) sits
  *beneath* that router: tier decides *which sandbox*, provider decides *which engine*.
  Whichever series lands first must keep the two axes separate — do not merge tier and
  provider into one enum. Docker's ephemeral-tier equivalent is a `--rm` container; that
  mapping belongs to hybrid Epic 02, not here.
- **Coordination with `horizontal-scale` (shipped).** A Docker-runtime workspace is
  pinned to the host whose daemon owns the container. Consequences this series must
  honour rather than paper over:
  - In `RUN_EXECUTION_MODE=queue` any worker may claim any run, so the Docker provider
    is supported only where API, worker(s) and preview proxy all share **one** daemon
    (the compose stack / a single self-host box). Epic 06 validates that at creation
    time and documents it; a multi-worker fleet on separate hosts is not a supported
    Docker profile.
  - The Kustomize production overlay (`deploy/k8s/`) stays Daytona-only: its workloads
    run non-root with a read-only root FS and strict egress network policies
    (`tests/test_deployment.py::test_workloads_are_locked_down`), so a Docker-socket
    `hostPath` mount is out of the question there. Docker lives in `docker-compose.yml`
    and the self-host docs, never in `deploy/k8s/`.
  - Epic 06 records both limits (docs + a `sandbox_node` note in Future Enhancements).
- No dependency on `copilot-workspace-parity` or `improvement-epics` in either direction.

## Shared Rules For Every Epic

- **Daytona behaviour must not change.** Every epic keeps the Daytona path
  byte-for-byte equivalent; the shared interface conformance tests run against both
  providers' fakes.
- **Provider imports stay in `services/sandbox/`.** No `import daytona` or `import docker`
  anywhere else (enforce with a source-grep test, the same shape as
  `tests/test_run_runtime.py::test_no_call_site_reaches_into_process_local_registries`).
- **No new process-local state.** Idle clocks, container caches and the like live in
  Postgres (transaction-scoped locks only — `pg_advisory_xact_lock`, never session
  locks, per AGENTS.md gotcha 22) or in Redis, never in a module-level dict.
- Land behind config: `sandbox_runtime_default` stays `"daytona"` until Epic 06 flips the
  self-host profile; per-workspace `runtime` is explicit opt-in before that.
- A DB migration for every schema change (Alembic, async engine).
- Backend tests (`pytest`) for the interface + both providers; run one shared conformance
  suite parametrized over providers. Frontend tests (Vitest) for runtime status states.
- **Preserve the agent command-safety model** in `agent_tools.py` verbatim (denylist —
  including `docker`/`docker-compose` — allowlist, shell-operator blocking, path
  confinement, output truncation). Containers NEVER get the Docker socket.
- Never log PATs/secrets; keep `sanitize_git_output` in every git path; git credentials go
  via askpass/env, never argv.

## Grounding: Current State Snapshot (October 2026, verified against `main`)

- **One runtime module**: `backend/app/services/daytona_runtime.py` (366 lines) — the only
  module importing the `daytona` package besides `terminal.py:6` (`PtySize`). SDK pinned
  `daytona==0.185.0`. Singleton via `@lru_cache get_daytona_runtime()` (line 365).
  Errors: `DaytonaRuntimeError`, `DaytonaUnavailableError`,
  `DaytonaSandboxNotFoundError`, `DaytonaProvisioningError`,
  `DaytonaCommandTimeoutError` (lines 27–45), caught by name in 15 modules.
- **Method surface** (all async, keyed off a `Workspace` ORM object): `configured`,
  `health`, `provision` (returns the raw SDK `Sandbox`; `workspaces.py:218` reads
  `.id`), `get_sandbox` (154), `status` (165), `delete` (177), `exec` (188),
  `stream_command` (215), `create_or_connect_pty` (270), `kill_pty` (296),
  `signed_preview_url` (305 — **no callers left**; the editor moved to a stable alias
  host), `preview_link` (316), `close`, plus internals `_ensure_started` (324),
  `_desired_network_settings` (331), `_apply_network_settings` (345).
- **The leak**: `get_sandbox()` returns the raw SDK `Sandbox`; consumers then call
  SDK-native surfaces directly — `sandbox.fs.*` in `files.py` (118–337),
  `agent_tools.py` (149–221), `repositories.py` (326–892), `dev_server.py` (254–470),
  `workspace_services.py` (1103–1129), `workspace_init.py` (115–147),
  `workspace_env.py` (181–190). Ops used: `list_files`, `get_file_info`,
  `download_file`, `upload_file`, `create_folder`, `move_files`, `delete_file`,
  `find_files`, **`set_file_permissions`** (SSH deploy keys `repositories.py:826`,
  `.env` materialisation `workspace_env.py:190`). SDK git is down to one call,
  `sandbox.git.status` (`repositories.py:368`); clone/pull/push/fetch already run
  through the git CLI with per-command auth (token or SSH deploy key,
  `_run_ssh_git`). `PtySize` is imported from `daytona` in `terminal.py:6` (used at
  230). `workspace.daytona_sandbox_id` is read directly in 11 modules instead of the
  `sandbox_id` alias.
- **Preview proxy is already provider-friendly** and, since horizontal-scale Epic 07,
  **stateless**: `preview_capability.py` mints an HMAC subdomain capability;
  `preview_alias.py` / `editor_alias.py` add stable per-workspace preview and editor
  hosts gated by grant cookies; `preview_proxy.py` resolves them (`resolve` 152,
  `resolve_alias` 183, `resolve_editor_alias` 241 — each requires
  `workspace.daytona_sandbox_id`), calls `runtime.preview_link(workspace, port)` per
  request (1056, 1070, 1121) and forwards HTTP/WS (`proxy_websocket` 615) with
  `X-Daytona-Preview-Token` / `X-Daytona-Skip-Preview-Warning` (1095, 1123). A
  `RuntimePreviewLink(url=..., token="")` makes the token header a harmless no-op.
  `tests/test_preview_proxy_scaling.py:218` fails the build if the module gains
  module-level state; the proxy owns its own `AppLifecycle` and its `/health` calls
  `proxy_runtime.health()` (411).
- **DB**: `workspaces.runtime` (default `"daytona"`), `daytona_sandbox_id` (unique index)
  with provider-neutral alias property `Workspace.sandbox_id` (`models/workspace.py:115`),
  `snapshot`, `workspace_path`, `dev_autostart`. Sandbox-adjacent tables:
  `terminal_sessions.pty_session_id`, `editor_sessions` (`bind_host="daytona"`,
  `editor.py:124`), `preview_sessions`, `workspace_preview_aliases`,
  `workspace_services` (multi-service preview, Epic 11), `workspace_worktrees`.
  Migration head is `0035_run_correlation`; anything after `0034` is expand-only.
- **No local execution exists.** No `docker` SDK dependency; `docker`/`docker-compose`
  appear only in the agent command denylist (`agent_tools.py:40-41`). Docker/Podman is
  used only for the dev stack (`docker-compose.yml`: postgres, redis, pgbouncer, backend
  :8000, worker, preview-proxy :8001, frontend :5173 — **no `networks:` section yet**)
  and for building the Daytona snapshot (`daytona/Dockerfile`, base
  `daytonaio/sandbox:0.8.0` + mise + code-server + uv/specify; `build-snapshot.sh`).
- **Execution topology**: `RUN_EXECUTION_MODE=inline` (default) runs agent/command runs
  in the API process; `queue` mode enqueues them (arq) and `app/worker.py` executes them,
  with leases, a reaper (`services/reaper.py`) and retention (`services/retention.py`) as
  the established background-task patterns. Both `main.py:94` and `worker.py:395` close
  the Daytona singleton on shutdown; `preview_proxy.py:341` closes its own instance.
- **Agent engine**: OpenHands SDK 1.28.0 runs *in the API or worker process*; its tools
  are bridged to the sandbox via `openhands_tools.py`/`agent_tools.py` — Enigma does
  **not** use OpenHands' own runtime container. This series gives Enigma its own
  equivalent.
- **Frontend** still says "Daytona" in copy: `CreateWorkspaceWizard.tsx:111,475`,
  `TeamRoute.tsx:323,420,441`, `DashboardOverviewRoute.tsx:25`,
  `DashboardRoute.tsx:519,593`.

## Decision Log

- **Second provider, not a replacement.** Daytona remains the default and the
  production-cloud path; Docker is the self-host/local/no-subscription path. Per-workspace
  choice via the existing `Workspace.runtime` column.
- **Interface extraction over emulation.** We refactor call sites to a
  `SandboxRuntime`/`RuntimeFileSystem` interface instead of building a fake Daytona
  `Sandbox` object for Docker. The SDK object graph leaks are few and enumerable.
- **Exec + tar over an in-container action server (for now).** OpenHands runs an HTTP
  action-execution server inside its runtime container; we start with `docker exec` + tar
  archives (no in-container daemon, no extra port, works on a stock image) and keep the
  action-server as a Future Enhancement if exec overhead ever hurts.
- **tmux for PTY reconnect.** Docker exec sessions are not reconnectable; Daytona's named
  PTY sessions are emulated with `tmux new-session -A -s <session_id>` inside the
  container (tmux baked into the image).
- **Preview via shared Docker network, not published host ports.** The preview proxy
  already runs in the compose stack; sandbox containers join a dedicated bridge network
  and the proxy dials `container:port` directly. No port-publishing races, no host
  firewall surface.
- **Official `docker` Python SDK wrapped in `asyncio.to_thread`** (the codebase's existing
  pattern for sync work) rather than `aiodocker` — the official SDK is more complete
  (exec resize, put/get archive) and better maintained.
- **Single host by design.** Multi-node container scheduling (Swarm/K8s/Nomad) is out of
  scope; that need is served by Daytona + `horizontal-scale`.
