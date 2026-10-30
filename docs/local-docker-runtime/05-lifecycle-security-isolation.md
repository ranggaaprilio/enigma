# Epic 05 - Lifecycle, Isolation & Security Hardening

## Goal

Give the Docker provider the operational safety net Daytona provides as a managed
service: idle auto-stop and orphan cleanup (Daytona's `auto_stop_interval` /
`auto_delete_interval` equivalents), resource limits, an egress policy matching
`network_block_all` / `network_allow_list`, and a locked-down container security
profile. After this epic a laptop or self-hosted box running many sandboxes stays
healthy, bounded, and safe.

## User Value

You can leave Enigma running locally without babysitting it: idle sandboxes stop and
free RAM/CPU, crashed-backend leftovers are reaped on restart, a runaway `npm install`
can't eat the machine, and agent code can't scan your LAN or touch the Docker daemon.

## Current State

- Daytona-side lifecycle is declarative at provision time
  (ex `daytona_runtime.py:125-140`): `auto_stop_interval` (=
  `daytona_auto_stop_minutes`, 30), `auto_archive_interval=0`, `auto_delete_interval=-1`,
  `ephemeral=False` — the platform enforces them. Docker has **no equivalent**; the
  provider must enforce them itself.
- Egress policy on Daytona: `_desired_network_settings` / `_apply_network_settings`
  (ex `daytona_runtime.py:331-362`) driven by `daytona_network_block_all` and
  `daytona_network_allow_list`. Epic 02 put Docker sandboxes on one bridge network with
  no policy.
- Epic 02 set security floors (non-root, no socket, no privileged, fixed cpu/mem) but no
  reaper, no pids/disk limits, no seccomp/cap profile, no startup reconciliation.
- **Background-maintenance scaffolding now exists** (horizontal-scale): the API
  lifespan (`main.py:57-104`) runs `reconcile_interrupted_runs` on startup and
  `start_retention` as a periodic task; the worker (`app/worker.py`) additionally runs
  `run_reaper` (`services/reaper.py`, lease expiry → `interrupted_worker_died`) and
  reconciles only its own `worker_id`. Those are the patterns to copy: a periodic
  asyncio task started from the lifespan, Postgres as the coordination point,
  `FOR UPDATE SKIP LOCKED` / `pg_advisory_xact_lock` instead of in-process flags, and
  bounded-label metrics in `core/metrics.py`. That reconciliation is for *runs*; this
  epic's is for *containers* — a different object, coordinated below.
- Activity signals that should reset an idle clock already flow through the runtime
  layer: `exec`/`stream_command`/PTY traffic, preview proxy requests, editor sessions —
  but they originate in **three processes** (API, worker, preview proxy), so the clock
  cannot live in memory.

## Scope

**In scope**

- Idle tracking + auto-stop reaper for Docker sandboxes; startup orphan reconciliation.
- Resource limits: CPU, memory, pids, disk (where the storage driver allows), ulimits.
- Container security profile: cap-drop, no-new-privileges, seccomp default, tmpfs /tmp.
- Egress policy for the sandbox network honoring `network_block_all` + allowlist
  semantics.
- Config + observability (structlog events, counts in health output).

**Out of scope**

- Billing/cost accounting (Daytona-specific; local containers are free).
- Ephemeral per-run sandboxes (that is `hybrid-runtime-migration/02`; this epic only
  makes sure nothing here conflicts with a future `--rm` container mode).
- Multi-node placement or capacity scheduling.

## Backend Tasks

