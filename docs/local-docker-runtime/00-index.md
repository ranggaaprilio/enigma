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
- `docs/horizontal-scale/` — scales the **control plane** (FastAPI replicas, jobs, bus).
  The Docker runtime is deliberately a **single-host** execution option (local dev,
  self-host, small teams); multi-node container scheduling is explicitly out of scope
  here (see Decision Log). Nothing in this series adds process-local state beyond what
  `DaytonaRuntime` already has, so it does not block that series.
- `docs/copilot-workspace-parity/` — pure orchestration/UX above the runtime; it only
  requires that everything executes through the runtime layer, which this series preserves.

## Why This Change

Today `backend/app/services/daytona_runtime.py` is the only execution path. If the Daytona
API key is absent or the subscription lapses, **nothing runs**: no agent tools, no terminal,
no preview. Yet the codebase is one refactor away from provider-neutrality:

- `Workspace.runtime` already exists as a discriminator column (default `"daytona"`,
  `models/workspace.py:51`) — added by migration `0011_daytona_runtime.py` but currently
  hardcoded at `services/workspaces.py:131`.
- `DaytonaRuntime` already exposes provider-neutral DTOs (`RuntimeCommandResult`,
  `RuntimePreviewLink`) and is injected everywhere through a single point
  (`get_daytona_runtime()`, `daytona_runtime.py:354`).
- The sandbox image is already a Dockerfile (`daytona/Dockerfile`, base
  `daytonaio/sandbox:0.8.0` + mise + code-server + uv/specify) — directly reusable as a
  local image.
- The test suite already contains hand-written fakes (`FakeSandbox`, `FakeFileSystem`,
  `FakeProcess`, `FakeRuntime`) that are effectively a written-down spec of the interface.

## Target Architecture

```
                 Workspace.runtime = "daytona" | "docker"
                                 │
                    ┌────────────▼─────────────┐
                    │  get_runtime(workspace)   │   services/runtime/factory.py
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
`dev_server`, `preview`, `speckit`, `commands`, `preview_proxy`) talks to the
`SandboxRuntime` interface only; nothing outside `services/runtime/` may import the
`daytona` or `docker` packages.

## The Epics

| # | Epic | Priority | Theme |
|---|------|----------|-------|
| 01 | [Runtime Provider Abstraction](./01-runtime-provider-abstraction.md) | **P1** | Foundation (pure refactor) |
| 02 | [Docker Runtime Core (lifecycle + exec)](./02-docker-runtime-core.md) | **P1** | The new provider |
| 03 | [Filesystem, Git & PTY Parity](./03-filesystem-git-pty-parity.md) | **P1** | Feature parity |
| 04 | [Preview, Editor & Dev Server on Docker](./04-preview-editor-devserver.md) | **P1** | Human surfaces |
| 05 | [Lifecycle, Isolation & Security Hardening](./05-lifecycle-security-isolation.md) | P2 | Safety / cost |
| 06 | [Provider Selection, Config & Rollout](./06-provider-selection-and-rollout.md) | P2 | Productization |

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
- **Coordination with `horizontal-scale`.** When the control plane goes multi-replica, a
  Docker-runtime workspace is pinned to the node whose daemon owns the container. This
  series must not pretend otherwise: Epic 06 records the limitation (docs + a
  `sandbox_node` note in Future Enhancements) and keeps the Docker provider valid for the
  single-node/self-host profile that series does not target.
- No dependency on `copilot-workspace-parity` or `improvement-epics` in either direction.

## Shared Rules For Every Epic

- **Daytona behaviour must not change.** Every epic keeps the Daytona path
  byte-for-byte equivalent; the shared interface conformance tests run against both
  providers' fakes.
- **Provider imports stay in `services/runtime/`.** No `import daytona` or `import docker`
  anywhere else (enforce with a lint/CI grep).
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

## Grounding: Current State Snapshot (August 2026)

- **One runtime module**: `backend/app/services/daytona_runtime.py` (356 lines) — the only
  module importing the `daytona` package (SDK pinned `daytona==0.185.0`). Singleton via
  `@lru_cache get_daytona_runtime()` (line 354). Errors: `DaytonaRuntimeError`,
  `DaytonaUnavailableError`, `DaytonaSandboxNotFoundError`, `DaytonaProvisioningError`,
  `DaytonaCommandTimeoutError` (lines 26–44).
- **Method surface** (all async, keyed off a `Workspace` ORM object): `configured`,
  `health`, `provision`, `get_sandbox`, `status`, `delete`, `exec`, `stream_command`,
  `create_or_connect_pty`, `kill_pty`, `signed_preview_url`, `preview_link`, `close`, plus
  internals `_ensure_started`, `_desired_network_settings`, `_apply_network_settings`.
- **The leak**: `get_sandbox()` (line 147) returns the raw SDK `Sandbox`; consumers then
  call SDK-native surfaces directly — `sandbox.fs.*` in `files.py`
  (118,145,161,169,213–331), `agent_tools.py` (147–219), `repositories.py` (642–660),
  `dev_server.py` (549,559); `sandbox.git.clone/status` in `repositories.py` (217,248);
  `PtySize` imported from `daytona` in `terminal.py:224`.
- **Preview proxy is already provider-friendly**: `preview_capability.py` mints an HMAC
  subdomain capability; `preview_proxy.py:247–317` resolves it, calls
  `runtime.preview_link(workspace, port)` per request, and forwards HTTP/WS with
  `X-Daytona-Preview-Token`. A `RuntimePreviewLink(url=..., token="")` makes the token
  header a harmless no-op.
- **DB**: `workspaces.runtime` (default `"daytona"`), `daytona_sandbox_id` (unique index)
  with provider-neutral alias property `Workspace.sandbox_id` (`models/workspace.py:82`),
  `snapshot`, `workspace_path`, `dev_autostart`. Sandbox-adjacent tables:
  `terminal_sessions.pty_session_id`, `editor_sessions` (`bind_host="daytona"`),
  `preview_sessions`, `workspace_worktrees`.
- **No local execution exists.** No `docker` SDK dependency; `docker`/`docker-compose`
  appear only in the agent command denylist (`agent_tools.py:40-41`). Docker/Podman is
  used only for the dev stack (`docker-compose.yml`: postgres, redis, backend :8000,
  preview-proxy :8001, frontend :5173) and for building the Daytona snapshot
  (`daytona/Dockerfile` + `build-snapshot.sh`).
- **Agent engine**: OpenHands SDK 1.28.0 runs *in the API process*; its tools are bridged
  to the sandbox via `openhands_tools.py`/`agent_tools.py` — Enigma does **not** use
  OpenHands' own runtime container. This series gives Enigma its own equivalent.

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
