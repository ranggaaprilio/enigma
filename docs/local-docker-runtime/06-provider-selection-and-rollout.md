# Epic 06 - Provider Selection, Config & Rollout

## Goal

Productize the second provider: let a user choose the runtime at workspace creation
(with sane defaults per deployment profile), surface provider identity and availability
across health/status/UI, finish the config/naming cleanup, ship the self-host
documentation, and wire both providers into CI. After this epic "I don't subscribe to
Daytona" is a supported first-class deployment, not a developer trick.

## User Value

The actual promise of the series: open the workspace-create form, pick **Local Docker**
(or have it picked for you because no Daytona key is configured), and get the full
Enigma experience on your own machine. Operators get one documented self-host profile.

## Current State

- Epics 01–05: both providers work behind `get_runtime(workspace)`, but
  `workspace.runtime` is still hardcoded `"daytona"` at creation
  (`services/workspaces.py:180`) and defaulted by the `default_runtime` validators in
  `schemas/workspaces.py:138-140,166-168` (`runtime: str` at 123, 157).
- `GET /api/health` checks only Daytona (`api/health.py:49-54`, a throwaway
  `DaytonaRuntime` per probe until Epic 01 routes it through `get_runtime_by_name`) and
  reports it as the `daytona` field; `/live` and `/ready` are runtime-free and are what
  the Kubernetes probes and `tests/test_deployment.py` depend on.
- Frontend: workspace creation UI has no runtime field; no provider badge anywhere; all
  HTTP via `src/lib/api.ts`; no component library (Tailwind only), React Context state.
  Hard-coded "Daytona" copy lives in `CreateWorkspaceWizard.tsx:111,475`,
  `TeamRoute.tsx:323,420,441`, `DashboardOverviewRoute.tsx:25` (a filter key
  `"daytona"`) and `DashboardRoute.tsx:519,593`.
- Config still carries `daytona_*` names for genuinely neutral concepts
  (`sandbox_home`, auto-stop minutes, preview proxy base URL/TTL, network policy) with
  aliases introduced piecemeal in Epics 02–05; `.env.example:76-100` documents the
  `DAYTONA_*` set.
- The `daytona_sandbox_id` → `sandbox_id` column rename (deferred from Epic 01) is
  still pending and is a **contract** migration under `tests/test_migration_safety.py`.
- Compose mounts the Docker socket into backend, worker and preview-proxy (Epic 02)
  unconditionally; docs for a Daytona-less install don't exist. `deploy/k8s/` is
  Daytona-only by design and stays that way.

## Scope

**In scope**

- Runtime selection at workspace creation: API validation + frontend picker + defaults.
- `sandbox_runtime_default` setting and provider availability detection.
- Health/status/UI surfacing of provider identity and daemon availability.
- Config + column rename cleanup; `.env.example` self-host profile; operator guide for
  the Docker-only install.
- CI: conformance suite for both providers on every PR; Docker integration job.

**Out of scope**

- Changing any provider mechanics (frozen from Epics 02–05).
- Migrating an existing workspace between providers (Future Enhancement — needs git
  re-hydration, best built after `hybrid-runtime-migration/04` lands its sync contract).
- Multi-node Docker (documented limitation, not built).

## Backend Tasks

1. **Creation flow**: `WorkspaceCreate` schema accepts `runtime: Literal["daytona",
   "docker"] | None`; `None` → `settings.sandbox_runtime_default`. Validate at creation:
   the requested provider must be `configured` (Daytona key present / Docker daemon
   reachable) — fail with a 422 carrying an actionable message, not at first use.
   Remove the hardcode at `services/workspaces.py:180`. For `runtime="docker"` also
   refuse (422) when `RUN_EXECUTION_MODE=queue` unless `docker_single_host=true` is set
   — the operator's explicit statement that API, workers and proxy share one daemon.
2. **`sandbox_runtime_default`** config (default `"daytona"` for continuity). If the
   default provider is unconfigured but the other is, log a startup warning and fall
   back at creation-time validation (explicit, surfaced in the response) — never
   silently at exec-time.
3. **Health**: `/api/health` reports both providers:
   `{"runtimes": {"daytona": {"configured": true, "status": "ok"},
   "docker": {"configured": true, "status": "ok", "managed_containers": 3}}}` — reusing
   `get_runtime_by_name`; unconfigured ⇒ `"status": "unconfigured"`, never an error.
   Keep the existing top-level `daytona` field and overall `ok|degraded` semantics for
   existing monitors; leave `/live` and `/ready` untouched (the proxy's own `/health`
   gets the same `runtimes` block).
4. **Workspace read schemas** already expose `runtime` — verify serialization and add
   it to any list endpoints that trim fields.
