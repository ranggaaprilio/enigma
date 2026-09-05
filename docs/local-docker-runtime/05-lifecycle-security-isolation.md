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
  (ex `daytona_runtime.py:118-133`): `auto_stop_interval` (=
  `daytona_auto_stop_minutes`, 30), `auto_archive_interval=0`, `auto_delete_interval=-1`,
  `ephemeral=False` — the platform enforces them. Docker has **no equivalent**; the
  provider must enforce them itself.
- Egress policy on Daytona: `_desired_network_settings` / `_apply_network_settings`
  (ex `daytona_runtime.py:314-352`) driven by `daytona_network_block_all` and
  `daytona_network_allow_list`. Epic 02 put Docker sandboxes on one bridge network with
  no policy.
- Epic 02 set security floors (non-root, no socket, no privileged, fixed cpu/mem) but no
  reaper, no pids/disk limits, no seccomp/cap profile, no startup reconciliation.
- The backend has no background-maintenance scaffolding beyond lifespan hooks
  (`main.py:13` only closes the runtime); `horizontal-scale/01` plans startup
  reconciliation for *runs* — this epic's reconciliation is for *containers*, a
  different object, coordinated below.
- Activity signals that should reset an idle clock already flow through the runtime
  layer: `exec`/`stream_command`/PTY traffic, preview proxy requests, editor sessions.

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
   `exec` / `stream_command` / PTY byte / `preview_link` call. Store it as a container
   label refresh or a lightweight table (`sandbox_activity(workspace_id,
   last_activity_at)`) — decide by write frequency; label writes are cheap but racy, DB
   is authoritative. Recommendation: DB row, debounced to at most one write per 30s.
2. **Reaper task**: an asyncio background task started from the FastAPI lifespan (only
   when the Docker provider is configured): every `docker_reaper_interval_seconds` (60),
   stop `enigma.managed` containers idle beyond `sandbox_auto_stop_minutes` (a neutral
   setting defaulting to the value of `daytona_auto_stop_minutes`, 30). Stopped
   containers are *kept* (parity with Daytona auto-stop, not delete); the
   ensure-started semantics from Epic 02 transparently restart on next use.
3. **Orphan reconciliation on startup**: list containers labelled `enigma.managed=1`,
   cross-check `workspaces.sandbox_id`; containers with no matching workspace row are
   removed (log + counter); workspaces whose container vanished get `status="error"`
   surfaced by the existing status endpoint. Single-flight guard so two backend
   processes (uvicorn reload) don't double-reap.
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
   surfaces as a distinct workspace status detail); health output includes
   managed-container count and reaper liveness timestamp.
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

- Migration for `sandbox_activity` **only if** the DB variant is chosen in task 1
  (columns: `workspace_id` FK/PK, `last_activity_at timestamptz`). Otherwise none.

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
- **Coordination, not overlap, with `horizontal-scale/01`** (startup orphan
  reconciliation for *runs*) — different objects, same lifespan hook; structure the
  lifespan so both reconcilers coexist, and note it in whichever PR lands second.
- Ready to serve `hybrid-runtime-migration/05` later: its ephemeral-cleanup reaper can
  extend this one rather than duplicate it (shared label scheme).

## Future Enhancements

- gVisor (`runsc`) or Kata runtime class as opt-in `docker_sandbox_runtime` config for
  stronger kernel isolation.
- Auto-delete tier (`auto_delete_interval` parity) for long-abandoned stopped
  containers, with a grace notification.
- Disk quotas via xfs project quotas on supported hosts; image GC for stale sandbox
  image versions.
