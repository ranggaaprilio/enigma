# Contributing to Enigma

Thanks for your interest in Enigma. This document explains how to set up a development
environment, the conventions the codebase follows, and what a pull request needs before it can
be merged.

By contributing you agree that your contributions are licensed under the project's
[AGPL-3.0](LICENSE) license.

## Before you start

- Check the [open issues](https://github.com/ranggaaprilio/enigma/issues) and
  [pull requests](https://github.com/ranggaaprilio/enigma/pulls) to avoid duplicate work.
- For anything larger than a bug fix, open an issue first so the approach can be discussed before
  you invest time. The planning documents under `docs/` describe where the project is heading.
- Security problems go through [`SECURITY.md`](SECURITY.md), never a public issue.

## Development setup

The repository is a monorepo:

| Package | Language | Tooling | Location |
|---|---|---|---|
| backend | Python 3.12+ | `uv`, FastAPI, SQLAlchemy 2 (async), Alembic, pytest, ruff | `backend/` |
| frontend | TypeScript 5 | `pnpm`, React 18, Vite, Tailwind 3, Vitest, ESLint | `frontend/` |

The quickest way to a running stack is the [Quick Start](README.md#quick-start) in the README
(compose with either local Docker sandboxes or Daytona). For working on the code itself:

```bash
# backend
cd backend
uv sync
uv run alembic upgrade head          # needs Postgres + Redis (compose provides them)
uv run uvicorn app.main:app --reload

# frontend
cd frontend
pnpm install
pnpm dev
```

[`AGENTS.md`](AGENTS.md) is the authoritative reference for commands, architecture, and the
project's gotchas (async-only backend, `asyncpg`, cookie sessions, the sandbox provider boundary,
queue mode, migrations policy, …). Read it before touching the backend.

## Running the checks

Every pull request must pass the same checks CI runs (`.github/workflows/ci.yml`):

```bash
# backend
cd backend
uv run ruff check .
uv run pytest

# frontend
cd frontend
pnpm lint
pnpm test
pnpm build
```

Real-daemon tests for the Docker sandbox provider are opt-in and need a local Docker or Podman
daemon:

```bash
cd backend && DOCKER_INTEGRATION_TESTS=1 uv run pytest tests/test_docker_integration.py
```

## Conventions

### Backend

- **Async everywhere.** All database and service calls are `await`-ed. Use `async_session_maker`;
  never block the event loop.
- **Domain layout.** Each domain has `api/`, `models/`, `schemas/`, `services/`. Put logic in a
  service, keep routes thin.
- **Migrations are expand-only.** One Alembic revision per schema change, named
  `NNNN_snake_case.py`. A migration that drops, renames, retypes, or tightens a column must carry a
  `# contract: <why>` comment and ship one release after the expand step.
- **Sandbox boundary.** Only `services/sandbox/daytona.py` may import the `daytona` SDK, and only
  `services/sandbox/docker.py` may talk to the Docker daemon. Everything else goes through the
  `SandboxRuntime` interface. `tests/sandbox/test_boundary.py` enforces this.
- **Secrets.** Never log or return credentials. Use the existing Fernet cipher for anything stored.
- **Process-local state** goes through `services/runtime.py`; module-level dicts of in-flight work
  are rejected by a test.
- Lint with `ruff` (rules E, F, I, UP, B). No mypy; keep type hints accurate anyway.

### Frontend

- No component library and no state library: Tailwind utility classes and React Context only.
- All HTTP goes through the typed functions in `src/lib/api.ts`. Never call `fetch()` directly.
- Icons come from `lucide-react` only.
- Follow the design language in `frontend/design.md` (warm canvas, single brand colour, hairline
  borders, 8px CTA radius).

### Tests

- Backend: `pytest` with `pytest-asyncio` in auto mode under `backend/tests/`. Add a test for
  every behaviour change; route tests may override rate-limit dependencies.
- Frontend: Vitest + Testing Library under `frontend/src/`.
- A fix for a bug should come with a test that fails without the fix.

## Pull request workflow

1. Fork the repository and create a branch from `main`:
   `feature/<short-name>`, `fix/<short-name>`, or `docs/<short-name>`.
2. Keep the change focused. Unrelated refactors and formatting churn make review slower and
   belong in their own PR.
3. Write commit messages in the imperative mood with a short type prefix, for example
   `feat(sandbox): add disk quota to Docker provider` or `fix(agents): stop run on lease expiry`.
4. Update documentation that your change affects: `README.md`, `AGENTS.md`, `.env.example`,
   `docs/self-host-docker.md`, or the relevant `docs/` plan.
5. Make sure the checks above pass locally, then open the pull request using the template.
   Describe what changed, why, and how you verified it.
6. A maintainer will review. Please respond to review comments in the PR rather than force-pushing
   silently, so the conversation stays attached to the code.

## Reporting bugs and requesting features

Use the issue templates. A good bug report includes the sandbox runtime you use (Docker/Podman or
Daytona), the `/health` output, relevant backend logs, and the steps to reproduce.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind.