1. **Idle tracking**: `DockerRuntime` records `last_activity_at` per sandbox on every
   `exec` / `stream_command` / PTY byte / `preview_link` call, in a lightweight table
   `sandbox_activity(workspace_id PK/FK, last_activity_at timestamptz)` — DB, not a
   container label and not a module-level dict (`tests/test_run_runtime.py` style
   guards apply; the proxy's AST guard forbids in-module caches outright). Debounce to
   at most one write per 30s **per process** using instance state on the runtime
   object, never module state; the proxy's debounce window lives on its
   `app.state.runtime` instance.
2. **Reaper task**: a periodic asyncio task modelled on `services/retention.py` /
   `services/reaper.py`, started from the lifespan only when the Docker provider is
   configured — in the API in `inline` mode, in each worker in `queue` mode (same split
   retention uses). Every `docker_reaper_interval_seconds` (60), stop `enigma.managed`
   containers idle beyond `sandbox_auto_stop_minutes` (a neutral setting defaulting to
   the value of `daytona_auto_stop_minutes`, 30). Stopped containers are *kept* (parity
   with Daytona auto-stop, not delete); the ensure-started semantics from Epic 02
   transparently restart on next use.
3. **Orphan reconciliation on startup**: list containers labelled `enigma.managed=1`,
   cross-check `workspaces.sandbox_id`; containers with no matching workspace row are
   removed (log + counter); workspaces whose container vanished get `status="error"`
   surfaced by the existing status endpoint. Serialise concurrent passes (uvicorn
   `--reload`, API + worker starting together) with `pg_advisory_xact_lock` inside one
   transaction — never a session-level advisory lock or an in-process flag (PgBouncer
   rule, AGENTS.md gotcha 22).
4. **Resource limits** (provision-time, from config): `nano_cpus`, `mem_limit` +
   `memswap_limit` (= mem, no swap abuse), `pids_limit` (default 512), `ulimits`
   (nofile), and `storage_opt={"size": ...}` guarded by a driver check (overlay2 +
   xfs/btrfs only — degrade to a documented warning elsewhere). Disk fallback: the
   reaper enforces a soft quota via `du` on the workspace dir and marks the workspace
   with a warning status.
5. **Security profile** (provision-time): `cap_drop=["ALL"]` +
   `cap_add=["CHOWN","SETUID","SETGID","FOWNER","DAC_OVERRIDE","KILL"]` (validate the
   minimal set that keeps git/mise/code-server working),
   `security_opt=["no-new-privileges:true"]` + default seccomp, `init=True` (zombie
   reaping for the sleep-PID1), non-root user, `read_only=False` (workspace writes) but
   `tmpfs` for `/tmp`. Encode as one `container_security_spec()` helper with unit tests,
   used by every provision path.
6. **Egress policy**: honor neutral settings `sandbox_network_block_all` /
   `sandbox_network_allow_list` (aliases of the `daytona_*` ones):
   - `block_all=True` → attach sandboxes to an **internal** bridge network
     (`internal=True`; proxy/backend still reach them over it) — no outbound at all.
   - allowlist mode → outbound only via an egress HTTP(S) proxy container with a domain
     allowlist (`HTTP_PROXY`/`HTTPS_PROXY` baked into sandbox env), or a documented
     `DOCKER-USER` iptables helper. Pick ONE supported path during implementation;
     recommendation: the egress-proxy container — portable across Docker/Podman, no
     host firewall mutation.
   - Default profile (`block_all=False`, empty allowlist) = unrestricted outbound,
     matching local-dev expectations; document the difference vs Daytona defaults.
7. **Observability**: structlog events for reap/stop/remove/limit-hit (`State.OOMKilled`
   surfaces as a distinct workspace status detail); counters in `core/metrics.py` with
   bounded labels (`provider`, `reason`) only — never a workspace id; health output
   includes managed-container count and reaper liveness timestamp.
8. **Config**: `sandbox_auto_stop_minutes`, `docker_reaper_interval_seconds`,
   `docker_sandbox_pids_limit`, `docker_sandbox_disk_gb`, `sandbox_network_block_all`,
   `sandbox_network_allow_list` — mirrored in `.env.example`; Daytona keeps consuming
   its existing names via aliases.
9. **Tests**: reaper unit tests with a fake clock (idle → stopped; active → untouched;
   stopped → not re-stopped); reconciliation tests (orphan container, orphan workspace
   row); security-spec snapshot test (exact container HostConfig); integration
   (`DOCKER_INTEGRATION_TESTS=1`): fork bomb capped by pids_limit, OOM surfaces as an
   error detail, `block_all` container cannot `curl` out but preview still works.

## Frontend Tasks

- Surface the auto-stopped state distinctly: the workspace card/status already renders
  `stopped`; add "will wake on next use" hint copy for Docker workspaces (reuse the
  Daytona auto-stop copy if present; Vitest state test).

## Database Tasks

- One expand-only migration adding `sandbox_activity` (`workspace_id` FK/PK,
  `last_activity_at timestamptz`); numbered after the current head `0035` and clean
  under `tests/test_migration_safety.py`.

## Security Requirements

- The reaper and reconciliation only ever touch containers labelled
  `enigma.managed=1` — never arbitrary containers on the host (test with a decoy
  container).
- The egress proxy (if chosen) must not be reachable from outside the sandbox network
  and must log denied domains without full URLs (query strings can carry secrets).
- The capability set is the tested minimum; adding a capability requires a comment +
  test justifying it.
- The security spec is asserted at provision time — a drifted/hand-edited container
  failing the assertion is refused (error status), not silently adopted.
- Nothing here weakens the Epic-02 floors: no socket, no privileged, no host network,
  non-root.

## Acceptance Criteria

- A Docker workspace idle for `sandbox_auto_stop_minutes` is stopped by the reaper;
  opening the terminal transparently restarts it; an active PTY keeps it alive.
- Kill the backend mid-run, restart: orphaned containers are reconciled per task 3; no
  `enigma.managed` container survives without a workspace row.
- A fork bomb inside the sandbox hits `pids_limit` without harming the host; a memory
  hog is OOM-killed and the workspace shows an actionable error detail.
- With `sandbox_network_block_all=true`: `curl https://example.com` fails inside the
  sandbox while agent tools, terminal, and preview all still function as configured.
- Daytona workspaces: zero behaviour change (the reaper ignores them; their lifecycle
  stays declarative).

## Dependencies

- **Epic 02** (provision/lifecycle) hard; **Epics 03–04** should be landed so the
  activity-signal list is complete (PTY + preview traffic).
- **Coordination, not overlap, with the shipped horizontal-scale reconcilers**
  (`services/reconciliation.py` for runs, `services/reaper.py` for leases,
  `services/retention.py` for data) — different objects, same lifespan hooks in
  `main.py` and `worker.py`; add the container reaper beside them following the
  inline/queue split retention already uses.
- Ready to serve `hybrid-runtime-migration/05` later: its ephemeral-cleanup reaper can
  extend this one rather than duplicate it (shared label scheme).

## Future Enhancements

- gVisor (`runsc`) or Kata runtime class as opt-in `docker_sandbox_runtime` config for
  stronger kernel isolation.
- Auto-delete tier (`auto_delete_interval` parity) for long-abandoned stopped
  containers, with a grace notification.
- Disk quotas via xfs project quotas on supported hosts; image GC for stale sandbox
  image versions.

## Implementation Notes (shipped)

Built on the Epic 04 branch. Verified on Podman 5.8: unit suites (fakes), 25 real-daemon
tests (`DOCKER_INTEGRATION_TESTS=1`, 8 new), and 6 full-stack e2e tests
(`DOCKER_STACK_E2E=1`, 2 new) through a compose backend and proxy. Plan decisions at Gate 1:
sibling isolation pulled into this epic; egress allow-list and disk quota deferred.

How each acceptance criterion is verified:

- **Idle stop, wake, PTY keep-alive**: the reaper against a real container and real
  `sandbox_activity` rows — an attached terminal spares a sandbox idle past the window, a
  detached tmux session does not, past `SANDBOX_BUSY_MAX_MINUTES` even an attached one is
  stopped (real-daemon); a terminal WebSocket on a stopped sandbox starts it and echoes
  (full stack).
- **Backend killed and restarted**: the compose backend is SIGKILLed with a loopback-only
  service previewing; while it is down a second workspace's row is deleted and the proxy is
  disconnected from the sandbox network. After restart the orphan's container and network
  are gone, the live sandbox is untouched and `ready`, and its preview serves again through
  the re-attached proxy and the existing relay (full stack). Agent runs interrupted by the
  restart are the horizontal-scale reconciler's (`services/reconciliation.py`), covered by
  its own tests.
- **Fork bomb / OOM**: real-daemon; the OOM detail reaches `GET /workspaces/{id}/status`
  but not yet the UI (below).
- **`block_all`**: outbound HTTP and outside DNS fail; the agent tools (write, read,
  search, run, git status/diff) and a terminal work; a network command fails as a plain
  non-zero exit; the proxy still reaches the preview (real-daemon).
- **Daytona untouched**: unit tests — the reaper skips `runtime="daytona"` workspaces, and
  the upkeep loop never starts in a Daytona-default process.

- **Isolation (added to scope)**: every sandbox gets its own bridge network
  `enigma-sbx-<workspace>` with an explicit /28 carved from `DOCKER_SANDBOX_SUBNET_POOL`
  (default `10.213.0.0/16`, ~4096 sandboxes; Docker's default pools allow ~31 networks),
  skipping subnets any other network uses; only a "taken subnet" error is retried, anything
  else is raised. The backend (the socket holder) attaches this stack's preview proxy to
  each network under `DOCKER_PREVIEW_PROXY_HOST`; proxies are found by
  `DOCKER_PREVIEW_PROXY_LABEL` **and** the backend's own compose project (it inspects its
  own container), so another stack's proxy is never attached. Each upkeep pass re-attaches
  (compose drops those connections when it recreates the proxy). Sandboxes still on the
  legacy shared network move on provision or the next pass; a sandbox on any other network
  is refused. Sibling sandboxes can neither resolve nor reach each other (real-daemon
  test); the proxy runs with `net.ipv4.ip_forward=0`. The shared `enigma-sandboxes`
  network is retired from compose.
- **Installation scoping (security/code review, HIGH/M1)**: migration `0036` also creates
  `sandbox_installation`, one random id per database. Sandboxes and networks carry it as
  `enigma.installation`, and the upkeep loop only lists, re-wires or removes its own — two
  stacks on one daemon (a second checkout, a test stack, a restored database) never treat
  each other's sandboxes as orphans. Containers created before this epic lack the label:
  they are never removed as orphans, but each upkeep pass still moves one onto its own
  network when this database names it as a workspace's sandbox (same workspace id and
  container id) — so no sandbox of ours stays on the old shared network (post-commit
  security scan). One this database does not know is left alone: it may be another stack's.
