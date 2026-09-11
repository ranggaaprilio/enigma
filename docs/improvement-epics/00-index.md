# Enigma Improvement Epics

This folder breaks the post-MVP improvement backlog into AI-ready, executable epics.
It complements (does not replace) `docs/implementation-plan/`, which covers the original
MVP build (Epics 0–11). Everything here assumes the MVP workflow works end to end:
Projects → Teams → Workspaces, Daytona sandboxes, GitHub clone/commit/push, the
OpenAI/OpenHands agent, terminal, code-server editor, preview, files, commands,
notifications, and audit logs.

Use the same per-epic execution pattern described in
`docs/implementation-plan/13-ai-handoff-template.md`: implement one epic at a time,
add migrations + tests, keep changes scoped.

## The Epics

| # | Epic | Priority | Theme |
|---|------|----------|-------|
| 01 | [Spec-Driven Development + Human-in-the-Loop](./01-spec-driven-development.md) | **P1** | Quality / process |
| 02 | [VCS: Pull Requests & Branch Workflow](./02-vcs-pr-and-branch-workflow.md) | **P1** | Core product loop |
| 03 | [Agent Editing Tools (patch / search / safe git)](./03-agent-editing-tools.md) | P2 | Agent capability |
| 04 | [Anthropic / Claude Agent Provider](./04-anthropic-claude-provider.md) | P2 | Agent quality |
| 05 | [Multi-Turn Agent Sessions](./05-multi-turn-agent-sessions.md) | P2 | Agent UX |
| 06 | [Cost & Usage Analytics](./06-cost-and-usage-analytics.md) | P2 | Operations |
| 07 | [Observability, Production Hardening & CI](./07-observability-and-hardening.md) | P2 | Reliability |
| 08 | [Real-Time Collaboration](./08-realtime-collaboration.md) | P3 | Collaboration |
| 09 | [Workspace Templates & Multi-Provider Git](./09-templates-and-git-providers.md) | P3 | Onboarding / growth |
| 10 | [Run Configuration & Unified Command Registry](./10-run-configuration-and-commands.md) | **P1** ✅ shipped | Full-stack preview |
| 11 | [Long-Running Services & Multi-Port Preview](./11-multi-service-preview.md) | **P1** ✅ shipped | Full-stack preview |
| 12 | [Environment Variables: Injection, Bulk Editing & Scoping](./12-environment-variables-ux.md) | **P1** | Full-stack preview |
| 13 | [Service Wiring & Preview Composition](./13-service-wiring-and-preview-composition.md) | P2 | Full-stack preview |

## Recommended Build Order

These epics are mostly independent, but there are a few dependencies. A pragmatic order:

1. **04 — Claude provider** (small, ~1–2 days). Adds a first-class high-quality coding
   model and a per-workspace provider/model selector. Makes every later agent-driven
   epic better, especially SDD.
2. **02 — PR & branch workflow** (closes the "AI writes code → review → ship" loop).
3. **01 — Spec-Driven Development + Human-in-the-Loop** (the headline process change).
   Benefits from 02 (it ends in a PR gate) and 04 (better spec/plan generation), but can
   start in parallel using existing providers.
4. **03 — Agent editing tools** (cheaper, safer edits; helps SDD `implement` phase).
5. **07 — Observability & hardening + CI** (foundational; can run in parallel from day 1,
   especially the CI gate, since the repo currently has no test/lint CI).
6. **05, 06, 08, 09** as capacity allows.

### Epics 10-13: the full-stack preview track

Epics 10-13 form one coherent track and should be built in order, because each defines schema the
next one consumes. They exist because the preview pipeline today only works for a **single-process
frontend at the repository root**; a repo with a frontend and a backend, a monorepo, or a
backend-only API cannot be previewed without them.

1. **12 (injection half only)** — one focused change: pass `env=` to the dev-server launches so
   workspace environment variables actually reach the running app. Without it, a backend preview
   starts and immediately fails on its first database call. Independent of 10 and 11, so it can
   land on its own in a day.
2. **10 — Run configuration & command registry.** ✅ **Shipped** (migration
   `0024_run_configuration`). Defines `app_root`, and turns `custom_commands` into the single
   registry of runnable things (role, working directory, port, ordering, per-command timeout).
   Everything else builds on this schema.
