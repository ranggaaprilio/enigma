# Epic 07 - Observability, Production Hardening & CI

## Goal

Make Enigma safe and reliable to operate. This is the concrete, code-grounded execution of
`docs/implementation-plan/12-production-hardening.md` (mostly unbuilt) **plus** a real CI
quality gate, which the repo currently lacks.

## User Value

Operators can monitor health and cost of failure; developers get a CI safety net; deployments
stop shipping insecure defaults.

## Current State

- Only `/health` exists (`api/health.py`). **No** `/ready`, `/metrics`, request-id
  middleware, or Prometheus metrics.
- Rate limiting covers **login only** (`auth_login_rate_limit_*`); AI/agent endpoints are
  unbounded.
- **Insecure defaults in `config.py`**: `auth_secret_key` and
  `workspace_env_encryption_key` ship with hardcoded default values; `auth_cookie_secure`
  defaults to `False`.
- CI is a single workflow (`.github/workflows/ai-code-review.yml`) that only runs an AI PR
  review — **no test/lint/typecheck/build gate**. 18 backend test files, 6 frontend.

## Scope

- Health/readiness/metrics endpoints + request correlation.
- Rate limiting for agent/AI endpoints.
- Secret hygiene (refuse insecure defaults in non-local environments).
- WebSocket reconnection + frontend resilience.
- A proper CI pipeline.

## Backend Tasks

- Add `/ready` (checks Postgres + Redis) and `/metrics` (Prometheus-compatible).
- Request-ID middleware; bind the ID into `structlog` context and echo it in responses.
- Rate-limit agent run creation and other expensive endpoints (reuse `services/rate_limit.py`
  + Redis), per user/workspace.
- **Fail fast on insecure config** when `environment != "local"`: refuse to boot if
  `auth_secret_key` / `workspace_env_encryption_key` are still defaults; default
  `auth_cookie_secure=True` in production.
- Audit coverage for destructive actions (workspace/repo deletion, env-var changes).
- Optional: a background job runner so long agent/SDD runs aren't tied to the request/WS
  lifecycle; graceful shutdown handling.

## Frontend Tasks

- WebSocket reconnect with backoff for the agent, terminal, command, and notification
  streams (helpers already exist in `lib/api.ts`).
- Global React error boundary.
- Offline / reconnecting indicators.
- Permission-aware UI (hide/disable actions the user's role can't perform).

## CI Tasks (`.github/workflows/`)

- New `ci.yml` running on PRs:
  - Backend: `uv sync`, `uv run ruff check .`, `uv run pytest`.
  - Frontend: `pnpm install`, `pnpm lint`, `pnpm test`, `pnpm build` (tsc typecheck).
  - Cache `uv` and pnpm stores.
- Make it a required check; keep the AI review as a separate, non-blocking workflow.
- Expand frontend test coverage (only 6 files today) for critical flows.

## Database Tasks

- Review indexes on high-volume tables (`agent_events`, `command` logs, `notifications`).
- Retention/rollup policy for `agent_events` and command logs.
- Document backup/restore (build on `scripts/clean_data.sh`).

## Security Requirements

- HTTPS/WSS only in production; secure cookies.
- Encrypt API keys / git tokens / env vars at rest (already via `WorkspaceSecretCipher` —
  verify coverage) and redact secrets from logs.
- Keep path-traversal and command-sandbox protections; prefer the allowlist from Epic 03.

## Acceptance Criteria

- `/health`, `/ready`, `/metrics` all work and `/ready` reflects dependency health.
- Requests carry a correlation ID end to end.
- The app refuses to start in non-local env with default secrets.
- WebSockets reconnect after a transient disconnect without a page reload.
- CI runs lint + tests + typecheck + build on every PR and can block merge.

## Dependencies

- Foundational; the CI gate should land early. WebSocket reconnect is a prerequisite for
  **Epic 08**.

## Future Enhancements

- Kubernetes deployment + per-workspace resource quotas (from the original plan).
- 2FA/MFA, SSO/SAML.
- Distributed tracing.
