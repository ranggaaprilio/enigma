# Epic 12 - Environment Variables: Injection, Bulk Editing & Scoping

## Goal

Make workspace environment variables usable for real applications: **inject them into everything
that runs** (today the dev server gets none), let users paste a whole `.env` instead of typing one
key at a time, scope variables to specific services, and materialise a `.env` file for frameworks
that read one from disk.

This is the epic that makes a **backend workspace connected to an external database** work at all.

## User Value

A user pastes their `.env` into a textarea, saves once, clicks **Run** — and the API starts with
`DATABASE_URL` and its API keys present. Changing a variable prompts "restart the 2 services that
use it", and secrets never appear in a log the browser can read.

## User Stories

- As a backend developer, I paste 15 variables at once from my local `.env`.
- As a developer, my app reads `.env` from disk (Next.js, Laravel, Django), so I ask Enigma to
  write one into the app directory.
- As a developer, I give the frontend and the backend different values for the same key name.
- As a developer, I fix a typo in `DATABASE_URL` and restart just the API.
- As a security-conscious user, I can confirm that a saved secret never appears in a log or an
  audit entry.

## Current State

**Storage and API exist and are sound:**

- `workspace_env_vars` (`models/workspace.py:103`) — `UNIQUE (workspace_id, key)`, value stored as
  `encrypted_value`, encrypted with `WorkspaceSecretCipher` (Fernet).
- Endpoints: `GET` / `PUT` / `DELETE /workspaces/{id}/env-vars`
  (`api/workspaces.py:1440`, `:1453`, `:1487`).
- Responses are **write-only by design**: `env_response` returns `masked_value=mask_env_value()`
  (`api/workspaces.py:339`), and `mask_env_value()` (`services/workspaces.py:57`) takes no
  arguments — it is a constant mask, so a value can never be read back through the API.

**The critical gap — the dev server receives no environment at all:**

- `WorkspaceEnvVar` is read in exactly two places: `TerminalService.environment`
  (`services/terminal.py:149`) and `CommandService._workspace_env`
  (`services/commands.py:403`).
- Every `runtime.exec` call in `services/dev_server.py` passes `cwd` and `timeout` but **no
  `env=`**. So variables the user carefully saved in Settings never reach `mise run dev`. An API
  previewed today starts with no `DATABASE_URL` and fails on its first query — which is precisely
  why only frontend repositories have previewed successfully so far.

**A latent secret-leak path exists in the dev-server launch scripts:**

- `_setup_command` / `_run_command` (`dev_server.py:417`, `:470`) begin with `exec >LOG_PATH 2>&1`
  followed by `set -x`, so **every line of the script is traced into the log**.
- `_read_log_tail` (`dev_server.py:532`) reads that log and it is surfaced in the UI inside the
  `error` status message.
- Consequently, injecting secrets by writing them into the script body — the obvious
  implementation — would print them into a log the browser displays. Injection **must** go through
  the runtime's `env` parameter, which never enters the script body: `exec` hands `env` straight to
  the Daytona SDK (`daytona_runtime.py:181-195`), and `stream_command` builds a `shlex.quote`d
  `env K=V …` prefix ahead of the command (`daytona_runtime.py:227`).

**The editing UX is one key at a time:**

- `EnvVarManager` (`frontend/src/routes/DashboardRoute.tsx:1415`) is a single key/value form:
  add or replace one variable per request. There is no bulk paste, no import, no export, no
  multi-line value affordance, and — because values are never returned — editing means retyping
  the whole value from memory.

**No scoping:** all variables apply to everything (terminal + commands), so a frontend and a
backend in one workspace cannot hold different values for the same key.

## Scope

- Inject workspace env vars into dev-server / service launches and one-shot command runs.
- Bulk editing: paste-a-`.env`, diff-and-confirm, export a redacted template.
- Scoping: workspace-wide (default) or bound to specific registry entries / services.
- Optional `.env` file materialisation into the app directory inside the sandbox.
- "Restart affected services" flow after a change.
- Hardened redaction across logs, audit entries, and command output.

### Non-Goals

- **Provisioning a database.** Workspaces point at an external database; Enigma stores and
  delivers the connection string and nothing more.