5. **Naming cleanup** (finish the aliases): neutral settings become canonical
   (`sandbox_home`, `sandbox_auto_stop_minutes`, `preview_proxy_base_url`,
   `sandbox_network_*`); `daytona_*` env names remain accepted aliases for one release
   (pydantic-settings `AliasChoices`), with a deprecation log line.
6. **Column rename, expand/contract**: migrations after `0034` must be expand-only
   (`tests/test_migration_safety.py`), so the rename ships in two releases —
   *expand*: add `workspaces.sandbox_id` + `ix_workspaces_sandbox_id`, backfill, make
   the ORM write both columns; *contract* (next release): drop `daytona_sandbox_id` and
   its index with a `# contract: <why safe>` marker, and drop the ORM alias property.
   `editor_sessions.bind_host` data values stay (`"daytona"`/`"docker"` are already
   neutral enough).
7. **Docs (operator)**: `docs/self-host-docker.md` — prerequisites (Docker/Podman,
   socket path, compose overrides), building `enigma-sandbox:latest`, env profile with
   no Daytona key, egress policy options, resource-limit tuning, the single-host
   limitation (`docker_single_host`, explicit pointer to `docs/horizontal-scale/` and
   `deploy/README.md` for what this profile does NOT cover — Kubernetes stays
   Daytona-only), backup/reset guidance (`docker rm` + reprovision-from-git, plus
   `scripts/backup_db.sh` / `restore_db.sh` for Postgres).
8. **CI**: conformance suite (both fakes) on every PR; a separate scheduled/opt-in job
   runs `DOCKER_INTEGRATION_TESTS=1` against a real daemon with the built image
   (mirroring the `DAYTONA_INTEGRATION_TESTS=1` pattern).

## Frontend Tasks

1. **Runtime picker** in workspace creation: radio between "Daytona (cloud)" and
   "Local Docker", options enabled per `/api/health` runtime availability; an
   unavailable option shows the reason ("no API key configured" / "Docker daemon not
   reachable"). Default = the server's `sandbox_runtime_default` (exposed via an
   existing config/meta endpoint or the health payload). Tailwind only, no new deps.
2. **Provider badge**: small "Daytona" / "Local Docker" chip on the workspace card and
   workspace header (lucide-react icon), driven by the existing `runtime` field.
3. **Status copy**: creating/starting/stopped/error states get provider-appropriate
   copy (Docker error states can suggest "is the Docker daemon running?"). Replace the
   hard-coded "Daytona" strings listed under Current State (`CreateWorkspaceWizard`,
   `TeamRoute`, `DashboardRoute`) with copy driven by `runtime`; make the
   `DashboardOverviewRoute` filter list both providers.
4. **Vitest**: picker availability logic (both available / one / neither), badge
   rendering, default selection.

## Database Tasks

- The expand half of task 6 (add `sandbox_id` + index, backfill from
  `daytona_sandbox_id`), numbered after the current head `0035`. The contract half is a
  separate later migration carrying the `# contract:` marker.

## Security Requirements

- Creation-time validation must not leak infrastructure details to non-admin users
  beyond what the picker shows (configured yes/no + generic reason).
- The health endpoint's runtime block must not include daemon socket paths, API URLs,
  or version strings for unauthenticated callers — match the existing health exposure
  level.
- Authorization unchanged: runtime choice is a workspace attribute governed by the same
  project/team membership rules as creation itself.
- Docs must state plainly: mounting the Docker socket into the backend grants that
  service root-equivalent host access — isolate the host accordingly (dedicated
  machine/VM for multi-user self-hosts).

## Acceptance Criteria

- Fresh install, **no Daytona key**: `.env` profile from the docs, build the image,
  `docker compose up` → create a workspace (picker shows only Local Docker) → agent run
  edits files, commits, pushes → terminal, editor, preview all work. The complete
  no-subscription path, verified as one scripted walkthrough.
- Existing install with both configured: the picker offers both; the default honors
  `sandbox_runtime_default`; Daytona workspaces behave exactly as before the series.
- `/api/health` correctly reports each provider in all four configured/unconfigured
  combinations.
- Requesting `runtime="docker"` with the daemon down → immediate 422 with an actionable
  message; nothing half-provisioned.
- CI: conformance suite green on both fakes; the Docker integration job green on the
  reference runner; the `grep`-guard from Epic 01 still enforced.
- Alembic upgrade+downgrade of the expand migration round-trips on a seeded DB and
  passes `tests/test_migration_safety.py`.
- `runtime="docker"` with `RUN_EXECUTION_MODE=queue` and no `docker_single_host=true`
  → 422; with the flag, a run enqueued by the API executes in the compose `worker`
  against the same container.
