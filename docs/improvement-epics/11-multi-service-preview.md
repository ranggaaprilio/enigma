# Epic 11 - Long-Running Services & Multi-Port Preview

## Goal

Replace "one dev server per sandbox" with **N concurrently running services per workspace**, each
with its own lifecycle (start / stop / restart), its own log stream, and its own preview URL. This
is what makes a full-stack repository previewable: a frontend on `5173` and an API on `8000`
served at the same time, from the same sandbox, on the same branch.

## User Value

The user clicks **Run** on each service they care about and gets one preview link per service —
a web UI to open and an API base URL to call — plus per-service logs to debug a crash without
digging through a single merged log file.

## User Stories

- As a full-stack developer, I start `dev:api` and `dev:web` and see two live preview URLs.
- As a developer, my API crashes; I restart **only** the API and the frontend keeps its state.
- As a developer, I open a service's log tail in the UI instead of guessing from a status pill.
- As a developer, I stop the frontend to save sandbox resources while I work on the API.
- As a developer switching worktrees, I choose whether the running services follow the branch.

## Current State

**Exactly one dev server can exist per sandbox, by construction:**

- `services/dev_server.py` launches `nohup sh -c '…' &` and tracks state in three fixed files
  inside the sandbox: `STATUS_PATH`, `LOG_PATH`, `GEN_PATH` (`dev_server.py:22-28`). One path set
  means one process's state.
- A generation token guards status writes so a killed launch cannot clobber its replacement
  (`_run_command`, `dev_server.py:470`) — a workaround for having only one status file.
- Stop and worktree-switch use `pkill -f '[m]ise run dev'` (`stop`, `_stop_processes`), which is
  process-pattern-based and cannot target one service among several.
- Desired-state is single-valued on the workspace: `dev_autostart` and `dev_worktree_id`
  (`models/workspace.py:81`). `ensure_running` (`dev_server.py:237`) reconciles exactly one server
  after a sandbox sleep/wake.

**Preview resolves to a single port:**

- `COMMON_PREVIEW_PORTS = (3000, 5173, 8000, 8080)` is a hard-coded tuple (`preview.py:24`), and
  `_first_open_port` returns the **first** open port (`preview.py:229`). With a frontend on `5173`
  and an API on `8000`, the API never receives a URL.
- Port `8000` is ambiguous in that tuple: it is FastAPI/Django's default *and* a common frontend
  port, so first-match ordering is a coin flip on some stacks.

**What already supports this epic:**

- `preview_sessions` is **already multi-row per workspace** and already carries `worktree_id`
  (`models/preview.py`); `list_for_workspace` returns and re-signs all of them
  (`preview.py:52`). Only `detect()` is single-port.
- The preview proxy already handles every HTTP method and WebSockets
  (`preview_proxy.py:30`, `preview_proxy.py:176`), so an API — including streaming endpoints —
  works through it unchanged.
- `DaytonaRuntime.stream_command` already creates a **Daytona session** and runs the command with
  `run_async=True`, then follows it with `get_session_command_logs_async`
  (`daytona_runtime.py:208`). This is a per-command lifecycle primitive with real log streaming —
  a far better foundation than `/tmp` status files. It currently deletes the session in `finally`,
  which is correct for a one-shot command and wrong for a service.

## Architecture

Promote `custom_commands.kind = 'service'` (Epic 10) into a running instance tracked in a new
`workspace_services` table:

```
custom_commands (definition: name, command, cwd, port, role, kind)
        │  1:N
        ▼
workspace_services (runtime state: worktree_id, status, pid/session id, port, desired, last_error)
        │  1:1 while active
        ▼
preview_sessions (existing: port, worktree_id, signed capability URL)
```

- **Definition vs. instance** is the key split. The command says *what* to run; the service row
  says *what is running now, where, and whether the user wants it running*.
- `desired` replaces the workspace-wide `dev_autostart`, per service.
- Per-service state files (`/tmp/enigma-svc-<service_id>.{status,log,gen}`) remove the single-slot
  constraint immediately, and the generation-token trick becomes per service rather than global.

**Spike before committing to sessions:** determine whether a Daytona session command survives
sandbox auto-stop/wake and a backend restart. If it does, sessions replace the `nohup` +
status-file machinery and give native logs. If it does not, keep `nohup` for the process and use
sessions only for log tailing — either way `ensure_running` reconciliation stays necessary.

## Scope

- Per-service start / stop / restart, with `desired` state and auto-resume after sandbox wake.
- Per-service status and log tail, streamed to the UI.
- Multi-port preview: one `PreviewSession` per running service port.
- Configurable ports (from `custom_commands.port`) instead of the fixed tuple.
- Worktree switching semantics for a set of services.
- `DevServerService` kept as a compatibility facade so Epic 10's seeded `dev` entry still works.

### Non-Goals