- The command registry and service lifecycle — **Epics 10** and **11**.
- Cross-service URL values (`API_BASE_URL` derived from a sibling's preview URL) — **Epic 13**.
- Team- or project-level shared secrets (listed under Future Enhancements).

## Backend Tasks

- **Share one environment resolver**
  - Extract the duplicated `TerminalService.environment` / `CommandService._workspace_env` logic
    into `services/workspace_env.py`: `resolve(workspace_id, *, service_id=None, worktree_id=None)`
    → `dict[str, str]`, applying scope precedence (service-specific overrides workspace-wide).
  - Call it from `terminal.py`, `commands.py`, `dev_server.py`, and Epic 11's service launcher, so
    there is exactly one place that decrypts secrets.
- **Inject into the dev server / services** (`services/dev_server.py`)
  - Pass `env=` to the `runtime.exec` calls that launch setup and run. **Never** interpolate values
    into the script body — see the `set -x` leak above.
  - Verify the `env K=V` prefix survives the `nohup sh -c '…' &` wrapper (the prefix applies to the
    outer shell, so the background script inherits it) and add a test asserting a variable reaches
    the launched process.
  - Keys are already validated on write; also reject keys that would break the prefix
    (non `[A-Za-z_][A-Za-z0-9_]*`) with a clear error rather than silently dropping them.
- **Bulk endpoints**
  - `POST /workspaces/{id}/env-vars/bulk` — accepts a `.env`-formatted body or a key/value map;
    parses `KEY=value`, `export KEY=value`, quoted and multi-line values, `#` comments; returns a
    per-key result (`created` | `updated` | `unchanged` | `invalid`) so the UI can show a diff
    before and after.
  - `DELETE` accepting a list of keys.
  - `GET /workspaces/{id}/env-vars/template` — exports `KEY=` lines with **values omitted**, for
    sharing a `.env.example` with a teammate. Never an endpoint that returns values.
- **Scoping**
  - `workspace_env_vars.scope` (`workspace` | `service`) + nullable `custom_command_id`; relax the
    unique constraint to `(workspace_id, key, custom_command_id)` with a partial unique index for
    the workspace-wide row.
- **`.env` materialisation** (opt-in per workspace)
  - `env_file_path` on the workspace (relative to the app root, default unset). When set, write the
    resolved variables to that path inside the sandbox before a service starts, `chmod 600`, and
    ensure the path is git-ignored — appending to `.git/info/exclude` rather than editing the
    repository's `.gitignore`, so the file can never be committed.
  - Rewrite on every service start so the file cannot drift from the database.
- **Change propagation**
  - `PUT`/bulk responses report which running services consume the changed keys (Epic 11 state), so
    the UI can offer a restart. Do not restart implicitly.
- **Redaction**
  - Route every log tail, command output, and audit metadata write through
    `git_providers.redact_secrets`, extended to redact the *current* workspace's env values (not
    just token-shaped strings) before persistence.

## Frontend Tasks

- Rebuild `EnvVarManager` (`DashboardRoute.tsx:1415`) into an env editor:
  - A table of keys with scope badges, an inline **Replace value** action, and multi-select delete.
  - **Bulk edit** mode: a textarea accepting pasted `.env` text, with a parsed preview
    (`12 new · 3 updated · 1 invalid`) and an explicit confirm.
  - **Export template** button (keys only) and a copy-to-clipboard.
  - A permanent, plain note that values cannot be read back — the mask is a design choice, not a
    bug — so users are not confused when a value shows as `••••`.
- Scope selector on each variable: *All services* or specific registry entries.
- After a save, a toast: "`DATABASE_URL` changed — restart API?" with a restart button.
- Show a warning when a workspace has services defined but no variables saved, since that is the
  most common cause of a preview that starts and then 500s.

## Database Tasks

- Migration (reversible): `workspace_env_vars.scope` (CHECK in (`workspace`, `service`)) and
  `custom_command_id` (nullable FK, `ON DELETE CASCADE`); replace
  `uq_workspace_env_vars_workspace_key` with a partial unique index on
  `(workspace_id, key) WHERE custom_command_id IS NULL` plus a unique index on
  `(workspace_id, key, custom_command_id)`.
- `workspaces.env_file_path` (nullable, validated repo-relative).
- Backfill: every existing row becomes `scope='workspace'`, `custom_command_id = NULL`.

## Security Requirements

- Values stay encrypted at rest (`WorkspaceSecretCipher`) and are decrypted only in the resolver,
  immediately before an `exec`/PTY call.
- **No endpoint ever returns a value.** Keep `mask_env_value()`; the template export emits keys
  only.
- Injection happens exclusively through `DaytonaRuntime`'s `env` parameter. Adding a value to a
  command string, a script body, or anything under `set -x` is a defect, and a test should assert
  that no env value appears in `LOG_PATH`.
