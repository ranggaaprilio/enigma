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
  (`services/workspaces.py:131`) and defaulted in `schemas/workspaces.py:80,103`.
- `GET /api/health` checks only Daytona (`api/health.py:41-45`, via the neutral factory
  since Epic 01).
- Frontend: workspace creation UI has no runtime field; no provider badge anywhere; all
  HTTP via `src/lib/api.ts`; no component library (Tailwind only), React Context state.
- Config still carries `daytona_*` names for genuinely neutral concepts
  (`sandbox_home`, auto-stop minutes, preview proxy base URL) with aliases introduced
  piecemeal in Epics 02–05.
- The optional `daytona_sandbox_id` → `sandbox_id` column rename (deferred from
  Epic 01) is still pending.
- Compose mounts the Docker socket (Epic 02) unconditionally; docs for a Daytona-less
  install don't exist.

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
   Remove the hardcode at `services/workspaces.py:131`.
2. **`sandbox_runtime_default`** config (default `"daytona"` for continuity). If the
   default provider is unconfigured but the other is, log a startup warning and fall
   back at creation-time validation (explicit, surfaced in the response) — never
   silently at exec-time.
3. **Health**: `/api/health` reports both providers:
   `{"runtimes": {"daytona": {"configured": true, "status": "ok"},
   "docker": {"configured": true, "status": "ok", "managed_containers": 3}}}` — reusing
   `get_runtime_by_name`; unconfigured ⇒ `"status": "unconfigured"`, never an error.
   Keep overall health semantics unchanged for existing monitors.
4. **Workspace read schemas** already expose `runtime` — verify serialization and add
   it to any list endpoints that trim fields.
5. **Naming cleanup** (finish the aliases): neutral settings become canonical
   (`sandbox_home`, `sandbox_auto_stop_minutes`, `preview_proxy_base_url`,
   `sandbox_network_*`); `daytona_*` env names remain accepted aliases for one release
   (pydantic-settings `AliasChoices`), with a deprecation log line.
6. **Column rename migration**: `workspaces.daytona_sandbox_id` → `sandbox_id`
   (+ index rename `ix_workspaces_daytona_sandbox_id` → `ix_workspaces_sandbox_id`),
   dropping the ORM alias property. `editor_sessions.bind_host` data values stay
   (`"daytona"`/`"docker"` are already neutral enough). One reversible Alembic
   migration.
7. **Docs (operator)**: `docs/self-host-docker.md` — prerequisites (Docker/Podman,
   socket path, compose overrides), building `enigma-sandbox:latest`, env profile with
   no Daytona key, egress policy options, resource-limit tuning, the single-host
   limitation (explicit pointer to `docs/horizontal-scale/` for what this profile does
   NOT cover), backup/reset guidance (`docker rm` + reprovision-from-git).
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
   copy (Docker error states can suggest "is the Docker daemon running?").
4. **Vitest**: picker availability logic (both available / one / neither), badge
   rendering, default selection.

## Database Tasks

- The rename migration (task 6): `daytona_sandbox_id` → `sandbox_id` with its index.
  Reversible; no data transform.

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
- Alembic upgrade+downgrade of the rename migration round-trips on a seeded DB.

## Dependencies

- **Epics 01–05** complete (04 for the full walkthrough; 05 for honest self-host
  defaults).
- **Non-overlap check**: `hybrid-runtime-migration/06` (rollout of tiering) and this
  rollout are independent flags on independent axes; if both series are mid-flight,
  land their config keys with distinct prefixes (`hybrid_*` vs `sandbox_runtime_*`) —
  already the case as specced.
- `horizontal-scale` untouched: the docs deliverable records that Docker-runtime
  workspaces are node-pinned and out of that series' scope.

## Future Enhancements

- Workspace **provider migration** (Daytona ↔ Docker) via git re-hydration once
  `hybrid-runtime-migration/04` defines the hydrate/push contract.
- `sandbox_node` column + placement awareness if a multi-node self-host profile is ever
  wanted (would then belong to a horizontal-scale follow-up).
- Auto-detect "Daytona quota exhausted / unreachable" and suggest Docker fallback at
  creation time.
- A Kubernetes provider (`runtime="k8s"`) — the Epic 01 interface is the contract.
