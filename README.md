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