- Audit entries record the **key** changed, never the value (the current audit metadata pattern
  already does this for other domains — keep it).
- A materialised `.env` file is `600`, inside the sandbox only, and excluded from git via
  `.git/info/exclude`.
- The env prefix is built with `shlex.quote` (already the case in `daytona_runtime.py:227`); keep
  that when the resolver grows.
- An external database connection needs sandbox egress: when `DAYTONA_NETWORK_BLOCK_ALL=true`,
  the database host and port must be added to `DAYTONA_NETWORK_ALLOW_LIST` (`config.py:41-42`).
  Surface a clear error when a service cannot reach its host, instead of a generic startup failure.

## Acceptance Criteria

- A FastAPI service in a workspace with `DATABASE_URL` set to an external database starts, serves,
  and answers a query through its preview URL — with no repository changes.
- A pasted 15-line `.env` saves in one action, with a diff preview beforehand.
- No environment value appears in `/tmp/enigma-dev.log`, a command run's stored output, an audit
  entry, or any API response — asserted by tests.
- The same key can hold different values for two services, and each service receives its own.
- With `env_file_path` set, the file exists in the sandbox with mode `600` and is not shown as an
  untracked change in the repository status.
- Changing a variable surfaces which running services are affected and offers a restart.
- Existing workspaces keep working after the migration, with no behaviour change for terminal and
  command runs.

## Dependencies

- **Epic 10** for `custom_commands` (scoping targets) — required for the scoping half; the
  injection half is independent and can ship immediately.
- **Epic 11** to know which services are running for the restart prompt.

## Future Enhancements

- Project/team-level shared variables inherited by workspaces, with per-workspace overrides.
- Import from a `.env.example` in the repository to pre-create keys with empty values.
- A managed-secret integration (Doppler, 1Password, Vault, AWS Secrets Manager) as a variable
  source instead of stored values.
- Per-worktree overrides so a feature branch can point at a different external database.

## Implementation Notes (shipped)

Landed in one pass; migration head is now `0026_env_var_scoping`.

### Corrections to this document, found while implementing

1. **The "Current State" section was already partly stale.** Epics 10/11 had shipped a
   module-level `load_workspace_env` and `redactable_env_values` in `services/commands.py`
   before this epic started, so command-run output and the Epic 11 service log stream were
   already redacted. The two real remaining leaks were `DevServerService._read_log_tail`
   (produced no redaction at all, and reached the browser via `DevServerState.message`) and
   `CommandService._execute_run`'s exception path (appended a raw error string to `CommandRun.output`
   without redaction). Both are fixed.
2. **Audit metadata for env-var changes was already silently empty**, not merely
   theoretically leaky: `safe_metadata()` (`services/audit.py`) drops any metadata dict key whose
   name contains `"token"`, `"secret"`, `"password"`, `"key"`, or `"credential"` — and the existing
   calls used `metadata={"key": env_var.key}`, so every pre-epic env-var audit row persisted
   `log_metadata == {}`. Fixed by renaming the field to `variable`/`variables`, which does not
   collide with the filter.

### Architecture

