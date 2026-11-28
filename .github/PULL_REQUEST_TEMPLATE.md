## What

<!-- What does this change do? One or two sentences. -->

## Why

<!-- The problem it solves or the issue it closes. Use "Closes #123" when applicable. -->

## How was it verified?

<!-- Tests added or run, manual steps, sandbox runtime used (OpenSandbox/Daytona). -->

## Checklist

- [ ] `cd backend && uv run ruff check . && uv run pytest` pass
- [ ] `cd frontend && pnpm lint && pnpm test && pnpm build` pass
- [ ] New behaviour has tests; a bug fix has a regression test
- [ ] Schema changes ship as one expand-only Alembic migration (or carry `# contract:`)
- [ ] Docs updated where affected (`README.md`, `AGENTS.md`, `.env.example`, `docs/`)
- [ ] No secrets, tokens, or personal data in the diff or the description
