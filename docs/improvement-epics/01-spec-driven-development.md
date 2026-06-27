# Epic 01 - Spec-Driven Development (SDD) + Human-in-the-Loop

## Goal

Make the agent build software through an explicit, reviewable **Spec-Driven Development**
pipeline instead of jumping straight from a prompt to code. Adopt the
[GitHub Spec Kit](https://github.com/github/spec-kit) workflow
(`constitution → specify → clarify → plan → tasks → implement`) and require a **human to
validate each artifact — the spec in particular — before the agent is allowed to
implement**.

## User Value

- A developer describes *what* they want; the agent produces a reviewable spec, plan, and
  task list before writing any code.
- A reviewer must approve the spec (and plan/tasks) before implementation runs — no
  surprise diffs, no wasted agent runs on a misunderstood request.
- Every feature ships with durable, versioned artifacts (`spec.md`, `plan.md`,
  `tasks.md`) committed alongside the code.

## User Stories

- As a developer, I can start a "spec workflow" for a workspace from a natural-language
  feature description.
- As a developer, I can see each phase's generated artifact (constitution, spec, plan,
  tasks) rendered as Markdown.
- As a reviewer, I must **approve or request changes** on the spec before the workflow
  advances; the `implement` phase is hard-blocked until the plan and tasks are approved.
- As a reviewer, when I request changes I can add notes, and the agent re-generates that
  phase using my feedback.
- As a developer, the approved artifacts and the implementation land on a feature branch
  and open a Pull Request (via Epic 02).

## Background: How Spec Kit Works

Install the CLI once (`specify`); scaffolding then runs **offline** from templates bundled
in the package — important for Enigma's network-restricted sandboxes.

```bash
# Install (build host / sandbox image — needs network at install time only)
uv tool install specify-cli --from git+https://github.com/github/spec-kit.git
# (optionally pin a release:  ...spec-kit.git@vX.Y.Z)

# Scaffold inside a cloned repo (offline; --ignore-agent-tools skips the interactive
# coding-agent CLI checks since Enigma drives its own provider)
specify init . --integration claude --script sh --ignore-agent-tools --force
```

`specify init` lays down:

```
.specify/
├── memory/constitution.md            # project principles (phase: constitution)
├── templates/
│   ├── spec-template.md              # drives the `specify` phase
│   ├── plan-template.md              # drives the `plan` phase
│   └── tasks-template.md             # drives the `tasks` phase
└── scripts/bash/                     # helper scripts (branch/feature numbering)
.claude/commands/speckit.*.md         # the per-phase prompt definitions
```

Each feature gets a numbered folder:

```
specs/001-feature-name/
├── spec.md          # what & why (specify)
├── plan.md          # tech stack & architecture (plan)
├── tasks.md         # ordered, actionable tasks (tasks)
├── research.md  data-model.md  contracts/  quickstart.md   # optional supporting docs
```

Phases (Spec Kit slash commands → Enigma phases):

| Enigma phase | Spec Kit command | Produces | Human gate |
|---|---|---|---|
| `constitution` | `/speckit.constitution` | `.specify/memory/constitution.md` | Optional (one-time per repo) |
| `specify` | `/speckit.specify` | `specs/NNN/spec.md` | **Required** |
| `clarify` | `/speckit.clarify` | clarifications appended to `spec.md` | **Required** |
| `plan` | `/speckit.plan` | `specs/NNN/plan.md` (+ data-model, contracts) | **Required** |
| `analyze` | `/speckit.analyze` | cross-artifact consistency report | Review (advisory) |
| `tasks` | `/speckit.tasks` | `specs/NNN/tasks.md` | **Required** |
| `implement` | `/speckit.implement` | code changes in a dedicated **git worktree** / feature branch (Epic 02) | **Hard-blocked** until plan + tasks approved |

## Installing Spec Kit on the Server (Sandbox)

The `specify` CLI must exist inside the Daytona sandbox. Because runtime egress is blocked
(same reason `mise` and `code-server` are baked into the snapshot — see
`daytona/Dockerfile`), bake `specify` into the snapshot at **build** time. `specify init`
then runs offline.

Add to `daytona/Dockerfile` (after the code-server install; this matches the existing
`$HOME/.local/bin` pattern that the runtime PATH already includes):

```dockerfile
# Install GitHub Spec Kit (the `specify` CLI) for Spec-Driven Development.
# Build-time network only; `specify init` afterwards scaffolds offline from the
# templates bundled in the package, so network-restricted sandboxes can run SDD.
ARG SPEC_KIT_REF=
RUN set -eux; \
    curl -fsSL https://astral.sh/uv/install.sh | sh; \
    if [ -n "${SPEC_KIT_REF}" ]; then \
      SRC="git+https://github.com/github/spec-kit.git@${SPEC_KIT_REF}"; \
    else \
      SRC="git+https://github.com/github/spec-kit.git"; \
    fi; \
    "$HOME/.local/bin/uv" tool install specify-cli --from "${SRC}"; \
    "$HOME/.local/bin/specify" check || true
```

Then rebuild and republish the snapshot with `./daytona/build-snapshot.sh` and recreate
sandboxes so they pick up the new image.

> A starter version of this Dockerfile change has been applied as part of this epic — review
> the diff, pin `SPEC_KIT_REF` to a tagged release for reproducibility, and verify the build
> against your Daytona target before relying on it.

## Architecture in Enigma

Enigma drives its **own** agent provider (OpenAI/OpenHands/Claude) — it does not run an
interactive coding-agent CLI. So SDD is orchestrated by the backend:

