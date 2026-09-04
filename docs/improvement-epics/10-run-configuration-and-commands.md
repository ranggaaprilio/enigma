# Epic 10 - Workspace Run Configuration & Unified Command Registry

## Goal

Make **"what to run"** a first-class, UI-editable part of a workspace instead of a hard-coded
`mise run dev` at the repository root. Converge the existing **Custom Commands** feature and the
**dev-server task** into a single registry of runnable things, so a full-stack, backend-only, or
monorepo repository can be prepared, migrated, and served without changing platform code.

## User Value

A user opens **Run Configuration**, sees the commands Enigma detected in their repo
(`install`, `dev:api`, `dev:web`, `migrate`, `test`), edits or adds their own with a working
directory and a port, and runs any of them with one button. No `mise.toml` authoring required,
and no assumption that the app is a single process at the repo root.

## User Stories

- As a developer with `backend/` + `frontend/` in one repo, I define two run commands with
  different working directories and start both.
- As a developer of a backend-only API, I define `alembic upgrade head` as a one-shot command
  and `uvicorn app.main:app --port 8000` as the served process, then click **Run**.
- As a developer of a pnpm monorepo, I point the workspace at `apps/api` and only that
  package's commands are offered.
- As a user, I press a button to run migrations against my external database after a `git pull`,
  and watch the output stream.
- As a user, I import my `package.json` scripts as commands instead of typing them.

## Current State

**The dev-server path is rigid:**

- `services/dev_server.py` runs exactly `mise run dev` and, for dependencies, exactly
  `mise run install` (`_run_command` / `_setup_command`). Neither the task name nor the tool is
  configurable.
- A `mise` config must exist **at the checkout root** (`MISE_CONFIG_FILES`, `dev_server.py:31`;
  `_has_mise_config`, `dev_server.py:575`); otherwise the status is `no-config`.
- The working directory is always `workspace.workspace_path` or a worktree's `path`
  (`_resolve_worktree`, `dev_server.py:357`). There is no notion of a subdirectory inside the
  repository, so monorepos can only be served by a root config that `cd`s internally.

**The Custom Commands feature is already 80% of the registry this epic needs:**

- `models/command.py` — `CustomCommand` (name, command, description, icon, hotkey,
  `requires_confirmation`) and `CommandRun` (status, exit_code, output, timestamps).
- `api/commands.py` — CRUD, run, run history, and a WebSocket output stream
  (`api/commands.py:221`).
- `services/commands.py` — streamed execution through `DaytonaRuntime.stream_command`,
  **workspace env-var injection** (`_workspace_env`, `commands.py:403`), notifications, and audit
  log entries on start and finish.

**But it cannot express a dev workflow today:**

| Gap | Evidence |
|---|---|
| No working directory — always the repo root | `cwd=workspace.workspace_path` in `_execute_run` |
| Not worktree-aware, so commands ignore the branch being worked on | same call; `WorkspaceWorktree.path` is never consulted |
| 20-second timeout shared with agent tooling | `agent_command_timeout_seconds: int = 20` (`config.py:50`) — `pnpm install` and `alembic upgrade head` cannot finish |
| Everyday dev commands are denied by default | `pnpm`, `npm`, `yarn`, `uv`, `git`, `curl` are in `DANGEROUS_COMMANDS` (`agent_tools.py:35`) and `&&`, `\|`, `;`, `>` in `DANGEROUS_TOKENS` (`agent_tools.py:54`); a user command must be flagged `requires_confirmation` merely to be *allowed* |
| No ordering, grouping, or lifecycle meaning | `list_commands` orders by `created_at` only |
| No port, so a command can never become a preview | no column exists |

## Scope

- An `app_root` subdirectory on the workspace (monorepo / nested-project support).
- Extend `custom_commands` into the single runnable registry: lifecycle role, working directory,
  port, ordering, per-command timeout, enabled flag.
- Worktree-aware execution for every command.
- A command-validation policy that distinguishes **agent-authored** from **owner-authored**
  commands.