- **Security profile**: `container_security_spec()` is the single source (cap_drop ALL and
  nothing added, no-new-privileges, init, pids, CPU, memory with `memswap = memory`,
  `/tmp` tmpfs of `DOCKER_SANDBOX_TMP_MB`, `nofile` ulimit). Adopting an existing
  container runs `assert_safe_container` on its inspect output (privileged, cap_add,
  no-new-privileges, unconfined or custom seccomp/AppArmor, `label=disable`, devices,
  sysctls, published ports, mounts, root user, host namespaces) and then reads the live
  process's `CapEff`/`CapBnd`, which must be zero — inspect formats differ (Docker reports
  `ALL`, Podman lists each capability). Outdated resource limits on a legacy container are
  not refused.
- **Idle clock + reaper**: `sandbox_activity` (migration `0036`) is upserted with
  `clock_timestamp()` by `SandboxActivityRecorder`, debounced 30 s per runtime instance,
  from provision, exec, command streams, terminal output and `preview_link` (so the API,
  worker and proxy all count). Every `DOCKER_REAPER_INTERVAL_SECONDS` the upkeep loop stops
  (keeps) sandboxes idle past `SANDBOX_AUTO_STOP_MINUTES` (0 = never): the selection runs
  under `pg_try_advisory_xact_lock`, each stop happens outside the transaction after an
  idle re-check, and a sandbox with a live exec or attached terminal (pidfiles under `/tmp`,
  checked with shell builtins only) is skipped, so a long quiet build is not stopped — but
  only until it has been idle `SANDBOX_BUSY_MAX_MINUTES` (240): the probe reads the
  sandbox's own `/tmp`, which its tenant can fake (post-commit security scan). Any use wakes
  it again (Epic 02's ensure-started). An open dashboard's status polls count as activity.
- **Orphans (startup)**: under the lock, containers and networks of this installation
  whose workspace row is gone are removed; workspaces whose container vanished become
  `error`. Containers without the managed label are never touched (decoy test).
- **Where it runs**: `start_sandbox_maintenance` from the API lifespan (inline mode) or each
  worker (queue mode), only when `SANDBOX_RUNTIME_DEFAULT=docker` (what the compose
  override sets) and a daemon is reachable — never in a Daytona-default process, the test
  suite's app included.
- **OOM**: the kernel kills the biggest process, not PID 1, so the container keeps running
  and `State.OOMKilled` stays false. `status_detail` (new on the runtime interface; Daytona
  returns None) reads the cgroup's `oom_kill` counter with an absolute `/bin/cat` (the
  tenant owns `~/.local/bin`, first on PATH) and `GET /workspaces/{id}/status` returns it as
  `status_detail`; a failure to read it never turns a healthy status into `error`.
