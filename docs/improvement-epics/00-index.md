# Enigma Improvement Epics

This folder breaks the post-MVP improvement backlog into AI-ready, executable epics.
It complements (does not replace) `docs/implementation-plan/`, which covers the original
MVP build (Epics 0–11). Everything here assumes the MVP workflow works end to end:
Projects → Teams → Workspaces, Daytona sandboxes, GitHub clone/commit/push, the
OpenAI/OpenHands agent, terminal, code-server editor, preview, files, commands,
notifications, and audit logs.

Use the same per-epic execution pattern described in
`docs/implementation-plan/13-ai-handoff-template.md`: implement one epic at a time,
add migrations + tests, keep changes scoped.

## The Epics

| # | Epic | Priority | Theme |
|---|------|----------|-------|
| 01 | [Spec-Driven Development + Human-in-the-Loop](./01-spec-driven-development.md) | **P1** | Quality / process |
| 02 | [VCS: Pull Requests & Branch Workflow](./02-vcs-pr-and-branch-workflow.md) | **P1** | Core product loop |
| 03 | [Agent Editing Tools (patch / search / safe git)](./03-agent-editing-tools.md) | P2 | Agent capability |
| 04 | [Anthropic / Claude Agent Provider](./04-anthropic-claude-provider.md) | P2 | Agent quality |
| 05 | [Multi-Turn Agent Sessions](./05-multi-turn-agent-sessions.md) | P2 | Agent UX |
| 06 | [Cost & Usage Analytics](./06-cost-and-usage-analytics.md) | P2 | Operations |
| 07 | [Observability, Production Hardening & CI](./07-observability-and-hardening.md) | P2 | Reliability |
| 08 | [Real-Time Collaboration](./08-realtime-collaboration.md) | P3 | Collaboration |
| 09 | [Workspace Templates & Multi-Provider Git](./09-templates-and-git-providers.md) | P3 | Onboarding / growth |

## Recommended Build Order

These epics are mostly independent, but there are a few dependencies. A pragmatic order:

1. **04 — Claude provider** (small, ~1–2 days). Adds a first-class high-quality coding
   model and a per-workspace provider/model selector. Makes every later agent-driven
   epic better, especially SDD.
2. **02 — PR & branch workflow** (closes the "AI writes code → review → ship" loop).
3. **01 — Spec-Driven Development + Human-in-the-Loop** (the headline process change).
   Benefits from 02 (it ends in a PR gate) and 04 (better spec/plan generation), but can
   start in parallel using existing providers.
4. **03 — Agent editing tools** (cheaper, safer edits; helps SDD `implement` phase).
5. **07 — Observability & hardening + CI** (foundational; can run in parallel from day 1,
   especially the CI gate, since the repo currently has no test/lint CI).
6. **05, 06, 08, 09** as capacity allows.

## Dependency Notes

- **01 (SDD)** reuses the agent-run streaming/events infrastructure and is most useful
  with **02** (PR gate), **03** (patch edits in `implement`), and **04** (Claude).
- **06 (cost analytics)** is unblocked today because `agent_runs.tokens_used` already
  exists — it is simply not surfaced. **04** should make providers report token usage
  consistently.
- **08 (collaboration)** depends on **07**'s WebSocket reconnect work.

## Shared Rules For Every Epic

- Clear API contracts before UI.
- A database migration for every schema change (Alembic, async engine).
- Backend tests (`pytest`) for service logic + routes; frontend tests (Vitest) for
  critical state transitions.
- Never store secrets in plain text — reuse `WorkspaceSecretCipher` (Fernet).
- Apply authorization checks consistently (project/team/workspace membership + role).
- Keep backend modules organized by domain (`api/`, `models/`, `schemas/`, `services/`).

## Grounding: Current State Snapshot (June 2026)

Verified against the codebase so epics target real gaps, not assumptions:

- **Agent providers**: only `openai` (`agent_providers.py`) and `openhands`
  (`openhands_provider.py`). No Anthropic/Claude. `agent_provider` defaults to `"auto"`.
- **Agent tools** (`agent_tools.py`): `list_files`, `read_file`, `write_file`
  (full-file overwrite), `run_command`. `git` is in the `DANGEROUS_COMMANDS` denylist;
  there is no patch-edit or search/grep tool.
- **Git** (`repositories.py`): GitHub-only (`sanitize_repository_url` rejects non
  `github.com`); supports clone/status/diff/pull/commit/push. Commit author is
  hardcoded `"Execute Dashboard" <execute@localhost>` (`repositories.py:241`). No PR
  or branch-management endpoints.
- **Agent runs** (`models/agent.py`): one `prompt` per run, no conversation/parent
  linkage. `tokens_used` column exists but is not surfaced anywhere.
- **Health/observability**: only `/health` exists. No `/ready`, `/metrics`, request-id
  middleware, or Prometheus metrics. Rate limiting covers login only.
- **CI**: only `.github/workflows/ai-code-review.yml` (AI PR review). No test/lint/
  typecheck/build gate. 18 backend test files, 6 frontend test files.
- **Sandbox**: Daytona, custom snapshot built via `daytona/build-snapshot.sh` +
  `daytona/Dockerfile` (mise + code-server baked in; runtime egress is restricted).
