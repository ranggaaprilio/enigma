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
# Self-host sandboxes (the default runtime): OpenSandbox next to the stack
podman compose -f docker-compose.yml -f docker-compose.opensandbox.yml up --build   # npm run dev:opensandbox
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
./scripts/backup_db.sh           # pg_dump -Fc -> backups/<db>-<timestamp>.dump
./scripts/restore_db.sh <dump> --drop-after  # test-restore into a scratch DB, verify, drop it
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
- **Migrations**: Alembic with async engine, 44 migrations in `alembic/versions/` (`NNNN_snake_case.py`, `revision` = filename stem; head `0044_verification_artifacts`)
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

The backend manages ephemeral sandboxes through the provider-neutral `SandboxRuntime` interface in `backend/app/services/sandbox/`: `base.py` (protocols, DTOs, errors), `daytona.py` (the Daytona provider and the **only** module that imports the `daytona` SDK), `factory.py` (`get_sandbox_runtime()` returns a dispatcher that routes each call by `Workspace.runtime`; `close_all()` runs in every lifespan). `tests/sandbox/test_boundary.py` fails the build if an SDK import or a raw sandbox object leaks outside the package; `tests/sandbox/test_conformance.py` is the provider contract. The `agent_tools.py` service maps agent tool calls to sandbox operations (write file, run command, etc.).

**Custom snapshot required**: For the preview (dev-server) and editor (code-server) features, a custom Daytona snapshot with `mise` and `code-server` pre-installed is needed. Build it via:
```bash
./daytona/build-snapshot.sh
```
Then set `DAYTONA_DEFAULT_SNAPSHOT=enigma-mise-code` in `.env`. The stock `daytonaio/sandbox` image will fail because runtime `curl | sh` installs are blocked by the sandbox network. The image definition is `sandbox/Dockerfile`, shared with OpenSandbox (`enigma-sandbox:latest`).

**Docker provider: removed** (Plan 8, `docs/superpowers/plans/2026-11-28-enigma-remove-docker-provider.md`; last release with it: `v0.1.0`). OpenSandbox (gotcha 32) replaced it for self-host; Daytona stays the cloud provider. `SANDBOX_RUNTIME_DEFAULT=docker` (or docker in `SANDBOX_RUNTIMES_ALLOWED`) fails startup with a message naming `v0.1.0`; leftover `DOCKER_*` variables are ignored and logged once as `removed_setting_name` (`core/config.py` `REMOVED_SETTING_NAMES`). A leftover `runtime='docker'` row 503s on every call and cannot be deleted through the API; the plan's design §1 and `docs/self-host-opensandbox.md` ("Moving from Docker") give the SQL to remove it. The `enigma-sandbox` image (`sandbox/Dockerfile`, `./sandbox/build-local-image.sh`) stays.

**Provider selection and shared sandbox rules:**
- **Choosing a runtime (Epic 06):** `POST /workspaces` (and the team route) take an optional `runtime`; `services/sandbox/availability.py` `choose_runtime` checks it **before** the row is written. An explicit choice is honoured or refused (422, generic reason, never a socket path/URL); no choice takes `SANDBOX_RUNTIME_DEFAULT` (`daytona` | `opensandbox`), or the other provider when only it is up. `SANDBOX_RUNTIMES_ALLOWED` (empty = all; must include the default) limits what is offered; a left-out provider is `disabled` on `/health` (not a fault) and refused by `choose_runtime`.
- **`/health`** carries a `runtimes` block (daytona and opensandbox, each with `configured`, `status` ok/error/unconfigured/disabled/insecure, `reason`) plus `sandbox_runtime_default`. An unconfigured provider is not a fault, a configured one that is down is; the top-level `daytona` field stays for monitors.
- **Settings:** neutral names are canonical (`SANDBOX_HOME`, `SANDBOX_WORKSPACE_PATH`, `PREVIEW_PROXY_*`, `SANDBOX_NETWORK_*`, `SANDBOX_AUTO_STOP_MINUTES`, `SANDBOX_UPKEEP_INTERVAL_SECONDS`); the `DAYTONA_*` aliases and `DOCKER_REAPER_INTERVAL_SECONDS` work for one release and are logged as `deprecated_setting_name` (`DEPRECATED_SETTING_NAMES`). The base compose forwards `SANDBOX_NETWORK_*` (with the `DAYTONA_*` fallback) to backend and worker — never drop it.
- **`sandbox_id`:** `0037` added `workspaces.sandbox_id`, `0038` moved reads to it, `0039` (`# contract:`) dropped `daytona_sandbox_id` and made `ix_workspaces_sandbox_id` unique (deploy `0039` only after a release with `0038` is live everywhere). Use `Workspace.sandbox_id` (tests too: `Workspace(sandbox_id=...)`).
- **Exec hygiene:** inside a sandbox never exec a bare command name from the backend: `~/.local/bin` leads PATH and is the tenant's (use `/bin/cat`, shell builtins). Never guard a launch with `pgrep -f '<pattern>'`: the exec shell's argv holds the script, so the pattern matches itself; check the listen table instead (see `editor.py`).

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
- Each feature runs in its own **git worktree**. Scaffolding and git (`specify init`, `git worktree`, commits) run directly through `SandboxRuntime.exec`, NOT the agent tool sandbox (which blocks `git`/`uv`/scripts). Artifact *content* (files under `specs/` and `.specify/`) is written by the agent.

