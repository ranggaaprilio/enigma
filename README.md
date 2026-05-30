# Execute Dashboard

Server-based AI agent dashboard foundation built with FastAPI, React, PostgreSQL, and Redis.

## Local Setup

1. Install `uv`, `pnpm`, and Podman.
2. Copy `.env.example` to `.env`.
3. Start the full stack:
   ```bash
   podman compose up --build
   ```
4. Open the frontend at `http://localhost:5173`.
5. Check the backend health endpoint at `http://localhost:8000/health`.
6. Run backend migrations when needed:
   ```bash
   cd backend
   uv run alembic upgrade head
   ```
7. Run backend tests:
   ```bash
   cd backend
   uv run pytest
   ```
8. Run frontend checks:
   ```bash
   cd frontend
   pnpm install
   pnpm lint
   pnpm test
   ```

The editor runs as a standalone `code-server` container. The backend returns a
browser-openable editor URL for the selected workspace, and the frontend opens
that URL in a new tab. The code-server container and backend both mount the same
host workspace root at `/workspace`.

## Services

- Frontend: Vite React app on port `5173`.
- Backend: FastAPI app on port `8000`.
- code-server: VS Code Web on port `8080`.
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
- `WORKSPACE_ROOT`
- `HOST_WORKSPACE_ROOT`
- `WORKSPACE_ENV_ENCRYPTION_KEY`
- `CODE_SERVER_PASSWORD`
- `CODE_SERVER_PORT`
- `EDITOR_PUBLIC_URL`
- `EDITOR_CONTAINER_WORKSPACE_ROOT`
- `EDITOR_TOKEN_TTL_SECONDS`
