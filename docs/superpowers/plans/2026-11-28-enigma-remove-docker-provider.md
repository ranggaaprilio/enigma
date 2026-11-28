# Enigma — Remove the Docker Provider (Plan 8)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** Delete the custom Docker sandbox provider (`runtime="docker"`). OpenSandbox replaced it as the self-host runtime in Plan 7b; Daytona stays the cloud provider.

**Release point:** `v0.1.0` (tag on `f23cef2`, PR #35) is the last release with the Docker provider. An install that still needs it stays on that tag.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §9.2: Docker is replaced by OpenSandbox, and must not be deleted before OpenSandbox passes conformance and boundary (it does: `tests/sandbox/test_conformance.py`, `test_boundary.py`, the live suite).

## Current state (verified, survey 2026-11-28)

- **Docker-only code:**
  - `services/sandbox/docker.py`, `docker_fs.py`, `docker_pty.py`, `port_relay.py` and `app/egress_proxy.py`;
  - the factory entry (`factory.py:37`);
  - availability placement rules and `require_metrics_token_for_docker`;
  - `/health`'s `managed_containers` / `upkeep_last_pass_at`;
  - the Docker upkeep in `sandbox_maintenance.py` (`stop_idle_sandboxes`, `reconcile_orphans`, `isolate_legacy_sandboxes`, `run_/start_sandbox_maintenance`);
  - `editor.py`'s loopback relay branch;
  - about 25 `DOCKER_*` settings plus `docker_single_host`, `docker_upkeep` and `sandbox_busy_max_minutes` (read only by Docker's idle stop);
  - the `docker` Python SDK dependency (imported only by `docker.py` and its tests).
- **Shared, stays:**
  - `sandbox/activity.py`, `sandbox/installation.py`, the `sandbox_activity` / `sandbox_installation` tables (migration 0036);
  - the generic upkeep helpers (`_try_lock`, `_count`, `_idle`, `_idle_workspaces`, `_workspace_uuid`) and the OpenSandbox upkeep;
  - `expose_port` in the protocol (Daytona and OpenSandbox implement it);
  - `docker_reaper_interval_seconds` (the OpenSandbox upkeep sleeps on it);
  - the `enigma-sandbox` image and `sandbox/Dockerfile`.
- **Leftover rows.** `workspaces.runtime` is a free `String(32)` (no enum or CHECK), so `runtime='docker'` rows survive. Without a provider, `get_runtime_by_name` raises `RuntimeUnavailableError` and every call 503s. `WorkspaceService.delete` calls `runtime.delete` first and re-raises, so **such a workspace could not even be deleted**.

## Design

### 1. Leftover Docker workspaces: the operator removes them (decision at Gate 1)

No compatibility shim. After the upgrade, a `runtime='docker'` row has no provider: every call raises `RuntimeUnavailableError` ("Unknown sandbox runtime 'docker'", 503), and it cannot be deleted through the API. The upgrade notes therefore say:
- **Before upgrading** (on v0.1.0): push branches, recreate each Docker workspace on OpenSandbox, and delete the Docker ones through the UI. That also removes their containers.
- **Or after**, in SQL: `DELETE FROM workspaces WHERE runtime = 'docker';`. Every foreign key to `workspaces` cascades, except `audit_logs` (set null, so the audit trail stays); verified on the dev database, which has no Docker rows. Leftover containers are then removed by hand (`docker ps -a --filter label=enigma.managed`).
- `/health`, the create form and validation drop Docker. `POST /workspaces` with `runtime="docker"` is a 422.
- The frontend labels an unknown runtime with its raw name, so a leftover row is recognisable.

### 2. Settings

- **Removed:** every Docker-only setting. A removed name still set in the environment is logged once at startup as `removed_setting_name` (like `deprecated_setting_name`); it is never an error, because `extra="ignore"` already drops it.
- **Renamed:** `docker_reaper_interval_seconds` → `sandbox_upkeep_interval_seconds`, with `DOCKER_REAPER_INTERVAL_SECONDS` accepted as a deprecated alias for one release (the existing `DEPRECATED_SETTING_NAMES` mechanism).
- `SANDBOX_RUNTIME_DEFAULT=docker`, or `SANDBOX_RUNTIMES_ALLOWED` naming docker, fails validation at startup with a message naming the release, so a stale config is never silently accepted.

### 3. Deleted outright

The modules above, their tests and fakes, `docker-compose.sandbox.yml`, `.github/workflows/docker-integration.yml`, `docs/self-host-docker.md`, `docs/local-docker-runtime/`, and the `docker` SDK (`uv lock`). `tests/test_sandbox_maintenance.py`'s `maker` / `_workspace` fixtures move to `tests/conftest.py` or a shared helper first, because the OpenSandbox tests import them.

### 4. Version

`0.1.0` → `0.2.0` in `backend/pyproject.toml`, the root and frontend `package.json`, and the production overlay's image tags.

## Non-goals

- Migrating Docker workspaces' state (git is the source of truth).
- Removing the leftover containers from operators' hosts.
- Renaming the generic `[docker]` section of OpenSandbox's own config (that is the server's runtime type).

## Tasks

### Task 1: Docker leaves the provider choice (TDD)

**Files:** `factory.py`, `availability.py`, `schemas/workspaces.py`, `config.py`, tests.

- [x] Failing tests:
  - `/health` lists only daytona and opensandbox;
  - `POST /workspaces` with `runtime="docker"` → 422;
  - `SANDBOX_RUNTIME_DEFAULT=docker` or `SANDBOX_RUNTIMES_ALLOWED=docker` fails validation with a message naming v0.1.0;
  - a `runtime="docker"` row resolves to `RuntimeUnavailableError`.
- [x] Implement (this lands together with Task 2, because the provider goes in the same step).

### Task 2: Remove the backend provider

**Files:** the modules listed above, `config.py`, `main.py`, `worker.py`, `preview_proxy.py`, `health.py`, `editor.py`, `sandbox_maintenance.py`, `pyproject.toml` / `uv.lock`, tests.

- [x] Move the shared fixtures; delete the Docker-only tests and fakes; edit the rest. Fixtures that only used `runtime="docker"` as a value switch to `opensandbox`.
- [x] Failing tests:
  - `removed_setting_name` is logged for a stale `DOCKER_*` name;
  - `DOCKER_REAPER_INTERVAL_SECONDS` still sets the upkeep interval and is logged as deprecated;
  - the boundary test forbids `import docker` anywhere in `app/`.
- [x] Remove the code and settings; `uv lock` drops the SDK.
- [x] Also gone with it: `DEPRECATED_PROVIDERS` and its startup warning, `/health`'s `deprecated` field, the metrics-token guard for a Docker default, the proxy's Docker probe skip, the editor's loopback relay branch, and an OpenSandbox upkeep write (`last_upkeep_at`) that only Docker's health read.

### Task 3: Frontend

**Files:** `lib/api.ts`, `lib/runtime.ts`, `CreateWorkspaceWizard.tsx`, `DashboardRoute.tsx`, `ProviderBadge.tsx`, `DashboardShell.tsx`, `DashboardOverviewRoute.tsx`, `LandingRoute.tsx`, tests.

- [ ] Failing tests:
  - the create form offers daytona and opensandbox only;
  - an existing docker workspace reads "Docker (removed)" with the recreate hint;
  - the snapshot field follows Daytona only.
- [ ] Implement; drop the health fields that are gone.

### Task 4: Infra, CI, docs, version

**Files:** the compose, workflow and doc files listed above, `.env.example`, `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `sandbox/build-local-image.sh`, `docker-compose.opensandbox.yml` comments, `deploy/k8s/overlays/production/kustomization.yaml`, versions, this plan.

- [ ] Delete and edit; add a "Removed in 0.2.0" note to the OpenSandbox guide's "Moving from Docker"; bump to 0.2.0.

### Task 5: Verify

- [ ] Full backend and frontend suites; `ruff`, `tsc`, `vite build`.
- [ ] A live run of the OpenSandbox suite through the compose override (separate `-p` project), because the shared upkeep code changed.
- [ ] Migration safety: no migration is added; leftover docker rows are the operator's (upgrade notes).

## Exit criteria

- No `docker` provider code, SDK, settings, compose file or workflow remains, and the boundary test enforces it.
- The upgrade notes give the SQL for leftover Docker workspaces (cascade verified).
- All suites green (known unrelated: the `test_retention` index test and the full-suite `.env` settings test), and the OpenSandbox live suite still passes.