### Git Worktrees & Pull Requests

- **Worktrees**: `services/worktrees.py` (`WorktreeService`) + `models/worktree.py` (`WorkspaceWorktree`). Each non-base worktree is an isolated checkout/branch. Terminals get a per-worktree PTY (pass `worktree_id` query param to the terminal socket); agent runs target the active worktree's branch.
- **Git providers**: `services/git_providers.py` is the pure provider abstraction (GitHub, GitLab, Bitbucket, generic; URL validation/canonicalisation, basic-auth usernames, redaction). Credentials are token **or** server-generated SSH deploy key (`services/ssh_keys.py`), one row per workspace. `RepositoryService` runs all clone/pull/fetch/push through the git CLI with per-command auth.
- **Templates / blank workspaces**: `workspace_templates` (seeded by migration `0022`), `services/templates.py`, `services/workspace_init.py` (clone template → fresh `git init` → initial commit). `workspaces.repo_url` is nullable; `source` ∈ `repository|template|blank`.
- **Pull requests**: `services/github_client.py` exposes `GitHubClient` (raw GitHub REST) + `PullRequestService` (create/get/list, cached in the `PullRequest` model). GitHub + token credentials only — other providers raise `PullRequestsUnsupportedError` (HTTP 409).
- **Endpoints**: under `workspaces` — `POST /repo/connect`, `GET /repo/credential`, `POST /repo/ssh-key`, `POST /repo/pull`, `GET|POST /repo/worktrees`, `GET /repo/pull-requests`; catalog at `GET /workspace-templates`.

## CI/CD

