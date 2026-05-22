# AI Agent Development Dashboard - Implementation Plan

This folder breaks the PRD into AI-ready implementation tasks. The target stack is:

- Backend: FastAPI, Python, SQLAlchemy, Alembic, WebSocket support
- Frontend: React, TypeScript, Vite, Tailwind CSS, shadcn/ui style components
- Database: PostgreSQL
- Cache/session/stream state: Redis
- Runtime: Docker Compose for local development, later Docker/Kubernetes for production

## Recommended Build Order

1. [01-project-foundation.md](./01-project-foundation.md)
2. [02-auth-and-users.md](./02-auth-and-users.md)
3. [03-workspace-management.md](./03-workspace-management.md)
4. [04-repository-management.md](./04-repository-management.md)
5. [05-ai-agent-interaction.md](./05-ai-agent-interaction.md)
6. [06-terminal-access.md](./06-terminal-access.md)
7. [07-vscode-web-integration.md](./07-vscode-web-integration.md)
8. [08-preview-system.md](./08-preview-system.md)
9. [09-custom-commands.md](./09-custom-commands.md)
10. [10-file-management.md](./10-file-management.md)
11. [11-notifications-and-audit.md](./11-notifications-and-audit.md)
12. [12-production-hardening.md](./12-production-hardening.md)

## MVP Boundary

Build the MVP first with:

- Email/password auth
- One workspace per user
- GitHub repository clone, diff, commit, push
- Prompt UI connected to one AI provider
- Streaming AI output
- Single terminal session
- Basic code-server embed
- One detected preview port
- PostgreSQL persistence

Delay these until after MVP:

- GitLab and Bitbucket
- Multiple workspaces per user
- Team collaboration
- Multi-tab terminal
- Pull request creation
- Slack/Discord/email notifications
- Marketplace
- Kubernetes multi-tenant scaling

## Shared Development Rules For Every Task

- Use clear API contracts before building UI.
- Add database migrations for every schema change.
- Add backend tests for service logic and API routes.
- Add frontend tests for critical forms and state transitions.
- Never store secrets in plain text.
- Prefer small, reviewable commits per epic.
- Keep backend modules organized by domain: `auth`, `workspaces`, `repositories`, `agents`, `terminal`, `preview`, `commands`, `audit`.