3. **11 — Services & multi-port preview.** ✅ **Shipped** (migration `0025_workspace_services`).
   Replaces one-dev-server-per-sandbox with N tracked `workspace_services` rows, each with its own
   lifecycle (start/stop/restart), log stream, and preview session.
4. **12 (remainder)** — bulk `.env` editing, per-service scoping, `.env` materialisation.
5. **13 — Service wiring.** Stable preview aliases, `${ENIGMA_PREVIEW_URL:api}` templates,
   dev-proxy detection, CORS diagnosis, health probes.

**Quick win available before any of this lands:** a repository can already be previewed by shipping
a root `mise.toml` whose `dev` task fans out (`depends = ["dev:api", "dev:web"]`) with the frontend
dev server proxying `/api` to `localhost:8000`. That keeps everything inside the existing
single-port, single-process contract. Epics 10-13 exist to remove the requirement that users
discover and hand-author that arrangement.

## Dependency Notes

- **01 (SDD)** reuses the agent-run streaming/events infrastructure and is most useful
  with **02** (PR gate), **03** (patch edits in `implement`), and **04** (Claude).
- **06 (cost analytics)** is unblocked today because `agent_runs.tokens_used` already
  exists — it is simply not surfaced. **04** should make providers report token usage
  consistently.
- **08 (collaboration)** depends on **07**'s WebSocket reconnect work.
- **10 → 11 → 13** is a hard chain: 11 needs 10's `kind='service'` + `port`, and 13 needs 11's
  running services plus 12's environment resolver.
- **12** is the only epic in the track with a genuinely independent, immediately shippable slice
  (env injection into the dev server). Do that slice first regardless of when the rest is scheduled.
- **10** builds on **02**'s worktrees (per-branch command execution) and **09**'s
  `workspace_templates` (presets ship with templates).
- Database provisioning is explicitly **out of scope** for the whole track: workspaces connect to
  an external database through environment variables. The only platform requirement is sandbox
  egress to the database host (`DAYTONA_NETWORK_ALLOW_LIST`).

## Shared Rules For Every Epic

- Clear API contracts before UI.
- A database migration for every schema change (Alembic, async engine).
- Backend tests (`pytest`) for service logic + routes; frontend tests (Vitest) for
  critical state transitions.
- Never store secrets in plain text — reuse `WorkspaceSecretCipher` (Fernet).
- Apply authorization checks consistently (project/team/workspace membership + role).
- Keep backend modules organized by domain (`api/`, `models/`, `schemas/`, `services/`).

## Grounding: Current State Snapshot (June 2026)

Verified against the codebase so epics target real gaps, not assumptions:

- **Agent providers**: only `openai` (`agent_providers.py`) and `openhands`
  (`openhands_provider.py`). No Anthropic/Claude. `agent_provider` defaults to `"auto"`.
- **Agent tools** (`agent_tools.py`): `list_files`, `read_file`, `write_file`
  (full-file overwrite), `run_command`. `git` is in the `DANGEROUS_COMMANDS` denylist;
  there is no patch-edit or search/grep tool.
- **Git** (`repositories.py`): GitHub-only (`sanitize_repository_url` rejects non
  `github.com`); supports clone/status/diff/pull/commit/push. Commit author is
  hardcoded `"Execute Dashboard" <execute@localhost>` (`repositories.py:241`). No PR
  or branch-management endpoints.
- **Agent runs** (`models/agent.py`): one `prompt` per run, no conversation/parent
  linkage. `tokens_used` column exists but is not surfaced anywhere.
- **Health/observability**: only `/health` exists. No `/ready`, `/metrics`, request-id
  middleware, or Prometheus metrics. Rate limiting covers login only.
- **CI**: only `.github/workflows/ai-code-review.yml` (AI PR review). No test/lint/
  typecheck/build gate. 18 backend test files, 6 frontend test files.
- **Sandbox**: Daytona, custom snapshot built via `daytona/build-snapshot.sh` +
  `daytona/Dockerfile` (mise + code-server baked in; runtime egress is restricted).

## Grounding: Full-Stack Preview Snapshot (September 2026)