- `tests/test_deployment.py` stays green: nothing Docker-related enters `deploy/k8s/`.

## Dependencies

- **Epics 01–05** complete (04 for the full walkthrough; 05 for honest self-host
  defaults).
- **Non-overlap check**: `hybrid-runtime-migration/06` (rollout of tiering) and this
  rollout are independent flags on independent axes; if both series are mid-flight,
  land their config keys with distinct prefixes (`hybrid_*` vs `sandbox_runtime_*`) —
  already the case as specced.
- `horizontal-scale` (shipped) untouched: the docs deliverable records that
  Docker-runtime workspaces are host-pinned, that queue mode needs the single-host
  flag, and that the Kubernetes overlay does not carry the Docker provider.

## Future Enhancements

- Workspace **provider migration** (Daytona ↔ Docker) via git re-hydration once
  `hybrid-runtime-migration/04` defines the hydrate/push contract.
- `sandbox_node` column + placement awareness if a multi-node self-host profile is ever
  wanted (would then belong to a horizontal-scale follow-up).
- Auto-detect "Daytona quota exhausted / unreachable" and suggest Docker fallback at
  creation time.
- A Kubernetes provider (`runtime="k8s"`) — the Epic 01 interface is the contract.

## Implementation Notes (shipped)

Built on the Epic 05 test branch. Gate 1 decisions:
- Ship only the **expand** half of the column rename.
- Automate the no-subscription walkthrough as a stack test with the mock agent provider.
- Run the Docker integration job weekly and on demand, while the main CI triggers stay
  disabled.
- **`METRICS_TOKEN` is required with Docker.**

The doc's "Current State" was stale. `sandbox_runtime_default` already existed, conformance
already ran both fakes, the base compose never mounted the socket, and the migration head was
`0036`.

- **Creation** (`services/sandbox/availability.py`):
  - `POST /workspaces` and the team route take an optional `runtime`.
  - `choose_runtime` runs before the row is written:
    - An explicit choice is honoured or refused, never swapped.
    - No choice takes `SANDBOX_RUNTIME_DEFAULT`, or the other provider when only that one
      is available, with a `sandbox_runtime_default_fallback` log line.
  - A refusal is a 422 with a generic reason: configured yes/no, or not reachable. There
    is no socket path or URL in it, and nothing is half-provisioned.
  - Docker additionally needs `DOCKER_SINGLE_HOST=true` in queue mode and a non-blank
    `METRICS_TOKEN` (security review M1). Otherwise it gets a generic "not available for
    this deployment", and the reason goes to the log only (`sandbox_runtime_refused`).
  - **Availability check.** An unconfigured provider is never probed. A configured one is
    asked `health()` under a 3 s timeout, so a hung daemon reads as down.
  - At startup the API logs `sandbox_runtime_default_unconfigured` when new workspaces
    will fall back.
- **Health:**
  - `/health` gains `sandbox_runtime_default` and a `runtimes` block per provider
    (`configured`, `status` ok/error/unconfigured, `reason`). Docker also gets
    `managed_containers` and `upkeep_last_pass_at`, carried over from Epic 05.
  - **Which statuses count as faults.** Unconfigured is not a fault. A configured provider
    that is down, or having no provider at all, is a fault (`degraded`, 503).
  - **Monitors.** The top-level `daytona` field stays. `/live` and `/ready` are unchanged.
  - **Probe cost.** Probes are shared through `app.state` for 5 s with one in flight, so an
    anonymous flood never becomes a flood of daemon or Daytona calls (security review M2).
  - **Operational detail.** The count and the upkeep time are shown only to a
    `METRICS_TOKEN` bearer when a token is set.
  - **Proxy `/health`.** The proxy's token-gated `/health` carries the same block but
    probes only its default provider. It holds no socket, so Docker is `skipped`.
- **`METRICS_TOKEN`.** With `SANDBOX_RUNTIME_DEFAULT=docker`, the API and the preview proxy
  refuse to start without one, because the proxy is reachable from every sandbox. The
  compose override passes it to backend, worker and proxy with `:?`.
- **Naming:**
  - `SANDBOX_HOME` and `SANDBOX_WORKSPACE_PATH` join the earlier neutral names.
  - Every `DAYTONA_*` alias of a neutral setting works for one release and is logged as
    `deprecated_setting_name` (`core/config.py` `DEPRECATED_SETTING_NAMES`).
  - Compose maps the old names onto the new ones, so under compose that log never fires.
  - The k8s config, compose, README and `.env.example` use the new names.
