# AGENTS.md — Execute Dashboard (enigma)

## Monorepo Boundary

| Package | Language | Package Manager | Location |
|---------|----------|----------------|----------|
| backend | Python 3.12+ | `uv` | `backend/` |
| frontend | TypeScript 5 | `pnpm` | `frontend/` |
| root | — | pnpm (workspace) | `.` |

The pnpm workspace only includes `frontend/`. Backend is standalone Python managed by `uv`, not part of the Node workspace.

## Exact Commands

### Full Stack (requires podman or docker + .env)
```bash
cp .env.example .env   # then fill DAYTONA_API_KEY
podman compose up --build    # or: npm run dev
```

### Backend (`backend/`)
```bash
uv sync                          # install deps
uv run uvicorn app.main:app --reload  # dev server on :8000
uv run pytest                    # all tests (async auto-mode)
uv run ruff check .              # lint (E, F, I, UP, B rules)
uv run alembic upgrade head      # run migrations
uv run alembic revision -m "…"   # create migration
uv run arq app.worker.WorkerSettings  # run worker (only with RUN_EXECUTION_MODE=queue)
```

### Frontend (`frontend/`)
```bash
pnpm install                     # install deps
pnpm dev                         # Vite dev server on :5173
pnpm build                       # tsc -b && vite build
pnpm lint                        # ESLint (flat config, typescript-eslint)
pnpm test                        # Vitest (jsdom, @testing-library/react)
```

### Utility
```bash
./scripts/clean_data.sh --yes    # flush Redis + recreate Postgres schema + re-migrate
```

## Architecture

### Backend (FastAPI + SQLAlchemy)
- **Entrypoint**: `backend/app/main.py` → `app:app` (FastAPI with CORS + lifespan)
- **Router**: `backend/app/api/router.py` aggregates 22 sub-routers
- **Config**: `backend/app/core/config.py` via `pydantic-settings` (reads `.env` from CWD, `extra="ignore"`)
- **Database**: PostgreSQL via SQLAlchemy 2.0 async + `asyncpg` driver (NOT psycopg2)
  - Connection string must be `postgresql+asyncpg://…`
  - Session: `async_session_maker` with `expire_on_commit=False`
  - All DB access MUST use async/await
- **Migrations**: Alembic with async engine, 32 migrations in `alembic/versions/` (`NNNN_snake_case.py`, `revision` = filename stem; head `0032_agent_events_stream_index`)
- **Auth**: Cookie-based sessions (`execute_session` cookie), NOT JWT. Set-Cookie on login, cleared on logout.
- **Logging**: `structlog` (configured in `core/logging.py`)
- **Testing**: `pytest` with `pytest-asyncio` (auto mode), tests at `backend/tests/`

### Frontend (React 18 + Vite + Tailwind 3)
- **Routing**: `react-router` v7 (`BrowserRouter`, `Routes`, `Outlet`); all routes declared in `src/App.tsx`
- **No component library** — all UI built with Tailwind utility classes. Do NOT assume shadcn/ui, Radix, or any component kit.
- **No state library** — React Context only. Main hook: `useAuth()` from `src/auth/context.ts`
- **API client**: `src/lib/api.ts` (~1500 lines). All HTTP calls go through exported typed functions — never `fetch()` directly.
  - Auth uses `credentials: "include"` for cookie-based sessions
  - WebSocket helpers: `getAgentStreamUrl()`, `getTerminalSessionSocketUrl()`, `getCommandRunStreamUrl()`, `getNotificationStreamUrl()`