**Two workflows**: `.github/workflows/ci.yml` (backend: postgres + redis services → `alembic upgrade head` → `ruff` → `pytest`; frontend: lint + test + build; deploy: `kubectl kustomize` the production overlay → kubeconform → build both images → Trivy fails on fixable HIGH/CRITICAL) and `.github/workflows/ai-code-review.yml` — **currently disabled** (PR trigger commented out); when enabled it runs `scripts/ai_code_review.py` on non-draft PRs. Posts an AI review comment. Uses LangChain + LangGraph + OpenAI. Requires `OPENAI_API_KEY` secret.

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
11. **Git/uv/scripts run via `SandboxRuntime.exec`, not the agent tool sandbox** — the agent tool sandbox blocks them. Spec Kit and worktree operations shell out through `SandboxRuntime.exec` directly; only file *content* goes through agent tools.
12. **Spec Kit `implement` is hard-gated** — it cannot run until the `plan` and `tasks` phases are approved. Don't bypass the `awaiting_review` halts.
13. **A stored LLM config always drives the OpenHands provider** — it is the only provider that accepts a `base_url`; `OpenAIResponsesProvider` hardcodes `api.openai.com/v1/responses`. With nothing configured, `create_run` raises `AgentConfigurationError` → **503**; tests that drive the run pipeline without an LLM must pass `Settings(agent_allow_mock_provider=True)`.
14. **`Settings()` is order-dependent in tests** — importing `openhands.sdk` calls `load_dotenv()`, which injects the repo-root `.env` into `os.environ`. A test that means "nothing is configured" must pin the fields explicitly (see `bare_settings()` in `tests/test_platform_llm_settings.py`), never rely on a bare `Settings()`. This is also why `test_settings.py::test_command_deny_dangerous_defaults_to_true` fails locally in a full-suite run but passes alone.
15. **Worktree-aware operations** — agent runs and terminals target the active worktree's branch. The frontend tracks it in `localStorage` (`activeWorktree.ts`); the backend resolves it server-side from `worktree_id`. PR creation needs a workspace GitHub token.
16. **Process-local run state goes through `services/runtime.py`** — run tasks/stop events/providers (`RunRegistry`), stream fan-out (`EventBus`, topics `agent_run:` / `command_run:` / `notifications:`) and the one-active-run-per-workspace guard (`RunLock`). Never add a module-level dict of in-flight work; `tests/test_run_runtime.py` greps for it.
17. **Startup marks every active run `error`/`interrupted_by_restart`** (`services/reconciliation.py`, called from the lifespan; the post-drain shutdown sweep is scoped to the runs this process owned). The startup sweep assumes **one backend replica**: a second live replica would have its runs killed. Spec Kit finalizers are registered in `run_registry` (keyed by phase id) so the drain covers them. `--reload` restarts count too. New work is refused with 503 while `lifecycle.draining` (`core/lifecycle.py`); `/live` is the dependency-free liveness probe, `/ready` the traffic gate, `/health` diagnostics.
18. **Compose requires `AUTH_SECRET_KEY` and `WORKSPACE_ENV_ENCRYPTION_KEY` in `.env`** (no fallbacks). The encryption key must be a valid Fernet key in every environment; changing it makes stored credentials undecryptable.
19. **`RUN_EXECUTION_MODE=inline|queue`** (default `inline`). In `queue` mode, create-run endpoints enqueue the run id (`services/jobs.py`, arq) and `app/worker.py` executes it. The event bus becomes `RedisEventBus`, and stop travels on `agent_run_control:{id}`. The API then skips the startup sweep, and each worker reconciles only its own `worker_id`. Workers claim runs with a conditional UPDATE on `claimed_at`, so a redelivered job never re-runs a run. A duplicate active run is blocked by the partial unique index `uq_agent_runs_one_active_per_workspace`, so a test fixture with two `thinking`/`executing` runs in one workspace fails. `redis` is pinned `<6` because arq requires it. Details: `docs/horizontal-scale/02-distributed-job-execution.md`.
20. **Streams are backfill-then-tail; Postgres is authoritative, the bus is an at-most-once hint.** A stream subscribes, waits for `wait_ready`, *then* reads persisted state, and re-reads on idle ticks and at the end. Agent frames de-dupe by event id (`catch_up`, high-water `created_at`); command `output` frames carry an `offset` into `run.output` (`tail_command_run`); notifications backfill from `GET /notifications` on reconnect. Persist before you publish, and never publish anything a reader could not re-derive from the DB. Details: `docs/horizontal-scale/03-distributed-event-streaming.md`.
21. **Runs are leased; status writes are conditional** (Epic 04). A worker's claim stamps `lease_expires_at` (Postgres `clock_timestamp()`, never the worker's clock); `heartbeat()` (`services/leases.py`) renews it every `RUN_HEARTBEAT_SECONDS` while the run executes, and only the owning `worker_id` can renew. Every worker runs the reaper (`services/reaper.py`), which ends active runs with an expired lease as `error`/`interrupted_worker_died`; inline runs have no lease and are never reaped. The same pass ends enqueued-but-unclaimed runs as `job_lost`, but only when arq reports the job `not_found` (older than `RUN_UNCLAIMED_GRACE_SECONDS`, never on a Redis error), so a run waiting behind a busy pool is never touched. Agent status changes go through `AgentService._transition` (`UPDATE … WHERE status IN active`): terminal is final, `stop_run` is idempotent, and a write that loses the race returns `False` and must not publish. `create_run` also takes `pg_advisory_xact_lock` on `agent_run:workspace:{id}`. Details: `docs/horizontal-scale/04-run-lifecycle-and-coordination.md`.
22. **Pools are sized; retention is opt-in; WebSockets never hold a session** (Epic 05). `engine_options()` (`db/session.py`) builds the pool from `DB_POOL_*`; with `DB_PGBOUNCER=true` it is `NullPool` with asyncpg statement caching off. Use only transaction-scoped Postgres state (`pg_advisory_xact_lock`, never session advisory locks, `SET`, or `LISTEN`), or it breaks behind PgBouncer. `services/retention.py` prunes old events, command output and notifications when a `RETENTION_*_DAYS` is set (workers in queue mode, the API in inline mode), in `FOR UPDATE SKIP LOCKED` batches; pruned runs keep their row and token totals and get `events_pruned_at` / `output_pruned_at`. `tests/test_data_layer.py` fails any WebSocket handler that calls `accept()` inside `async with async_session_maker()`. Redis clients get read timeouts, except the client a long-lived `pubsub.listen()` runs on (`get_redis_client(subscriber=True)`), which a read timeout would kill when idle; keep commands off that client (see `PresenceService`). Retention never deletes `budget_warning` notifications (budget de-dupe). Retention tests seed rows ~25 years old because the suite shares the dev database. Details: `docs/horizontal-scale/05-data-layer-scaling.md`.
23. **Fair use: rate limits fail closed; caps are Postgres counts** (Epic 06). `RedisRateLimiter` 429s carry `Retry-After` (the window's TTL) and a Redis error is a 503, never a pass; the login limiter is the exception (still a bare 500). Spec run triggers and sandbox actions use `api/rate_limits.py` deps; route tests bypass them with `app.dependency_overrides[enforce_sandbox_rate_limit]`. `services/capacity.py` caps active (`thinking`/`executing`) agent runs per team (`RUN_CAP_PER_TEAM`, via `workspaces.team_id`) and platform-wide (`RUN_CAP_GLOBAL`) inside `create_run`, under `pg_advisory_xact_lock` taken in the order workspace → team → global (keep that order). `RUN_QUEUE_MAX_DEPTH` (queue mode) counts enqueued-but-unclaimed agent + command runs. `RunCapacityExceededError` is handled globally in `main.py` (429 for a cap, 503 for queue backpressure, `Retry-After` always), so per-route error mappers need not know it. Command runs are not capped, only backpressured. There is no `queued` run status. `ACTIVE_RUN_STATUSES` now lives in `services/capacity.py` (re-exported by `services/agents.py`). Details: `docs/horizontal-scale/06-rate-limits-quotas-backpressure.md`.
24. **The preview proxy is stateless and host-gated** (Epic 07). `app/preview_proxy.py` may not write module-level state (an AST guard in `tests/test_preview_proxy_scaling.py` fails the build); every check is HMAC over `AUTH_SECRET_KEY` + Postgres, so replicas need no stickiness but must share that secret. Its `/live`, `/ready`, `/health` and `/metrics` answer **only on a non-preview Host** (bare proxy host or pod IP); on a preview host every path is proxied. `/health` and `/metrics` need the `METRICS_TOKEN` bearer. Each proxy app owns its own `AppLifecycle` (`app.state.lifecycle`) — never use the API's `lifecycle` singleton there, or a proxy test's shutdown drains the API for the rest of the process. A page load that would 503 gets the self-retrying "Waking preview…" HTML page; XHR keeps the JSON 503. The client's `X-Forwarded-Host` is stripped. Details: `docs/horizontal-scale/07-stateless-preview-proxy.md`.
25. **Production is Kubernetes via Kustomize; compose stays dev** (Epic 08). Both Dockerfiles are multi-stage: compose builds `target: dev`; the default last stage is the production image (backend: slim, uid 10001, read-only root FS, uvicorn as PID 1 — keep the exec-form CMD, no `uv run` wrapper; frontend: nginx-unprivileged static build). Manifests live in `deploy/k8s/{base,overlays/production}`, runbook in `deploy/README.md`; `tests/test_deployment.py` guards probes, rollout strategy, grace periods vs `SHUTDOWN_*`, secret hygiene, network policy and the KEDA worker query (which must count the same rows as `services/capacity.py`). Workers drain for up to `SHUTDOWN_RUN_GRACE_SECONDS=7500` so a rollout never interrupts a run. **Migrations must be expand-only**: `tests/test_migration_safety.py` fails any migration after `0034` that drops/renames/retypes/tightens in `upgrade()` unless it carries `# contract: <why>` (ship contract steps a release later). Details: `docs/horizontal-scale/08-deployment-and-autoscaling.md`.
26. **One request id tells a run's whole story; metrics labels are bounded; tracing is opt-in** (Epic 09). `create_run` stamps `request_id` and `traceparent` on the run row from the current context (`current_request_id()` / `current_traceparent()`), never from a job argument: the worker reads them off the row after claiming and binds `request_id` (and `trace_id`) into its log context for the job. Stream frames carry `request_id` only when the run has one. Wrap every new WebSocket handler in `websocket_context(kind)` (inside `router.websocket`). New metrics go in `core/metrics.py` with **bounded labels** (kind/status/reason/provider/model, never a workspace, run or user id); per-team series exist only on the API-side fleet sampler (`services/fleet_metrics.py`, top-`METRICS_TENANT_LABEL_LIMIT` teams plus `other`/`none`, rebuilt every pass, same on every replica so dashboards use `max()`, not `sum()`). Worker-side series are scraped from the worker's own `/metrics` on `METRICS_PORT` (`app/worker_metrics.py`); `tests/test_deployment.py` requires that to be the worker's only ingress. Queue depth has one definition, `capacity.waiting_run_counts()`. Tracing (`core/tracing.py`) does nothing until `OTEL_EXPORTER_OTLP_ENDPOINT` is set; keep `configure_tracing` the first thing `create_app`/the worker startup does. Details: `docs/horizontal-scale/09-observability-and-load-testing.md`.
27. **Verification runs are a resumable state machine** (Verifiable Agent Loop, Plan 2: `docs/superpowers/plans/2026-11-26-enigma-verification-close-the-loop.md`). `POST /workspaces/{id}/verification/runs` persists the run at `planning` with its `criteria` and `created_by` and returns **202**; `services/verification/execution.py` then drives it in a `run_registry` task (inline) or enqueues `run_verification_job` (queue). `VerificationOrchestrator.advance` does exactly one step from the row (`planning → testing → [fixing → testing]* → observing → review`); every transition is a conditional UPDATE, so replays are harmless. In `fixing`, `AgentFixer` starts **one** agent run through `AgentService.create_run` (budget, caps and guards apply; refusals become `fix` evidence and the run goes to the gate, never stays active). **Never await an agent run inside the queue job** — it defers with `Retry(defer=VERIFICATION_POLL_SECONDS)`, or a small `max_jobs` deadlocks. Inline-mode startup sweeps active verification runs to `error` (`reconciliation.py`); a worker sweep leaves them to their job. Test evidence keeps the last 8 KiB of output (`OUTPUT_TAIL_CHARS`).
28. **Spec Kit `implement` is verified before a workflow completes** (Plan 3: `docs/superpowers/plans/2026-11-27-enigma-verification-spec-kit.md`). When implement's agent run completes, `_finalize_phase_inner` resolves criteria with `services/verification/spec_link.py` `criteria_for_workflow`: the workflow's own `verification` (set at creation), else the workspace's first enabled `role="test"` custom command (cwd = worktree + `app_root` + command cwd), else none, in which case implement completes as before. With criteria, a run linked by `verification_runs.spec_workflow_id` (`0042`) starts in the feature worktree, and implement plus the workflow park at `awaiting_review`. **The evidence is the gate:** `approve_verification_run` approves the run and, only while implement is `awaiting_review`, the phase plus `completed` workflow in the same transaction; `approve_phase("implement")` is refused (409) when criteria exist. Linked runs still in `review` are superseded (→ `rejected`) when implement re-runs (`run_phase`, so *Request changes* too) and when a new linked run starts (`POST /spec/workflows/{id}/verify`), so stale evidence can never complete a workflow. `request_changes` locks the phase row, the same lock the approval takes.
29. **A completed Spec Kit workflow is handed over as a PR by an explicit action** (Plan 4: `docs/superpowers/plans/2026-11-27-enigma-verification-pull-request.md`). `POST /workspaces/{id}/spec/workflows/{wid}/pull-request` → `SpecKitService.open_pull_request`: refuses anything not `completed` (409), checks the workspace can open PRs (GitHub + token) **before** pushing, commits the feature worktree's uncommitted changes as `spec(<slug>): verification fixes` (verification fix runs never commit, so the verified code is the working tree), pushes the worktree's branch, opens the PR (`services/spec_pull_request.py` `build_pr_body`: description, artifact links, spec excerpt, evidence summary — **never** test/command output or observation text) and links it via `spec_workflows.pull_request_id` (`0043`). Idempotent: a linked workflow returns its PR (200), and GitHub's "already exists" adopts the open PR for the branch. A first Spec Kit PR also carries the `chore: initialize Spec Kit` commit (the scaffold is committed to the sandbox's base branch, never pushed); the body then gets a "Spec Kit scaffold" section counting the files, from `git diff --name-only origin/<base>...HEAD -- <scaffold paths>` (`spec_pull_request.scaffold_diff_command`).
30. **Verification runs are stoppable, reaped, and never orphan a fix agent** (Plan 5: `docs/superpowers/plans/2026-11-27-enigma-verification-hardening.md`). `VerificationOrchestrator.end(run_id, status, from)` is the one place a run is ended from outside its step loop (stop, `fail`, a crashing step, the reaper). It locks the row, and if the run was `fixing` it stops the fix agent run through `Fixer.stop` → `AgentService.stop_run`. `_step_fix` records `agent_run_id` only while the run is still `fixing`, and otherwise stops the agent it just started. `POST /verification/runs/{id}/stop` ends an active run as `rejected` (spec §6: a human cancel), owner-only for Spec Kit-linked runs, audited as `verification_run_stopped`; inline, a `testing`/`observing` task is cancelled, and a fix exits on its next poll. In queue mode, `reap_lost_verification_jobs` (run by every worker's reaper) ends active runs idle past `RUN_UNCLAIMED_GRACE_SECONDS` whose `verification_run:<id>` job arq no longer holds; a Redis error skips the run, never ends it.
31. **Observation screenshots are rendered inside the tenant's sandbox and stored in S3** (Plan 6: `docs/superpowers/plans/2026-11-27-enigma-verification-screenshots.md`). `services/verification/screenshot.py` runs the image's own `/usr/bin/chromium --headless=new --no-sandbox` (absolute paths, fresh `mktemp -d`, `127.0.0.1:<port>`) through `SandboxRuntime.exec`, driven over the DevTools protocol by `services/verification/cdp_capture.py` (stdlib only, never imported by the backend: its source runs as `/usr/bin/python3 -I -S -c`; it waits for `load`, bounded, plus 2 s of real time, because Chromium's `--screenshot` captures before a client-rendered page hydrates), reads the PNG back with `fs.download_file`, and accepts only a PNG ≤ 2 MB. Never render tenant pages in the backend or worker. `services/artifact_store.py` (`aioboto3`, any S3-compatible store; `ARTIFACT_S3_*`, empty bucket = no store) holds the bytes under `verification/<workspace>/<run>/<evidence>.png`; `verification_artifacts` (`0044`) holds the metadata, one row per evidence. A failed capture or upload never fails the observation (`detail.screenshot_captured=false` + `screenshot_error`). Images are served only by `GET /verification/runs/{run}/evidence/{evidence}/screenshot` (workspace-scoped, `nosniff`, `default-src 'none'`, private cache); the bucket stays private, with no presigned or public URLs. Dev store: `podman compose --profile artifacts` (RustFS 1.0.7; MinIO no longer publishes images).

32. **OpenSandbox is hardened; it replaced Docker** (Plan 7a: `docs/superpowers/plans/2026-11-27-enigma-opensandbox-hardening.md`; operator guide `docs/self-host-opensandbox.md`). On opensandbox-server 1.2.1, sandboxes share one bridge and execd trusts any caller without a token (verified live).
    - **Create:** every create goes through `build_create_kwargs` + `assert_safe_create_kwargs` (`services/sandbox/opensandbox.py`). It sends only the image (`OPENSANDBOX_SANDBOX_IMAGE`, digest required outside local), resources (`OPENSANDBOX_SANDBOX_*`), installation/workspace metadata, `EXECD_ACCESS_TOKEN` and an egress policy that always denies private, CGNAT, link-local and IPv4-mapped ranges. Never send `extensions`, `volumes`, `platform` or `entrypoint`.
    - **Token:** it is HMAC(`OPENSANDBOX_EXECD_TOKEN_KEY`, workspace id), required outside local and never stored. It rides on every SDK connection, PTY call and `RuntimePreviewLink.headers`. The preview proxy refuses dot-segment paths, because the preview URL shares execd's origin. The observation fetch stays on the preview's origin.
    - **Floor:** each sandbox is probed once per process (`FLOOR_PROBE`: CapEff 0, NoNewPrivs 1, non-root). Teardown skips the probe.
    - **Insecure servers:** a server without `OPENSANDBOX_API_KEY`, or one answering unauthenticated, is `insecure` and never offered. `OPENSANDBOX_ALLOW_INSECURE` is for local only.
    - **Upkeep:** `start_opensandbox_maintenance` (`OPENSANDBOX_UPKEEP`) pauses idle sandboxes, with the `sandbox_activity` heartbeat as the busy rule; use resumes them. It also destroys this installation's labelled orphans. A pass runs every `SANDBOX_UPKEEP_INTERVAL_SECONDS` (`services/sandbox_maintenance.py`); `tests/upkeep_fixtures.py` holds the shared upkeep DB fixtures.
    - **Server config:** the reference is `deploy/opensandbox/sandbox.toml`, and `[egress] mode = "dns+nft"` is required.
    - **Default (Plan 7b, `docs/superpowers/plans/2026-11-27-enigma-opensandbox-cutover.md`):** OpenSandbox became the self-host runtime; Plan 8 (`docs/superpowers/plans/2026-11-28-enigma-remove-docker-provider.md`) then removed the Docker provider (last in `v0.1.0`). `docker-compose.opensandbox.yml` runs the server (pinned by digest, the only service with the daemon socket) and sets `SANDBOX_RUNTIME_DEFAULT=opensandbox`. The base compose forwards `OPENSANDBOX_*` for a server elsewhere. Its config is `deploy/opensandbox/sandbox.compose.toml` (Linux: ports on the fixed `10.214.0.1` gateway) or `sandbox.compose-desktop.toml` (Podman machine / Docker Desktop: loopback + `host.docker.internal`, because their port forwarder binds on the desktop side), picked by `OPENSANDBOX_SERVER_CONFIG`. Both must keep every security key of `sandbox.toml` (`test_deployment.py`). k8s: `deploy/k8s/overlays/opensandbox-external` (server on a VM outside the cluster; the overlay must set `namespace`, or its NetworkPolicy lands in `default`).
    - **Suite:** the opt-in live suite `tests/test_opensandbox_integration.py` runs weekly against a host-run server and against the compose server (`docker compose run backend`). Run it locally with a separate compose project (`-p`), so `down -v` never drops the dev database.

33. **Any completed agent run can be verified, and its fix stays on its branch** (Plan 9: `docs/superpowers/plans/2026-11-28-enigma-verify-agent-runs.md`).
    - **Trigger:** `POST /workspaces/{id}/verification/agent-runs/{run_id}` (`services/verification/agent_link.py`). The run must be `completed` and must not itself be a fix run (409). The criteria come from the body, else the workspace's first enabled `role="test"` command run in the run's checkout (`spec_link.test_command_criteria`, shared with Spec Kit), else 422.
    - **Link:** the verification run records `source_agent_run_id` (`0045_verify_agent_runs`). A newer one supersedes the run's earlier ones in `review`. `GET /verification/runs?source_agent_run_id=` filters by it.
    - **Fix agent:** `AgentFixer` passes the source run's `worktree_id` and `session_id` to `create_run`, so the fix lands on that worktree's branch as a new turn of the same session. Never drop them: without them a worktree run would be "fixed" on the base clone.
    - **Auto-verify:** `workspaces.auto_verify_agent_runs` (opt-in) is honoured at the end of `AgentService._record_run_finished` (imported locally there: the verification package imports `agents`). It skips fix runs, Spec Kit phase runs, verified runs, runs without criteria and busy workspaces, and it can never fail the run.
    - **Migration ids:** keep them ≤ 32 characters (`alembic_version` is `varchar(32)`).

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
