# Enigma — Verifiable Agent Loop: Spec Kit Integration (Plan 3)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** Wire the verification loop into the Spec Kit pipeline so spec §3 runs end to end: **Plan** (Spec Kit phases, human-approved) → **Generate** (`implement` agent run in the feature worktree) → **Self-test** (verification run in that same worktree: test → fix → observe) → **Human gate** (approve the evidence) → the workflow is complete.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §3, §8.
**Previous plans:** `2026-11-26-enigma-verifiable-agent-loop.md`, `2026-11-26-enigma-verification-close-the-loop.md` (Plan 2, PR #22), plus PR #23 (live OpenSandbox fixes).

## Current state (verified in code)

- `implement` is a `TERMINAL_PHASES` member. When its agent run completes, `_finalize_phase_inner` commits and sets the phase to `completed` and the workflow to `completed`, with **no review step** (`services/speckit.py` ~963-980). Nothing checks that the generated code passes tests.
- Feature phases run in `workflow.worktree_path` (`worktrees/<slug>`, `_working_dir`, speckit.py:653). Relative paths resolve against `SANDBOX_HOME` on every provider. The verification runner and fixer already take `criteria.cwd`, so a verification run can target the worktree without new runtime work.
- `verification_runs` has no link to a spec workflow, and `plan_ref` is free text.
- Workspaces can have a custom command with `role="test"` (`models/command.py`, auto-detected by `run_detection.py`), but nothing uses it as a verification default.
- SpecPanel shows Approve/Request changes only for `awaiting_review`. It polls every 3 s while the workflow is `pending`/`running`.

## Design

**Where the criteria come from**, in this order:
1. **The workflow's own `verification` criteria**, set when the workflow is created (optional fields: test command, observe port, max iterations).
2. **Otherwise, the workspace's first enabled `role="test"` custom command.** Its command is used, with its `cwd` taken relative to the worktree. `max_iterations` defaults to 2.
3. **Otherwise, no criteria:** `implement` completes exactly as today. This keeps existing workflows and workspaces unchanged.

**After `implement` completes** (both modes share `_finalize_phase_inner`, inline via the task and queue via `finalize_spec_phase_job`):
- If criteria resolve, create a verification run with:
  - `plan_ref = "<spec_dir>/plan.md"`
  - `cwd = workflow.worktree_path`
  - `created_by` = the phase's user
  - `spec_workflow_id` = the workflow

  Start the run through `start_verification_run`, then set the phase to `awaiting_review` and the workflow to `awaiting_review`.
- If the run cannot be created (another verification run is active), still park at `awaiting_review` and send a notification with the reason. The human can use *Re-run verification*.

**The gate:**
- **Approving the linked verification run** (the existing `/verification/runs/{id}/approve`) also approves `implement`: phase `approved`, workflow `completed`, with an audit entry and a notification. This happens in the same transaction as the run's `review → approved`, so the two cannot disagree.
- **Rejecting it** leaves `implement` at `awaiting_review`. The human then either *Requests changes* on `implement` (the existing flow, which re-runs the agent with notes and triggers a fresh verification afterwards) or *Re-runs verification*.
- `approve_phase("implement")` is refused (409) while the workflow has verification criteria. The evidence gate is the only way through. Without criteria nothing changes.
- **New endpoint:** `POST /workspaces/{id}/spec/workflows/{wid}/verify` (re)starts a linked verification run while `implement` is `awaiting_review`.

## Global Constraints

- Migration `0042` is expand-only: two nullable columns plus an index.
- Async-only. No module-level state. Every status write stays conditional (`_transition` for verification; `phase.status` guards for Spec Kit).
- Verification and agent runs still go through `create_run` / `AgentService.create_run`, so budget, caps and guards apply.
- Spec Kit tests use the existing `test_specs.py` fixtures (`_fake_create_run`, `_noop_finalize`, `ScriptedRuntime`).

## Review Focus

- **Gate consistency:** a linked run's approval and the phase/workflow update are one transaction. An approve that loses the race changes neither.
- **No bypass:** `approve_phase("implement")` cannot skip the evidence when criteria exist.
- **Re-run safety:** *Request changes* or *Re-run verification* never leaves two active verification runs (the existing one-active guard), and a stale run's approval never completes a workflow whose `implement` has since been re-run. Approval counts only for the **latest** linked run.
- **Backwards compatibility:** a workflow with no criteria behaves exactly as before.

---

### Task 1: Link verification runs to workflows (migration `0042`)

**Files:** `alembic/versions/0042_verification_spec_link.py`, `models/speckit.py`, `models/verification.py`, `schemas/specs.py`, `schemas/verification.py`, model tests.

- [x] Write failing tests:
  - `spec_workflows.verification` JSONB (nullable)
  - `verification_runs.spec_workflow_id` (FK `spec_workflows`, `ON DELETE SET NULL`, indexed)
  - `SpecWorkflowCreateRequest.verification` (optional: `test_command`, `observe_port`, `max_iterations` 1–20) validates and round-trips
  - `VerificationRunResponse.spec_workflow_id` is exposed
- [x] Implement, `alembic upgrade`/`downgrade`/`upgrade`, green.
- [x] Commit `feat(verification): link verification runs to spec workflows`.

### Task 2: Resolve criteria for a workflow

**Files:** `services/verification/spec_link.py` (new), `tests/test_verification_spec_link.py`.

- [x] Write failing tests covering:
  - the workflow's own criteria win
  - fallback to the first enabled `role="test"` command, with its `cwd` joined under the worktree
  - a disabled or absent test command → `None`
  - `cwd` = `worktree_path` (or the base tree when there is no worktree yet)
  - `plan_ref = <spec_dir>/plan.md`
- [x] Implement `criteria_for_workflow(session, workspace, workflow) -> PlanCriteria | None`. Green.
- [x] Commit `feat(verification): derive verification criteria from a spec workflow`.

### Task 3: Verify after `implement`

**Files:** `services/speckit.py` (`_finalize_phase_inner`), `services/verification/spec_link.py`, `tests/test_specs.py`, `tests/test_worker_jobs.py`.

- [x] Write failing tests:
  - With criteria, a completed `implement` run → a linked verification run is created and started (start captured), and phase and workflow are `awaiting_review`.
  - Without criteria → `completed`, as today. An existing test pins this.
  - A refused create (one already active) → `awaiting_review` plus a notification, never `error`.
  - Queue mode: `finalize_spec_phase_job` takes the same path.
- [x] Implement, green.
- [x] Commit `feat(specs): verify the implement phase before completing a workflow`.

### Task 4: One gate for the evidence and the phase

**Files:** `api/verification.py` (`_finalize_gate`), `services/verification/spec_link.py`, `services/speckit.py` (`approve_phase`), `api/specs.py` (`/verify`), tests.

- [x] Write failing tests:
  - Approving the **latest** linked run → `implement` `approved` and workflow `completed` (audit + notification), in the same transaction.
  - Approving a **stale** linked run (implement re-run since) → the run is approved but the workflow is untouched.
  - Rejecting → `implement` stays `awaiting_review`.
  - `approve_phase("implement")` → 409 when criteria exist, unchanged otherwise.
  - `POST /spec/workflows/{wid}/verify` → a new linked run (202). 409 when one is active or `implement` is not `awaiting_review`.
  - *Request changes* on `implement`, then completion → a new verification run.
- [x] Implement, green.
- [x] Commit `feat(specs): approve implement through its verification evidence`.

### Task 5: Frontend — evidence in the SpecPanel

**Files:** `components/VerificationPanel.tsx` (extract `VerificationRunView`: header, evidence cards, polling, gate buttons), `components/SpecPanel/index.tsx`, `lib/api.ts`, tests.

- [x] Write failing tests:
  - `CreateWorkflowForm` sends the optional verification fields.
  - The `implement` phase shows the latest linked run (status, evidence, Approve/Reject) plus *Re-run verification* and *Request changes*.
  - The phase's own Approve button is hidden when the workflow has criteria.
  - Polling continues while the linked run is active.
  - The Verify panel still works through the extracted component (its existing tests stay green).
- [x] Implement, green (`tsc -b`, `eslint`, `vitest`, `vite build`).
- [x] Commit `feat(specs): show and gate implement's verification in the SpecPanel`.

### Task 6: Docs + live check

- [x] Add an AGENTS.md note (implement → verification → gate; criteria precedence; `0042`) and tick this plan.
- [ ] Live E2E (recipe in memory: OpenSandbox + host backend): a workflow with a test command, through implement, a failing test, the agent fix, approval, then `completed`. Record the result in the PR.
- [x] Commit `docs: spec kit verification gate`.

## Review fixes (applied)

- **Stale evidence:** linked runs still in `review` are superseded (→ `rejected`) as soon as `implement` re-runs (`run_phase`, which covers *Request changes*), in the same commit. Starting a new linked run supersedes them as well.
- `request_changes` locks the phase row (`FOR UPDATE`), the same lock `approve_verification_run` takes, so approve and request-changes serialise.
- `GET /verification/runs?spec_workflow_id=` filters server-side. The SpecPanel no longer scans the 50 newest runs.
- Audit and notification after an approval are best effort (logged). Any failure to start verification after `implement` notifies the human.

## Out of scope

- Creating a `WorkspaceWorktree` row or passing `worktree_id` for spec worktrees (they are still adopted later by `_reconcile`, as today).
- Screenshot rendering, a Stop button, the lost-queue-job reaper (the Plan 2 known gaps).
- Opening a PR from an approved workflow (spec §3 "PR → Merge"): next plan.