- **Styling**: Tailwind CSS 3 with a custom Cursor-inspired palette (`canvas`, `panel`, `ink`, `primary` #f54e00, etc.). Fonts: Inter (UI), JetBrains Mono (code/terminal).
- **Terminal**: `@xterm/xterm` + `@xterm/addon-fit` in `TerminalPanel.tsx`
- **Panels**: domain UIs live in `src/components/` — `AgentTab/`, `SpecPanel/`, `WorktreesPanel.tsx`, `EditorPanel.tsx`, `PreviewPanel.tsx`, `TerminalPanel.tsx`, `FileManagementPanel.tsx`, `CustomCommandsPanel.tsx`
- **Active worktree**: `src/lib/activeWorktree.ts` persists the selected worktree per workspace in `localStorage` and broadcasts an `enigma:active-worktree` window event. The selection drives which branch the agent and terminal target.
- **Icons**: `lucide-react` (only)
- **Testing**: Vitest + jsdom + `@testing-library/react`. Setup file: `src/test/setup.ts`

### Domain Organization

Backend follows domain-driven layout. Each domain has its own: `api/` (route), `models/` (ORM), `schemas/` (Pydantic), `services/` (logic).
Domains: `auth`, `workspaces`, `repositories`, `worktrees`, `agents`, `terminal`, `editor`, `preview`, `commands`, `notifications`, `files`, `dev_server`, `workspace_services`, `specs`, `projects`, `teams`, `dashboard`, `platform_settings`.

Some domains live in the service layer without their own top-level router — `repositories`, `worktrees`, and the GitHub PR client (`github_client.py`) are exposed through `workspaces` routes under `/{workspace_id}/repo/*` (e.g. `/repo/pull`, `/repo/worktrees`, `/repo/pull-requests`).

### Daytona Sandbox Integration

The backend manages ephemeral sandboxes via the Daytona API (`backend/app/services/daytona_runtime.py`). The `agent_tools.py` service maps agent tool calls to sandbox operations (write file, run command, etc.).

**Custom snapshot required**: For the preview (dev-server) and editor (code-server) features, a custom Daytona snapshot with `mise` and `code-server` pre-installed is needed. Build it via:
```bash
./daytona/build-snapshot.sh
```
Then set `DAYTONA_DEFAULT_SNAPSHOT=enigma-mise-code` in `.env`. The stock `daytonaio/sandbox` image will fail because runtime `curl | sh` installs are blocked by the sandbox network.

### Global LLM Configuration

One platform-wide row in `platform_llm_settings` (`models/platform_llm.py`,
`services/platform_llm.py`, `api/platform_settings.py`, prefix `/platform/llm-settings`) holds the
OpenAI-compatible base URL, model, and a Fernet-encrypted API key. Owner-only, gated by
`require_role("owner")` — this is that helper's first production caller. Frontend: `/settings`
(`routes/SettingsRoute.tsx`) plus the first-run `/setup` gate (`platform/llmStatus.ts`,
`SetupGate` in `App.tsx`).

**Precedence is DB row > env (`OPENHANDS_LLM_*`) > refuse.** The single seam is
`provider_for_run(session, settings)` in `services/agents.py`; `provider_from_settings` stays
**sync and env-only** because it is unit-tested directly and is the fallback branch.

### Spec-Driven Development (GitHub Spec Kit)

`backend/app/services/speckit.py` (`SpecKitService`) drives the Spec Kit pipeline on top of the agent infrastructure. Models: `SpecWorkflow` + `SpecPhase` in `models/speckit.py`. API: `api/specs.py` (prefix `/workspaces/{workspace_id}/spec`). Frontend: `SpecPanel/`.

- **Phases (in order)**: `constitution → specify → clarify → plan → analyze → tasks → implement`. Each phase is one agent run.
- **Human-in-the-loop**: each phase halts at `awaiting_review` until a human approves. `implement` is **hard-blocked** until both `plan` and `tasks` are approved.
- Each feature runs in its own **git worktree**. Scaffolding and git (`specify init`, `git worktree`, commits) run directly through `DaytonaRuntime.exec`, NOT the agent tool sandbox (which blocks `git`/`uv`/scripts). Artifact *content* (files under `specs/` and `.specify/`) is written by the agent.

### Git Worktrees & Pull Requests

- **Worktrees**: `services/worktrees.py` (`WorktreeService`) + `models/worktree.py` (`WorkspaceWorktree`). Each non-base worktree is an isolated checkout/branch. Terminals get a per-worktree PTY (pass `worktree_id` query param to the terminal socket); agent runs target the active worktree's branch.
- **Git providers**: `services/git_providers.py` is the pure provider abstraction (GitHub, GitLab, Bitbucket, generic; URL validation/canonicalisation, basic-auth usernames, redaction). Credentials are token **or** server-generated SSH deploy key (`services/ssh_keys.py`), one row per workspace. `RepositoryService` runs all clone/pull/fetch/push through the git CLI with per-command auth.
- **Templates / blank workspaces**: `workspace_templates` (seeded by migration `0022`), `services/templates.py`, `services/workspace_init.py` (clone template → fresh `git init` → initial commit). `workspaces.repo_url` is nullable; `source` ∈ `repository|template|blank`.
- **Pull requests**: `services/github_client.py` exposes `GitHubClient` (raw GitHub REST) + `PullRequestService` (create/get/list, cached in the `PullRequest` model). GitHub + token credentials only — other providers raise `PullRequestsUnsupportedError` (HTTP 409).
- **Endpoints**: under `workspaces` — `POST /repo/connect`, `GET /repo/credential`, `POST /repo/ssh-key`, `POST /repo/pull`, `GET|POST /repo/worktrees`, `GET /repo/pull-requests`; catalog at `GET /workspace-templates`.

## CI/CD

**Two workflows**: `.github/workflows/ci.yml` (backend: postgres + redis services → `alembic upgrade head` → `ruff` → `pytest`; frontend: lint + test + build) and `.github/workflows/ai-code-review.yml` — runs `scripts/ai_code_review.py` on non-draft PRs. Posts an AI review comment. Uses LangChain + LangGraph + OpenAI. Requires `OPENAI_API_KEY` secret.

**No pre-commit hooks, no lint-staged, no Makefile.**

## Important Gotchas

1. **`podman` is the default** container runtime, not Docker. Use `podman compose`, not `docker compose`. The `docker` variant exists as `npm run dev:docker`.
2. **Backend is async-only** — all DB queries and service calls must be `await`-ed. `create_async_engine` + `async_session_maker`.
3. **CORS origins accept comma-separated strings** via pydantic field validator. Add new origins to `.env` as `BACKEND_CORS_ORIGINS=http://a,b,c`.
4. **Database URL uses `asyncpg`**: `postgresql+asyncpg://user:pass@host:port/db`. Not `postgresql://`.
5. **uv run, not pip** — backend deps are managed by `uv`. Never run `pip install` in the backend directory.
6. **Frontend `CI=true`** is set in `docker-compose.yml` for the frontend service — this may disable some dev-only features.
7. **`.env` is loaded by pydantic-settings from CWD** — the backend container runs from `/app`, so `.env` must be mounted or baked into the image.
8. **Docker Compose mounts source as volumes** (`./backend:/app`, `./frontend:/app`) for hot reload. The backend command also runs `git config safe.directory '*'` before starting.
9. **`pnpm-workspace.yaml` exists in both root AND frontend/** — the frontend has its own workspace config. Modify the correct one.
10. **No runtime type checking in Python** — mypy is not configured. Type hints are documentation only.
11. **Git/uv/scripts run via `DaytonaRuntime.exec`, not the agent tool sandbox** — the agent tool sandbox blocks them. Spec Kit and worktree operations shell out through `DaytonaRuntime.exec` directly; only file *content* goes through agent tools.
12. **Spec Kit `implement` is hard-gated** — it cannot run until the `plan` and `tasks` phases are approved. Don't bypass the `awaiting_review` halts.
13. **A stored LLM config always drives the OpenHands provider** — it is the only provider that accepts a `base_url`; `OpenAIResponsesProvider` hardcodes `api.openai.com/v1/responses`. With nothing configured, `create_run` raises `AgentConfigurationError` → **503**; tests that drive the run pipeline without an LLM must pass `Settings(agent_allow_mock_provider=True)`.
14. **`Settings()` is order-dependent in tests** — importing `openhands.sdk` calls `load_dotenv()`, which injects the repo-root `.env` into `os.environ`. A test that means "nothing is configured" must pin the fields explicitly (see `bare_settings()` in `tests/test_platform_llm_settings.py`), never rely on a bare `Settings()`. This is also why `test_settings.py::test_command_deny_dangerous_defaults_to_true` fails locally in a full-suite run but passes alone.
15. **Worktree-aware operations** — agent runs and terminals target the active worktree's branch. The frontend tracks it in `localStorage` (`activeWorktree.ts`); the backend resolves it server-side from `worktree_id`. PR creation needs a workspace GitHub token.
16. **Process-local run state goes through `services/runtime.py`** — run tasks/stop events/providers (`RunRegistry`), stream fan-out (`EventBus`, topics `agent_run:` / `command_run:` / `notifications:`) and the one-active-run-per-workspace guard (`RunLock`). Never add a module-level dict of in-flight work; `tests/test_run_runtime.py` greps for it.
17. **Startup marks every active run `error`/`interrupted_by_restart`** (`services/reconciliation.py`, called from the lifespan; the post-drain shutdown sweep is scoped to the runs this process owned). The startup sweep assumes **one backend replica**: a second live replica would have its runs killed. Spec Kit finalizers are registered in `run_registry` (keyed by phase id) so the drain covers them. `--reload` restarts count too. New work is refused with 503 while `lifecycle.draining` (`core/lifecycle.py`); `/live` is the dependency-free liveness probe, `/ready` the traffic gate, `/health` diagnostics.
18. **Compose requires `AUTH_SECRET_KEY` and `WORKSPACE_ENV_ENCRYPTION_KEY` in `.env`** (no fallbacks). The encryption key must be a valid Fernet key in every environment; changing it makes stored credentials undecryptable.
19. **`RUN_EXECUTION_MODE=inline|queue`** (default `inline`). In `queue` mode, create-run endpoints enqueue the run id (`services/jobs.py`, arq) and `app/worker.py` executes it. The event bus becomes `RedisEventBus`, and stop travels on `agent_run_control:{id}`. The API then skips the startup sweep, and each worker reconciles only its own `worker_id`. Workers claim runs with a conditional UPDATE on `claimed_at`, so a redelivered job never re-runs a run. A duplicate active run is blocked by the partial unique index `uq_agent_runs_one_active_per_workspace`, so a test fixture with two `thinking`/`executing` runs in one workspace fails. `redis` is pinned `<6` because arq requires it. Details: `docs/horizontal-scale/02-distributed-job-execution.md`.
20. **Streams are backfill-then-tail; Postgres is authoritative, the bus is an at-most-once hint.** A stream subscribes, waits for `wait_ready`, *then* reads persisted state, and re-reads on idle ticks and at the end. Agent frames de-dupe by event id (`catch_up`, high-water `created_at`); command `output` frames carry an `offset` into `run.output` (`tail_command_run`); notifications backfill from `GET /notifications` on reconnect. Persist before you publish, and never publish anything a reader could not re-derive from the DB. Details: `docs/horizontal-scale/03-distributed-event-streaming.md`.

## Implementation Plan Reference

Detailed implementation plans live in `docs/implementation-plan/` (14 docs covering project foundation through production hardening). See `00-index.md` for build order and MVP boundary.

Post-MVP improvement epics are tracked in `docs/improvement-epics/` (e.g. spec-driven development, VCS/PR & branch workflow, agent editing tools, an Anthropic Claude provider, multi-turn sessions, cost/usage analytics). See `00-index.md` for the epic list. Note: the Anthropic provider is *planned* — current agent providers are `openai` (`OpenAIResponsesProvider`), `openhands` (`openhands_provider.py`), and `mock`.

## Design System (Frontend)

The frontend follows a Cursor-inspired design language defined in `frontend/design.md`:
- Warm cream canvas (`#f7f7f4`), not white
- Single brand color: `primary` `#f54e00` (used sparingly)
- Hairline-only depth (1px borders, no drop shadows)
- Timeline pill palette: 5 pastel colors for AI agent action stages
- Display weight stays at 400 (magazine voice)
- 8px CTA border radius
