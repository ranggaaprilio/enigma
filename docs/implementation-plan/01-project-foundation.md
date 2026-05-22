# Epic 0 - Project Foundation

## Goal

Create the base full-stack project structure for a server-based AI agent dashboard using FastAPI, React, and PostgreSQL.

## User Value

Developers can start the app locally with one command and have a clean foundation for all future epics.

## Scope

- Monorepo structure
- Backend FastAPI app
- Frontend React app
- PostgreSQL and Redis via Docker Compose
- Environment configuration
- Basic health checks
- Initial CI-friendly test commands

## Suggested Structure

```text
.
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/
│   │   ├── db/
│   │   ├── models/
│   │   ├── schemas/
│   │   ├── api/
│   │   └── services/
│   ├── alembic/
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   ├── package.json
│   └── vite.config.ts
├── docker-compose.yml
├── .env.example
└── README.md
```

## Backend Tasks

- Create FastAPI application with `/health` endpoint.
- Configure SQLAlchemy database connection.
- Configure Alembic migrations.
- Add Redis client configuration.
- Add structured JSON logging.
- Add CORS config for local frontend.
- Add settings loader from environment variables.

## Frontend Tasks

- Create React + TypeScript app with Vite.
- Install Tailwind CSS.
- Add base layout with sidebar, top bar, main panel, right agent panel, status bar.
- Add API client wrapper for backend calls.
- Add placeholder route for dashboard.
- Add loading, error, and empty states.

## Database Tasks

- Create empty initial Alembic migration.
- Verify PostgreSQL connection from backend.

## Docker Tasks

- Add `docker-compose.yml` with:
  - `postgres`
  - `redis`
  - `backend`
  - `frontend`
- Add persistent volumes for Postgres and Redis.
- Add `.env.example` with all required values.

## Acceptance Criteria

- `docker compose up` starts frontend, backend, PostgreSQL, and Redis.
- Backend `/health` returns status `ok`.
- Frontend can call backend health endpoint.
- Alembic can run migrations successfully.
- README explains local setup in under 10 steps.