- The registry, `app_root`, and detection — **Epic 10**.
- Env-var injection into services — **Epic 12** (this epic must not ship secrets through the
  traced launch script; see that epic's leak note).
- Cross-service URL wiring and CORS — **Epic 13**.

## Backend Tasks

- **New `services/workspace_services.py`**
  - `start(workspace, command_id, worktree_id)`, `stop(service_id)`, `restart(service_id)`,
    `status(workspace)` → list, `logs(service_id, tail)`.
  - Launch per service with its own state files; stop by recorded pid/session id, not by
    `pkill -f` pattern. Keep a pattern-based sweep only as a last-resort fallback.
  - `ensure_running(workspace)` reconciles **every** service with `desired = true` — the
    multi-service successor to `DevServerService.ensure_running`, keeping its safety checks
    (never double-launch; verify the process is actually dead before relaunching).
  - Port conflict detection: refuse to start a service whose port is already bound by another
    service, with an actionable message.
- **Preview multi-port** (`services/preview.py`)
  - Replace `_first_open_port` with a scan that returns every open port of interest, and upsert
    one `PreviewSession` per port; keep the existing capability-URL renewal logic in `_proxy_url`
    untouched.
  - Take candidate ports from the workspace's registry entries, falling back to
    `COMMON_PREVIEW_PORTS` for repositories with no configuration yet.
  - Link `preview_sessions` to the service that owns the port so the UI can label it
    ("Web · 5173", "API · 8000") instead of showing bare numbers.
  - Mark a session `error`/`stopped` when its service stops, instead of leaving a stale `active`.
- **Log streaming**
  - Reuse the command WebSocket pattern (`api/commands.py:221`) for a per-service stream; a
    service is a long-lived `CommandRun`-like entity, so keep one output ring buffer per service
    bounded by `agent_max_command_output_chars`.
- **Worktree switching**
  - Today a switch force-stops the single dev server. Make it explicit: on switch, offer
    "restart running services on the new branch" vs. "leave them". Record `worktree_id` per
    service so mixed states are at least *visible* rather than silently wrong.
- **Compatibility**
  - Keep `/workspaces/{id}/dev/*` endpoints working, delegating to the new service for the seeded
    `dev` entry, so `PreviewPanel` and the auto-resume poller keep functioning during the
    migration.

## Frontend Tasks

- Rework `PreviewPanel.tsx` from a single session into a **service list**: name, role, port,
  status pill, branch, Run / Stop / Restart, and **Open preview** per row.
- Per-service log drawer (reuse the streaming output component from `CustomCommandsPanel.tsx`).
- Show all preview URLs at once, with a copy button — the API URL is something the user pastes
  into a client, not something they click.
- Port-conflict and "service exited (code 1)" states with the log tail inline, following the
  existing pattern of surfacing `_read_log_tail` output in the status message.
- Keep the auto-resume poll loop, but drive it from the service list instead of one status value.

## Database Tasks

- Migration (reversible): `workspace_services` (`id`, `workspace_id`, `custom_command_id`,
  `worktree_id` nullable FK, `port`, `status` CHECK in
  (`starting`,`running`,`stopped`,`error`,`exited`), `desired` bool, `session_id`/`pid` nullable,
  `last_error` text, `started_at`, `last_seen_at`), unique on
  (`workspace_id`, `custom_command_id`), index on (`workspace_id`, `status`).
- `preview_sessions.service_id` (nullable FK, `ON DELETE SET NULL`).
- Migrate the existing single-slot columns: derive one `workspace_services` row from
  `workspaces.dev_autostart` + `dev_worktree_id`, then keep those columns for one release
  (deprecated) before dropping them.

## Security Requirements

- Ports come from server-side registry rows; a client may never pass an arbitrary port into
  `create_preview_proxy_url` — the capability token binds `(preview_session_id, port)` and must
  stay derived from stored state (`preview_capability.py`).
- Preview URLs remain signed, expiring capabilities; nothing about multi-port changes the TTL or
  renewal-window logic.
- A service's log output is user-visible: apply `redact_secrets` before persisting or streaming.
- Cap the number of concurrently running services per workspace (a sandbox is a shared, sized
  resource) and surface the limit in the UI.
- Authorisation on every service action, consistent with `api/dev_server.py`.

## Acceptance Criteria

- A Vite frontend (`5173`) and a FastAPI backend (`8000`) run simultaneously in one workspace,
  each with its own working preview URL.
- Restarting the API leaves the frontend running and its preview URL valid.
- Stopping a service marks its preview session non-active; the other services are untouched.
- After a sandbox auto-stop/wake, every service marked `desired` comes back, and none is
  double-launched.
- Starting two services on the same port fails with a clear message and no partial state.
- A backend-only workspace previews on `8000` with no frontend present.
- Per-service logs are visible in the UI within a second of being written.

## Dependencies

- **Epic 10** (registry, `kind='service'`, `port`, `cwd`) — required.
- **Epic 12** for anything needing credentials at runtime (an API without `DATABASE_URL` starts
  and then fails on first request).
- Reuses **Epic 02** worktrees and the existing preview proxy.

## Future Enhancements

- Health/readiness probes per service (`GET /health`) so status reflects usable, not just bound.
- Service dependency ordering (start the API before the web app).
- Resource usage per service, feeding Epic 06's usage analytics.
- One preview host per service with stable subdomains instead of rotating capability tokens.
