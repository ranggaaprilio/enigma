# AI Handoff Template

Use this template when asking an AI to implement one epic at a time.

## Prompt Template

```text
You are building an AI Agent Development Dashboard.

Tech stack:
- Backend: FastAPI, Python, SQLAlchemy, Alembic
- Frontend: React, TypeScript, Vite, Tailwind CSS
- Database: PostgreSQL
- Cache/session/stream state: Redis
- Local runtime: Docker Compose

Important rules:
- Follow the existing project structure.
- Keep changes scoped to this epic.
- Add or update database migrations when schema changes.
- Add backend tests for service logic and API endpoints.
- Add frontend tests for critical UI behavior.
- Do not store secrets in plain text.
- Do not implement future-scope items unless this epic explicitly requires them.

Task:
Implement the epic described below.

Expected output:
1. List files changed.
2. Explain important implementation decisions.
3. Explain how to run the feature locally.
4. Explain how to test the feature.
5. Mention anything intentionally deferred.

Epic file:
[paste one epic markdown file here]
```

## Recommended AI Execution Pattern

Ask the AI to work in this order:

1. Read the current project structure.
2. Identify existing patterns.
3. Create or update backend models, schemas, services, and routes.
4. Create or update Alembic migrations.
5. Create or update frontend pages, components, API clients, and stores.
6. Add tests.
7. Run formatting, linting, and tests.
8. Report the final result.

## Per-Epic Completion Checklist

- Backend routes exist and are documented.
- Frontend can call the backend successfully.
- Database migrations run cleanly.
- Authorization checks are applied.
- Errors are handled with useful messages.
- Loading and empty states exist in the UI.
- Tests cover the most important happy path and failure path.
- README or developer notes are updated if setup changed.

## Suggested First Prompt

Start with:

```text
Please implement docs/implementation-plan/01-project-foundation.md.
Create the initial project structure and make sure I can run the app locally with Docker Compose.
```

After that, move through the files in numeric order.