- **One decrypt point**: `services/workspace_env.py` now owns every read of
  `WorkspaceEnvVar.encrypted_value`. `resolve(session, settings, workspace_id, *,
  custom_command_id=None)` returns the env for one launch (workspace-wide rows, overridden by
  that command's scoped rows); `load_all_values(...)` returns every scope's values for building
  a redaction tuple only, never handed to a process. `terminal.py`, `commands.py`,
  `workspace_services.py`, `dev_server.py`, and the service log websocket all call through it;
  `commands.load_workspace_env` is now a one-line back-compat shim over `resolve`.
- **Injection is exclusively via the Daytona SDK's `env=` parameter.** Both launch sites
  (`WorkspaceServiceManager._launch` and `DevServerService._launch_setup`) call a new
  `prepare_launch_env(...)` before building the launch script, then pass `env=env or None` into
  `runtime.exec`. No value is ever interpolated into a script body, so nothing under the
  launch scripts' `set -x` can leak a secret. Verified by tests asserting the secret string is
  absent from every recorded `runtime.exec` command string while present in the recorded `env`
  dict. `daytona_runtime.stream_command` additionally validates every key against
  `^[A-Za-z_][A-Za-z0-9_]*$` before building its `env K=V` prefix, as defense in depth (keys are
  already validated on write).
- **`.env` materialisation** (`workspace_env.materialize_env_file`) is a true no-op when
  `workspace.env_file_path` is unset (no sandbox calls at all). When set, the file is written via
  `sandbox.fs.upload_file` + `set_file_permissions(mode="600")` — never through a command string —
  and one idempotent `runtime.exec` (containing only the file *path*, never a value) appends the
  pattern to the result of `git rev-parse --git-path info/exclude`, resolved dynamically because a
  linked worktree's `.git` is a file, not a directory, so a hardcoded
  `<checkout>/.git/info/exclude` would fail there. Failure to update the exclude file is a logged
  warning, not fatal; failure because the parent directory doesn't exist in the checkout raises
  `EnvFileError`, which both launch sites turn into `status="error"` rather than a silent skip.
- **Scoping**: `workspace_env_vars.scope` (`workspace`|`service`) + nullable `custom_command_id`,
  with a partial unique index for workspace-wide rows and a composite unique index for
  service-scoped rows (Postgres treats `NULL` as distinct in a plain unique index, so the
  workspace-wide half needed the partial index). `resolve()` and `affected_running_services()`
  both treat a service-scoped row as shadowing a workspace-wide row of the same key for that
  service only.
- **Bulk editing**: `POST /workspaces/{id}/env-vars/bulk` accepts either a pasted `.env`-formatted
  `text` or a `values` map (exactly one), parsed by the new pure `services/env_file.py`
  (`parse_env_text`/`serialize_env` — `export` prefix, single/double-quoted values with escapes,
  multi-line double-quoted values, inline comments only after whitespace, no shell interpolation).
  `dry_run: true` computes the same per-key `created|updated|unchanged|invalid` diff without
  writing anything, so the frontend's preview-then-apply flow needed no separate endpoint.
  `GET /env-vars/template` returns `text/plain`, keys only, grouped by scope as comments.
- **Change propagation**: `workspace_env.affected_running_services(...)` computes which active
  `WorkspaceService` rows a changed key affects (all of them for a workspace-wide key, minus any
  service with its own override for that key; exactly one for a service-scoped key) and is
  returned on every upsert/bulk/delete response as `affected_services`, even in `dry_run` mode.
  Nothing is restarted automatically — the frontend shows a banner with a Restart button per
  affected service, calling the pre-existing `WorkspaceServiceManager.restart` /
  `POST .../services/{id}/restart` from Epic 11.
- **Egress warnings**: `workspace_env.egress_warnings(...)` is a no-op unless
  `DAYTONA_NETWORK_BLOCK_ALL` is set; otherwise it scans newly-submitted values for a URL-shaped
  or libpq-style host, skips loopback/localhost, and warns (naming only the key/host/port, never
  the value) when an IP-literal host isn't covered by any `DAYTONA_NETWORK_ALLOW_LIST` CIDR, or
  advisorially when the host is a hostname (which can't be checked against CIDRs at save time).

### Frontend

`EnvVarManager`'s single-key-at-a-time rail component is replaced by
`components/EnvVarEditor/`, moved out of the 360px settings rail into a full-width section:
grouped read rows with scope badges, inline replace-value, multi-select delete through the
existing `ConfirmModal`, a bulk-edit mode (paste → preview counts → apply), export-template with
the existing copy-to-clipboard-with-timeout pattern, an `.env` file path toggle, and an
`AffectedServicesBanner` with a per-service Restart button. Gated by the pre-existing
`workspace.manageEnv` permission, which no component had actually checked before this epic.

### Verification

`ruff` clean; **618 backend tests pass** (was 593 before this epic's test-writing — net new: 25
scoping/bulk tests plus the injection/materialisation/redaction tests from earlier in the epic).
One test, `test_settings.py::test_command_deny_dangerous_defaults_to_true`, fails only when run as
part of the full suite (order-dependent env-var pollution) and passes in isolation — confirmed
pre-existing on a clean checkout, unrelated to this epic. Migration `0026_env_var_scoping` verified
reversible (`upgrade → downgrade -1 → upgrade`) against local Postgres, including a documented
lossy dedupe on downgrade for genuinely-scoped rows. Frontend: eslint clean (4 pre-existing
warnings), **tsc -b** and **vite build** succeed, **205 tests pass** (1 pre-existing skip, up from
199).

Not covered by automated tests, and still worth a manual pass against a live Daytona sandbox: a
FastAPI service with `DATABASE_URL` set to a real external database actually answering a query
through its preview URL, and confirming a materialised `.env` file is genuinely absent from `git
status` inside a real sandbox checkout (the exclude-file append is unit-tested against a fake
runtime only).
