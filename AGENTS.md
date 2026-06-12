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
- **Router**: `backend/app/api/router.py` aggregates 12 sub-routers
- **Config**: `backend/app/core/config.py` via `pydantic-settings` (reads `.env` from CWD, `extra="ignore"`)
- **Database**: PostgreSQL via SQLAlchemy 2.0 async + `asyncpg` driver (NOT psycopg2)
  - Connection string must be `postgresql+asyncpg://…`
  - Session: `async_session_maker` with `expire_on_commit=False`
  - All DB access MUST use async/await
- **Migrations**: Alembic with async engine, 11 migrations in `alembic/versions/`
- **Auth**: Cookie-based sessions (`execute_session` cookie), NOT JWT. Set-Cookie on login, cleared on logout.
- **Logging**: `structlog` (configured in `core/logging.py`)
- **Testing**: `pytest` with `pytest-asyncio` (auto mode), tests at `backend/tests/`

### Frontend (React 18 + Vite + Tailwind 3)
- **No react-router** — custom `navigate()` in `src/lib/router.ts` using `window.history.pushState` + `PopStateEvent`
- **No component library** — all UI built with Tailwind utility classes. Do NOT assume shadcn/ui, Radix, or any component kit.
- **No state library** — React Context only. Main hook: `useAuth()` from `src/auth/context.ts`
- **API client**: `src/lib/api.ts` (875 lines). All HTTP calls go through exported typed functions — never `fetch()` directly.
  - Auth uses `credentials: "include"` for cookie-based sessions
  - WebSocket helpers: `getAgentStreamUrl()`, `getTerminalSessionSocketUrl()`, `getCommandRunStreamUrl()`, `getNotificationStreamUrl()`
- **Styling**: Tailwind CSS 3 with a custom Cursor-inspired palette (`canvas`, `panel`, `ink`, `primary` #f54e00, etc.). Fonts: Inter (UI), JetBrains Mono (code/terminal).
- **Terminal**: `@xterm/xterm` + `@xterm/addon-fit` in `TerminalPanel.tsx`
- **Icons**: `lucide-react` (only)
- **Testing**: Vitest + jsdom + `@testing-library/react`. Setup file: `src/test/setup.ts`

### Domain Organization

Backend follows domain-driven layout. Each domain has its own: `api/` (route), `models/` (ORM), `schemas/` (Pydantic), `services/` (logic).
Domains: `auth`, `workspaces`, `repositories`, `agents`, `terminal`, `editor`, `preview`, `commands`, `notifications`, `files`, `dev_server`.

### Daytona Sandbox Integration

The backend manages ephemeral sandboxes via the Daytona API (`backend/app/services/daytona_runtime.py`). The `agent_tools.py` service maps agent tool calls to sandbox operations (write file, run command, etc.).

**Custom snapshot required**: For the preview (dev-server) and editor (code-server) features, a custom Daytona snapshot with `mise` and `code-server` pre-installed is needed. Build it via:
```bash
./daytona/build-snapshot.sh
```
Then set `DAYTONA_DEFAULT_SNAPSHOT=enigma-mise-code` in `.env`. The stock `daytonaio/sandbox` image will fail because runtime `curl | sh` installs are blocked by the sandbox network.

## CI/CD

**Single workflow**: `.github/workflows/ai-code-review.yml` — runs `scripts/ai_code_review.py` on non-draft PRs. Posts an AI review comment. Uses LangChain + LangGraph + OpenAI. Requires `OPENAI_API_KEY` secret.

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

## Implementation Plan Reference

Detailed implementation plans live in `docs/implementation-plan/` (14 docs covering project foundation through production hardening). See `00-index.md` for build order and MVP boundary.

## Design System (Frontend)

The frontend follows a Cursor-inspired design language defined in `frontend/design.md`:
- Warm cream canvas (`#f7f7f4`), not white
- Single brand color: `primary` `#f54e00` (used sparingly)
- Hairline-only depth (1px borders, no drop shadows)
- Timeline pill palette: 5 pastel colors for AI agent action stages
- Display weight stays at 400 (magazine voice)
- 8px CTA border radius
