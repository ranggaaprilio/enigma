# Enigma

AI agent workspace platform built with FastAPI, React, PostgreSQL, and Redis.

Organises work in a three-tier hierarchy: **Projects → Teams → Workspaces**.
Each workspace is a persistent Daytona sandbox with a full dev environment —
git, files, terminal, AI agent runner, `code-server` editor, and preview server.

## Quick Start

### Prerequisites

- [Podman](https://podman.io/) (or Docker) with Compose support
- A [Daytona](https://daytona.io/) account and API key

### Steps

1. Copy the example environment file and fill in your values:
   ```bash
   cp .env.example .env
   ```
   At minimum set `DAYTONA_API_KEY`. See [Environment](#environment) for the full list.

2. Start all services:
   ```bash
   podman compose up --build -d
   # or: docker compose up --build -d
   ```

3. Run database migrations (required on first start and after any update):
   ```bash
   podman compose exec backend uv run alembic upgrade head
   # or inside the backend directory without containers:
   # cd backend && uv run alembic upgrade head
   ```

4. Open the app:
   - Frontend: http://localhost:5173
   - Backend API: http://localhost:8000
   - Health check: http://localhost:8000/health

5. Register an account. The first user automatically becomes the platform owner.

### First-time workflow

1. Log in and create a **Project**.
2. Inside the project, create one or more **Teams**.
3. Inside a team, create a **Workspace** — this provisions a Daytona sandbox.
4. Use the workspace dashboard to run the AI agent, browse files, open a terminal, start the editor, and preview your app.

## Services

| Service | Port | Notes |
|---|---|---|
| Frontend (Vite + React) | 5173 | Hot-reload in dev mode |
| Backend (FastAPI) | 8000 | Auto-reload with `--reload` |
| Preview proxy | 8001 | Proxies Daytona preview URLs |
| PostgreSQL 16 | 5432 | Persisted in `postgres_data` volume |
| Redis 7 | 6379 | Persisted in `redis_data` volume |

## Development

Run backend tests:
```bash
cd backend
uv run pytest
```

Run frontend checks:
```bash
cd frontend
pnpm install
pnpm lint
pnpm test
```

## Environment

Copy `.env.example` to `.env`. Key variables:

| Variable | Required | Description |
|---|---|---|
| `DAYTONA_API_KEY` | Yes | Daytona API key for sandbox provisioning |
| `DAYTONA_API_URL` | No | Override for self-hosted Daytona (default: `https://app.daytona.io/api`) |
| `DAYTONA_TARGET` | No | Daytona target region |
| `DAYTONA_DEFAULT_SNAPSHOT` | No | Default sandbox snapshot ID |
| `AUTH_SECRET_KEY` | Yes (prod) | Secret for signing session tokens — change from default in production |
| `WORKSPACE_ENV_ENCRYPTION_KEY` | Yes (prod) | Fernet key for encrypting workspace env vars |
| `OPENAI_API_KEY` | No | Required if `AGENT_PROVIDER=openai` |
| `OPENHANDS_LLM_API_KEY` | No | Required if `AGENT_PROVIDER=openhands` |

Full list of variables: see `.env.example` and `backend/app/core/config.py`.

## Sandbox requirements

The Daytona snapshot used by each workspace must provide: a Linux shell, Git,
Python 3, `code-server`, and standard networking utilities for port detection and
process management.