- **Column** (migration `0037`, expand):
  - Adds `workspaces.sandbox_id` and `ix_workspaces_sandbox_id`, and backfills them.
  - The `Workspace.sandbox_id` hybrid writes both columns and still reads
    `daytona_sandbox_id`, which the previous release keeps writing during a rolling deploy.
  - Upgrade, downgrade and upgrade round-trip on a seeded scratch database.
  - **Contract (next release):** backfill again, switch reads, then drop the old column
    and index with `# contract:`.
- **Frontend:**
  - Runtime picker in `CreateWorkspaceWizard`, fed by `/health`:
    - It preselects the server default when that is available.
    - It disables an unavailable option and shows its reason.
    - It offers "Check again" when nothing is available.
    - It hides the Daytona snapshot field on Docker.
  - `ProviderBadge` on the workspace header and the team's workspace cards.
  - `runtimeLabel` replaces the hard-coded "Daytona" copy (team, dashboard, settings,
    landing).
  - The overview and foundation checks list both providers.
  - The status pill suggests "Is the Docker daemon running?" on a Docker `error`.
  - `status_detail` is shown in the UI, carried over from Epic 05. It is read once per
    workspace, never polled (a poll would keep a sandbox awake), and not while
    `creating`.
- **Docs and CI:**
  - `docs/self-host-docker.md` is the operator guide. It covers the socket being
    root-equivalent, install, tuning, egress, single-host limits, backup/reset and
    troubleshooting.
  - `.github/workflows/docker-integration.yml` builds the image and runs the real-daemon
    suite, weekly and on demand (`permissions: contents: read`).

**Verification:**
- Unit tests: health in all four configured combinations, the cache and the token gating;
  the choice, fallback, queue and token rules; the 422 route; settings aliases; the column
  mapping.
- Vitest: picker with both, one or no provider available, the default, Check again, and the
  badge.
- Real-daemon: 25 tests.
- Stack e2e: 7 tests on a **fresh compose project with no Daytona key** and the mock
  provider, run 4 times.
  - `/health` shows only Docker, and an explicit `runtime=daytona` gets the 422.
  - A create with no runtime lands on Docker.
  - The agent run's tool call runs in the sandbox, followed by an edit, a commit and the
    log.
  - The terminal, editor, previews, services, wake on use and kill/restart all work.
- **Keep-alive flake.** The stack tests' HTTP clients keep idle connections for 2 s, under
  uvicorn's 5 s keep-alive. Through Podman's port forwarder a stale connection otherwise
  failed with "Server disconnected without sending a response", which was the cause of the
  earlier unexplained flake.

**Not verified / deferred:**
- **Push in the walkthrough.** It needs a real remote and credentials, and the SSRF guard
  refuses `localhost` remotes. The git token path is covered by the real-daemon tests.
- **Agent edits.** The mock provider only lists files. Agent file and git tools on Docker
  are covered by `test_agent_file_and_git_tools_on_a_blank_workspace`.
- **Queue mode with `DOCKER_SINGLE_HOST=true`.** That a run enqueued by the API executes in
  the compose worker is not exercised; only the 422 rule is.
- **Existing Daytona workspaces unchanged.** Not re-checked against a real Daytona account;
  there is no key here.
- **The integration workflow** has not run on GitHub yet.
- **Deferred:**
  - An operator allow-list of runtimes users may pick (security review, LOW).
  - Pinning actions to SHAs (the main CI uses tags too).
  - The contract migration.

### Follow-ups (shipped after the epic)

- **Runtime allow-list** (security review, LOW). `SANDBOX_RUNTIMES_ALLOWED` is a
  comma-separated list; empty means every provider.
  - Settings refuse unknown names, and a list that leaves out `SANDBOX_RUNTIME_DEFAULT`.
  - `choose_runtime` refuses an explicit pick of a left-out provider (422, "Not enabled on
    this deployment") and never falls back to one.
  - `runtime_availability` reports it as `disabled` without probing it. `/health` shows
    `disabled`, which is not a fault, and the create form does not offer it.
  - Existing workspaces keep running on their provider.
- **Column contract, step 1** (migration `0038`, expand-only). It backfills
  `workspaces.sandbox_id` once more, for rows the pre-0037 release wrote during 0037's
  rollout. The `Workspace.sandbox_id` hybrid now **reads `sandbox_id`** and still writes
  both columns, because the 0037 release reads `daytona_sandbox_id` while it overlaps this
  one. Tests build workspaces with `sandbox_id=`.
- **Column contract, step 2** (migration `0039`, `# contract:`) backfills one last time,
  makes `ix_workspaces_sandbox_id` unique (the old index was), and drops
  `daytona_sandbox_id` with its index. `Workspace.sandbox_id` is a plain mapped column again.
  It must ship in the **next** release, once no deployed replica reads the old column.
  Upgrade, downgrade and upgrade round-trip on the dev database.