- **Egress**: `SANDBOX_NETWORK_BLOCK_ALL=true` makes each sandbox network `internal`
  (no outbound traffic or outside DNS; the proxy still reaches the sandbox — real-daemon
  test); a changed policy rebuilds existing networks on the next pass.
- **Config**: neutral names with the Daytona ones as aliases —
  `SANDBOX_AUTO_STOP_MINUTES`, `SANDBOX_NETWORK_BLOCK_ALL`, `SANDBOX_NETWORK_ALLOW_LIST`;
  new `DOCKER_SANDBOX_SUBNET_POOL`/`_SUBNET_PREFIX`, `DOCKER_PREVIEW_PROXY_LABEL`,
  `DOCKER_SANDBOX_TMP_MB`, `DOCKER_SANDBOX_NOFILE`, `DOCKER_REAPER_INTERVAL_SECONDS`.
  `Workspace.sandbox_id` is now a hybrid property, usable in queries.
- **Frontend**: a stopped Docker workspace's pill reads "Workspace stopped · wakes on next
  use".
- **Deferred / not done**: egress allow-list on Docker (an egress proxy container; Daytona
  still honours `SANDBOX_NETWORK_ALLOW_LIST`); per-sandbox disk quota; managed-container
  count and reaper liveness in `/health` (Epic 06 owns health surfacing — the
  `sandbox_maintenance_actions_total` counter exists); showing `status_detail` in the UI
  (the dashboard reads the stored status, not the status endpoint). Residual exposure from
  the security review, unchanged by this epic: a sandbox can reach its gateway (host
  services bound to `0.0.0.0`) and, with egress open, the cloud metadata address; the
  proxy holds `DATABASE_URL` and `AUTH_SECRET_KEY` while sitting on every sandbox network,
  so it is the one pivot to harden (read-only DB role, `METRICS_TOKEN` required on Docker).
