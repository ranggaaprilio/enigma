# Epic 11 - Production Hardening

## Goal

Prepare the app for secure, reliable, and scalable deployment.

## User Stories

- As an owner, I can trust that secrets are protected.
- As a developer, I can rely on the dashboard during long-running AI sessions.
- As an operator, I can monitor health, logs, and failures.

## Scope

This epic should begin after the MVP workflow works end to end.

## Backend Tasks

- Add structured JSON logging everywhere.
- Add request ID middleware.
- Add rate limiting for auth and AI prompt endpoints.
- Add background job system for long-running tasks.
- Add graceful shutdown handling.
- Add health endpoints:
  - `/health`
  - `/ready`
  - `/metrics`
- Add secret encryption service.
- Add command denylist and allowlist policy.
- Add audit coverage for destructive actions.
- Add backup job for PostgreSQL.

## Frontend Tasks

- Add global error boundary.
- Add reconnect behavior for WebSockets.
- Add offline/reconnecting indicators.
- Add permission-aware UI.
- Add mobile-responsive monitoring layout.
- Add keyboard shortcut registry.

## Infrastructure Tasks

- Add production Dockerfiles.
- Add reverse proxy config using Caddy or Traefik.
- Add HTTPS config.
- Add environment variable documentation.
- Add database backup and restore docs.
- Add monitoring with Prometheus-compatible metrics.
- Add deployment checklist.

## Database Tasks

- Review indexes for all high-volume tables.
- Add retention policy for agent events and command logs.
- Add backup and restore procedure.

## Security Requirements

- HTTPS/WSS only in production.
- Encrypt API keys, Git tokens, and environment variables at rest.
- Redact secrets from logs.
- Prevent path traversal in file APIs.
- Prevent command execution outside workspace sandbox.
- Use isolated workspace containers before public multi-tenant release.

## Acceptance Criteria

- App can be deployed with documented production environment variables.
- Health and readiness endpoints work.
- Metrics endpoint exposes useful service metrics.
- WebSockets reconnect gracefully after temporary disconnect.
- Secrets are encrypted at rest.
- Destructive actions are audited.
- Backups can be restored in a test environment.

## Future Enhancements

- Kubernetes deployment.
- Workspace container isolation with resource quotas.
- Multi-region deployment.
- SOC 2 and GDPR readiness.
- SSO/SAML for enterprise.

