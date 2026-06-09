# Execute Dashboard

Server-based AI agent dashboard foundation built with FastAPI, React, PostgreSQL, and Redis.

## Local Setup

1. Install `uv`, `pnpm`, and Podman.
2. Copy `.env.example` to `.env`.
3. Add a Daytona API key and optionally configure a self-hosted `DAYTONA_API_URL`.
4. Start the full stack:
   ```bash
   podman compose up --build
   ```
5. Open the frontend at `http://localhost:5173`.
6. Check the backend health endpoint at `http://localhost:8000/health`.
7. Run backend migrations when needed:
   ```bash
   cd backend
   uv run alembic upgrade head
   ```
8. Run backend tests:
   ```bash
   cd backend
   uv run pytest
   ```
9. Run frontend checks:
   ```bash
   cd frontend
   pnpm install
   pnpm lint
   pnpm test
   ```

Each project runs in a persistent Daytona sandbox. Git, files, commands, terminal,
preview servers, and `code-server` all operate inside `/workspace` in that sandbox.
The editor and preview use short-lived signed Daytona URLs.

The selected Daytona snapshot must provide a Linux shell, Git, Python 3,
`code-server`, and standard process/network utilities used for commands and port
detection.

## Services

- Frontend: Vite React app on port `5173`.
- Backend: FastAPI app on port `8000`.
- PostgreSQL: port `5432`, persisted in the `postgres_data` Docker volume.
- Redis: port `6379`, persisted in the `redis_data` Docker volume.

Docker Compose also works with:

```bash
docker compose up --build
```

## Environment

The root `.env.example` lists required values for local development:

- `DATABASE_URL`
- `REDIS_URL`
- `BACKEND_CORS_ORIGINS`
- `VITE_API_BASE_URL`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`
- `WORKSPACE_ENV_ENCRYPTION_KEY`
- `DAYTONA_API_KEY`
- `DAYTONA_API_URL`
- `DAYTONA_TARGET`
- `DAYTONA_DEFAULT_SNAPSHOT`
- `DAYTONA_DEFAULT_LANGUAGE`
- `DAYTONA_AUTO_STOP_MINUTES`

Existing database rows can be provisioned and cloned into Daytona after the
Alembic upgrade:

```bash
cd backend
uv run python scripts/migrate_workspaces_to_daytona.py
```