1. `specify init` scaffolds `.specify/` + `specs/` in the workspace (once per repo).
2. For each phase, the backend reads the corresponding Spec Kit prompt + template, builds
   an agent run (reusing the existing agent infrastructure), and writes the resulting
   artifact into `specs/NNN-feature/`.
3. After the phase completes, its status becomes `awaiting_review`. The orchestrator
   **halts** until a human approves.
4. On approval, the next phase is unlocked. On "request changes", the phase re-runs with
   the reviewer's notes appended to the prompt.

## Backend Tasks

- Add `SpecKitService` (`services/speckit.py`):
  - `ensure_initialized(workspace)` — run `specify init . --script sh --ignore-agent-tools`
    in the sandbox if `.specify/` is absent; detect/return the constitution.
  - `run_phase(workflow, phase, feedback=None)` — load the phase template/prompt, run the
    agent against the cloned repo, persist the produced artifact path, set status.
  - Read artifact content for review (`spec.md`, `plan.md`, `tasks.md`).
- Add models + migration:
  - `spec_workflows` (`id`, `workspace_id`, `user_id`, `feature_name`, `feature_slug`,
    `branch`, `spec_dir` e.g. `specs/001-...`, `status`, `current_phase`, timestamps).
  - `spec_phases` (`id`, `workflow_id`, `phase`, `status`
    [`pending`,`running`,`awaiting_review`,`approved`,`changes_requested`,`completed`,`error`],
    `artifact_path`, `agent_run_id` FK → `agent_runs`, `reviewed_by`, `reviewed_at`,
    `review_notes`, `attempt`, timestamps).
- Endpoints (mounted under the existing workspace router):
  - `POST   /workspaces/{id}/spec/workflows` — start a workflow from a feature description.
  - `GET    /workspaces/{id}/spec/workflows` / `GET .../{workflow_id}` — list / detail with phases.
  - `GET    /workspaces/{id}/spec/workflows/{wid}/phases/{phase}/artifact` — rendered Markdown.
  - `POST   .../phases/{phase}/approve` — approve; unlock next phase.
  - `POST   .../phases/{phase}/request-changes` — body `{notes}`; re-run phase with feedback.
  - `POST   .../phases/{phase}/run` — explicitly trigger a phase (e.g. start `implement`).
  - `WS     .../phases/{phase}/stream` — reuse the agent-run stream for live output.
- Enforce the gate server-side: reject a `run` of `implement` unless `plan` and `tasks`
  phases are `approved`. This is a security/correctness boundary, not just UI.
- Authorization: only workspace owner / team `lead` / project `admin` may approve
  (configurable). Record approver for audit.
- Emit notifications + audit-log entries on `awaiting_review`, `approve`, and
  `request-changes` (reuse `services/notifications.py` and `services/audit.py`).

## Frontend Tasks

- Add a **Spec** view to the workspace dashboard (sits alongside `agents`, `repository`,
  etc. in `App.tsx` routes and the dashboard shell).
- Build a **phase stepper**: constitution → specify → clarify → plan → analyze → tasks →
  implement, with per-phase status badges (`pending`, `running`, `awaiting_review`,
  `approved`, `changes requested`, `error`).
- Artifact viewer: render `spec.md`/`plan.md`/`tasks.md` as Markdown; show re-generation
  history (attempts).
- Review controls per gated phase: **Approve** and **Request changes** (notes textarea).
- Hard-block the `implement` action in the UI until plan + tasks are approved, with a clear
  "Approve & run implementation" confirmation.
- Live phase output via the existing agent stream component (reuse `AgentTab`).
- Notification surface when a phase is `awaiting_review`.

## Database Tasks

- Migrations for `spec_workflows` and `spec_phases`.
- Indexes on `workspace_id`, `workflow_id`, `status`, `current_phase`.
- FK from `spec_phases.agent_run_id` → `agent_runs.id` (reuse existing run/event storage).

## Security Requirements

- The `implement` gate must be enforced in the service layer, not just the UI.
- `specify init` and all phase commands run inside the sandbox workspace only (no host
  execution); reuse the existing path-traversal and command restrictions.
- Approvals are immutable audit records (who/when/notes).

## Acceptance Criteria

- `specify` is available inside the sandbox snapshot and `specify init` succeeds offline.
- Starting a workflow generates `spec.md`; the workflow halts at `awaiting_review`.
- The `implement` phase cannot run (API returns 409/403) until `plan` and `tasks` are
  approved.
- "Request changes" with notes re-runs the phase and produces an updated artifact.
- Approved artifacts are committed to the feature branch and (with Epic 02) a PR is opened.
- All gate transitions are audited and notified.

## Dependencies

- Reuses the agent-run streaming/events infrastructure (Epic 4 of the original plan).
- Best paired with **Epic 02** (the workflow ends in a PR) and **Epic 04** (Claude
  produces stronger specs/plans). **Epic 03** improves the `implement` phase.

## Future Enhancements

- `/speckit.analyze` as a blocking quality gate with auto-generated checklists
  (`/speckit.checklist`).
- `/speckit.taskstoissues` to mirror tasks into GitHub issues.
- Spec Kit **presets** for org-specific conventions (e.g. a Hubexo preset) and
  project-local template overrides in `.specify/templates/overrides/`.
- Constitution managed at the **project** level and inherited by all workspaces.
- Re-runnable "converge" pass (`/speckit.converge`) to detect spec/code drift.