- Repository detection that proposes a command list (with an explicit user confirmation step).
- A **Run Configuration** surface with manual trigger buttons and a command palette.

### Non-Goals

- Long-running service lifecycle, restart, and multi-port preview — **Epic 11**.
- Environment-variable editing UX and injection into services — **Epic 12**.
- Wiring one service's URL into another — **Epic 13**.
- Provisioning a database inside the sandbox. Workspaces connect to an **external** database via
  environment variables (see Epic 12's operational notes on sandbox egress).

## Backend Tasks

- **Workspace app root**
  - Add `workspaces.app_root` (nullable, repo-relative, e.g. `apps/api`). Resolve it in one
    helper shared by `dev_server`, `commands`, `terminal`, and `editor` so a single function owns
    `workspace_path` + worktree path + `app_root` composition.
  - Validate strictly: relative only, no `..`, no absolute paths, no symlink escape. Reuse the
    containment style already applied to worktree paths (ids over raw paths).
- **Extend the command registry** (`models/command.py`, `services/commands.py`)
  - New columns on `custom_commands`: `role` (`install` | `migrate` | `dev` | `test` | `lint` |
    `seed` | `custom`), `kind` (`task` | `service` — `service` is implemented in Epic 11),
    `cwd` (relative to the resolved app root), `port` (nullable), `sort_order`, `enabled`,
    `timeout_seconds` (nullable → falls back to a new setting).
  - `create_run` accepts an optional `worktree_id`; resolve it exactly like
    `DevServerService._resolve_worktree` (id in, path out — never a client-supplied path) and
    record it on `CommandRun`.
  - Add `command_run_timeout_seconds` (default 900) to `Settings`; stop reusing
    `agent_command_timeout_seconds` for user commands. Per-command `timeout_seconds` overrides it,
    bounded by a platform maximum.
  - Add a reorder endpoint (bulk `sort_order` update) and keep `list_commands` ordered by
    `sort_order, created_at`.
- **Split the command-safety policy** (`services/agent_tools.py`, `services/commands.py`)
  - Keep `validate_agent_command` (today's denylist) for `agent_tools.run_command` — an agent's
    command choice is model output and stays constrained.
  - Add `validate_user_command` for owner-authored registry entries: allow shell operators and
    package managers, because the workspace owner **already** has an unrestricted interactive PTY
    into the same sandbox (`services/terminal.py`) — the denylist protects nothing here while
    making ordinary commands (`cd api && pnpm dev`) unusable without a confirmation checkbox.
    Keep `requires_confirmation` as a *user intent* flag (destructive commands like `rm -rf`,
    `git reset --hard`), not as an authorisation mechanism.
  - Everything still executes **inside the sandbox** via `DaytonaRuntime`, never on the platform
    host; every run stays audited and rate-limited.
- **Repository detection** (new `services/run_detection.py`)
  - Inspect the checkout and return **candidates**, never auto-persisted: `mise task ls`;
    `package.json` scripts (+ workspace globs from `pnpm-workspace.yaml`, `turbo.json`,
    `nx.json`, `package.json#workspaces`); `pyproject.toml` / `manage.py` / `alembic.ini`;
    `Makefile` targets; `go.mod`, `Cargo.toml`, `composer.json`, `pom.xml`, `build.gradle`.
  - Each candidate carries a suggested `role`, `cwd`, `port`, and the file it was derived from,
    so the UI can explain *why* it was proposed.
  - Detection is read-only and cheap: bounded file reads, no installs, one sandbox round trip
    where possible.
- **Stack presets**
  - Ship named presets (`node-fullstack`, `vite+fastapi`, `django`, `nextjs`, `spring-boot`,
    `laravel`) that expand into a set of registry entries. Reuse the seeding style of
    `workspace_templates.mise_config` (Epic 09) so a template can ship a run configuration
    instead of only a `mise.toml`.
- **Keep the mise path working**
  - `mise run dev` becomes the *default seeded entry* for a repo that has a mise config, not the
    hard-coded behaviour. `DevServerService` keeps its public API and delegates task resolution
    to the registry, so nothing regresses for repositories that already work.

## Frontend Tasks

- New **Run Configuration** section (in Overview & Settings, or a tab beside Commands):
  - `app_root` picker (browse the tree via the existing files API).
  - A sortable table of registry entries: name, role badge, command, working directory, port,
    enabled toggle, and a **Run** button per row.
  - **Detect commands** button → a review dialog listing candidates with checkboxes and the
    source file for each; the user picks what gets saved.
  - **Import from…** shortcuts for `package.json` scripts / `mise` tasks / `Makefile`.
- Extend `CustomCommandsPanel.tsx`:
  - Group by role, respect `sort_order`, drag-to-reorder.
  - Working directory + port + timeout fields in the create/edit modal
    (`CommandModal`, `CustomCommandsPanel.tsx:499`).
  - Show which worktree/branch a run targeted, and let the user pick the worktree before running
    (defaulting to the active one from `lib/activeWorktree`).
- A command palette (existing `hotkey` field is currently stored but not bound) so `install` /
  `migrate` / `dev` are one keystroke away.
- Surface `requires_confirmation` as "ask me first", clearly separated from availability.

## Database Tasks

- Migration (reversible): `workspaces.app_root`; `custom_commands` + `role`, `kind`, `cwd`,
  `port`, `sort_order`, `enabled`, `timeout_seconds` with CHECK constraints on `role`/`kind` and
  `port BETWEEN 1 AND 65535`; `command_runs.worktree_id` (FK, `ON DELETE SET NULL`) and
  `command_runs.cwd`.
- Backfill: existing commands become `role='custom'`, `kind='task'`, `sort_order` by
  `created_at`, `enabled=true` — no behaviour change for current workspaces.
- Index `custom_commands (workspace_id, sort_order)`.

## Security Requirements

- Path containment: `app_root` and per-command `cwd` are resolved server-side and must stay inside
  the workspace checkout (or the selected worktree). Never accept an absolute path from the client.
- Worktrees are addressed by database id, never by path — mirror
  `WorktreeService.resolve_path` / `DevServerService._resolve_worktree`.
- Authorisation: registry writes require workspace ownership/role, consistent with the rest of
  `api/commands.py`.
- The relaxed user-command policy must be accompanied by: audit entries for create/update/delete
  and every run (already present), a per-workspace run rate limit, and secret redaction on all
  output (`git_providers.redact_secrets`).
- Detection must not execute repository code — parse manifests, do not run `npm run`, and never
  evaluate a `Makefile`.

## Acceptance Criteria

- A workspace with `backend/` and `frontend/` can be fully driven from the UI: two `install`
  commands in two directories, one `migrate`, and two run commands — no `mise.toml` in the repo.
- A workspace with `app_root = apps/api` runs its commands in that directory, and detection only
  proposes that package's scripts.
- `pnpm install` and `alembic upgrade head` complete without hitting a timeout and without the
  `requires_confirmation` checkbox.
- **Detect commands** on a Vite + FastAPI repository proposes `dev:web` (port 5173, `frontend/`)
  and `dev:api` (port 8000, `backend/`) with the source files shown.
- Running a command while a feature worktree is active executes in that worktree, and the run
  history shows the branch.
- Existing workspaces and existing custom commands behave exactly as before after the migration.

## Dependencies

- Builds on **Epic 02** (worktrees) for per-branch execution and **Epic 09**
  (`workspace_templates`) for shipping presets with templates.
- **Epic 11** consumes `kind='service'` and `port`; **Epic 12** consumes per-command env scope.
  Land this epic first — it defines the schema both rely on.

## Future Enhancements

- Commit the run configuration to the repository (`.enigma/run.toml`) so it travels with the code
  and can be reviewed in a PR.
- Team-level default run configurations applied to every new workspace.
- Dependency graph between commands (`migrate` depends on `install`) with a single **Prepare**
  button.
- Let the agent read the registry so "run the tests" uses the user's own command.