Verified against the codebase for Epics 10-13. This is a point-in-time record of *why* these
epics exist — it is deliberately **not** rewritten as they land. **Epics 10 and 11 have since
shipped**, so the following bullets no longer describe the code (see each epic's "Implementation
Notes"):

- *Dev server* — `mise run dev` is now the default, not the hard-coded behaviour. A workspace with
  an enabled `role='dev'` registry entry runs that instead, in its own working directory, and no
  root `mise` config is required. Everything else in the bullet (single status-file set, `pkill`,
  single-valued desired state) still stands and is Epic 11's problem.
- *Preview* — `PreviewSession` rows are now upserted per running `workspace_services` row with a
  confirmed port, so a frontend on 5173 and an API on 8000 both get their own preview URL. The
  legacy single-port scan (`_first_open_port`) survives only as the manual-detect fallback for a
  workspace with no service rows yet.
- *Dev server (continued)* — the single-slot constraint is gone: `workspace_services` tracks N
  running instances, each with its own `/tmp/enigma-svc-<id>.*` state files (the *primary* row —
  whichever `DevServerService`'s `/dev/*` facade controls — keeps the legacy `/tmp/enigma-dev.*`
  paths for byte-for-byte compatibility). Stop targets a recorded pid tree, not a `pkill` pattern,
  except for the primary row's legacy fallback.
- *Custom commands* — the listed gaps are closed: working directory, worktree awareness, role,
  port, ordering and a real timeout (`command_run_timeout_seconds`, 900s) all exist, and the
  denylist no longer applies to owner-authored commands.
- *Environment variables* — command-run output is now redacted with `redact_secrets`. The
  dev-server `env=` gap is untouched and remains Epic 12's first slice.

The snapshot as originally written follows.

- **Dev server**: `services/dev_server.py` runs exactly `mise run dev` (and `mise run install`),
  requires a `mise` config at the checkout root (`MISE_CONFIG_FILES`), and tracks state in three
  fixed sandbox paths (`/tmp/enigma-dev.{status,log,gen}`). One path set = one dev server per
  sandbox; stop and worktree-switch use `pkill -f '[m]ise run dev'`. Desired state is single-valued
  on the workspace (`dev_autostart`, `dev_worktree_id`).
- **Preview**: `COMMON_PREVIEW_PORTS = (3000, 5173, 8000, 8080)` is hard-coded and
  `_first_open_port` returns the *first* match, so a frontend on 5173 hides an API on 8000. The
  `preview_sessions` table is already multi-row per workspace and already has `worktree_id`; only
  `detect()` is single-port. Preview URLs are signed capabilities in the **subdomain** and are
  re-issued near expiry, so a preview hostname **rotates** (default TTL 1 hour).
- **Preview proxy**: already handles every HTTP method plus WebSockets, so serving an API through
  it needs no transport work.
- **Custom commands**: `custom_commands` / `command_runs` already provide CRUD, streamed output
  over WebSocket, run history, notifications, audit entries, **and** workspace env-var injection
  (`_workspace_env`). Missing for dev use: working directory, worktree awareness, lifecycle role,
  port, ordering, and a usable timeout — user commands share `agent_command_timeout_seconds`
  (**20s**), and `pnpm`/`npm`/`uv`/`git` plus `&&`/`|`/`;` are in the agent denylists, so ordinary
  dev commands are rejected unless flagged `requires_confirmation`.
- **Environment variables**: stored encrypted (`workspace_env_vars`, Fernet) and injected into
  **terminal sessions and command runs only** — `dev_server.py` passes no `env=` at all. Values are
  never returned by the API (`mask_env_value()` is a constant mask), and the UI edits one key at a
  time (`EnvVarManager` in `DashboardRoute.tsx`).
- **Secret-leak path to avoid**: the dev-server launch scripts start with
  `exec >/tmp/enigma-dev.log 2>&1` + `set -x`, and `_read_log_tail` surfaces that log in the UI —
  so environment values must be injected through the runtime's `env` parameter, never written into
  a script body.
- **Daytona primitives available**: `DaytonaRuntime.stream_command` already creates a Daytona
  session, runs `run_async=True`, and streams logs — a better foundation for long-running services
  than `nohup` + status files, but it deletes the session when the stream ends (correct for
  one-shot commands, wrong for services).
