# Enigma — Verify an Ordinary Agent Run (Plan 9)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** The self-test loop (test → observe → fix → human gate) works for any agent run, not only Spec Kit's `implement`. A user finishes an agent run and gets proof that it works, on the same branch the agent worked on.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §3 (the core loop is the product, not a Spec Kit feature).

## Current state (verified, 2026-11-28)

- **A verification run can be started by hand.** `POST /workspaces/{id}/verification/runs` (`api/verification.py`, `VerificationPanel.tsx`) takes a typed `test_command`, `observe_port`, `cwd` and `max_iterations`. It knows nothing about which agent run produced the code.
- **Spec Kit links its runs.** `verification_runs.spec_workflow_id` links a run to its workflow. `spec_link.criteria_for_workflow` derives criteria from the workflow's own `verification`, else the workspace's first enabled `role="test"` custom command, with cwd = worktree + `app_root` + command cwd. Review runs are superseded when the code changes.
- **The fixer ignores the agent's branch.** `AgentFixer.start` (`verification/fixer.py:118`) calls `AgentService.create_run(workspace, created_by, prompt, working_dir=cwd)`: no `worktree_id` and no `session_id`. For a run made in a worktree, the fix agent targets the base checkout's branch. It also starts a new conversation, so it does not see what the original agent did.
- **Agent runs.** `agent_runs` has `worktree_id` and `session_id`. A run ends `completed | error | stopped` (`AgentService`, `TERMINAL_RUN_STATUSES`). The fix runs a verification starts are recorded as `verification_runs.agent_run_id`.

## Design

### 1. A verification run can name the agent run it verifies

- **Migration `0045_verification_source_agent_run`** (expand-only): `verification_runs.source_agent_run_id` (FK `agent_runs`, `ON DELETE SET NULL`, indexed).
- **`POST /workspaces/{id}/agent/runs/{run_id}/verify`** (owner/driver of the workspace, sandbox rate limit):
  - **Refusals:** 409 when the run is not `completed`; 409 when it is itself a fix run (some verification run's `agent_run_id`), so a fix is never verified on its own.
  - **Criteria:** from the body when given (`test_command`, `observe_port`, `max_iterations`). Otherwise from the run's own context, the same rule Spec Kit uses (`spec_link` is generalised, not copied): the workspace's first enabled `role="test"` command, with cwd = the run's worktree path (or the base checkout) + `app_root` + command cwd. With neither, 422 with an actionable message ("add a test command with role 'test', or give one").
  - **Linking:** `plan_ref` is the run's prompt (truncated), `source_agent_run_id` is the run, and `created_by` is the caller.
  - **Supersede:** a newer verification of the same run supersedes that run's earlier ones still in `review` (→ `rejected`), as Spec Kit does.
  - **Response:** 202 with the run. It then executes like any verification run (inline task or queue job).
- `GET /agent/runs/{id}` and the run list carry `verification`: the latest linked run's `id` and `status`, or null.

### 2. The fix lands on the agent's branch, in its conversation

For a run with a source agent run, `AgentFixer.start` passes the source run's `worktree_id` and `session_id` to `create_run`. The fix is then a new turn of the same session, on the same worktree branch. Runs without a source (manual, Spec Kit) are unchanged.

### 3. Auto-verify (opt-in, per workspace)

- `workspaces.auto_verify_agent_runs` (bool, default false), in the same migration. It is set through `PATCH /workspaces/{id}`.
- When an agent run ends `completed` and auto-verify is on, the run's finaliser starts a verification exactly as the endpoint would, **only if criteria exist**. It never starts one for a fix run, a Spec Kit phase run (`spec_phases.agent_run_id`), or a run that already has one. A refusal (no criteria, budget, caps) is logged, never fatal to the run.
- It works in queue mode too: the finaliser runs in the worker and uses the same `start_verification_run` path.

### 4. UI

- **AgentTab (run history and thread):**
  - A completed run shows **Verify**. It opens a small dialog prefilled with the derived test command, editable, plus an optional port.
  - A verified run shows a status chip (testing / fixing / review / approved / rejected / error) that opens the evidence in the existing VerificationPanel view.
- **Workspace settings:** an "Auto-verify agent runs" toggle, with a hint when no `role="test"` command exists.

## Non-goals

- New evidence kinds, or changes to the orchestrator's state machine.
- Verifying runs that ended `error` or `stopped` (the agent did not finish).
- Spec Kit behaviour: unchanged. Its runs keep `spec_workflow_id` and its own gate.

## Tasks

### Task 1: Link and start (backend, TDD)

**Files:** migration `0045`, `models/verification.py`, `models/workspace.py`, `services/verification/spec_link.py` (generalised criteria), `api/agents.py` (or `api/verification.py`), `schemas/agents.py`, tests.

- [x] Failing tests:
  - `/verify` on a completed run creates a linked run with derived criteria (base checkout and worktree cwd);
  - it refuses a non-completed run and a fix run (409) and a run with no criteria (422);
  - a newer verify supersedes the previous run in review;
  - the run detail and the list carry `verification`;
  - migration safety stays expand-only.
- [x] Implement.
- **As built (deviations from the design above):**
  - The route is `POST /workspaces/{id}/verification/agent-runs/{run_id}`, in the verification router, which already has the session and gate patterns.
  - Instead of a `verification` field on agent run responses (which would change the agent service protocol and every fake), `VerificationRunResponse` carries `source_agent_run_id` and `GET /verification/runs` filters by it.
  - The migration is `0045_verify_agent_runs`: Alembic's `alembic_version` column is `varchar(32)`, so the longer id failed.
  - The `role="test"` lookup moved to `spec_link.test_command_criteria`, now shared by Spec Kit and agent runs.

### Task 2: Fix on the agent's branch and session (TDD)

**Files:** `verification/fixer.py`, tests.

- [x] Failing tests:
  - a run with a source passes that run's `worktree_id` and `session_id`;
  - a run without one passes neither (Spec Kit and manual runs unchanged).
- [x] Implement.

### Task 3: Auto-verify (TDD)

**Files:** `services/agents.py` (finaliser hook), `schemas/workspaces.py`, `api/workspaces.py`, tests.

- [ ] Failing tests:
  - on: a completed run with criteria starts one linked verification, inline and in queue mode;
  - off: none;
  - never for a fix run, a Spec Kit phase run, a run with no criteria or a failed/stopped run;
  - a refusal never changes the agent run's status.
- [ ] Implement.

### Task 4: Frontend

**Files:** `lib/api.ts`, `components/AgentTab/*`, `VerificationPanel.tsx` (open a run by id), workspace settings, tests.

- [ ] Failing tests:
  - Verify shows only on completed, non-fix runs and posts the edited criteria;
  - the chip shows each status and opens the evidence;
  - the auto-verify toggle saves, and hints when no test command exists.
- [ ] Implement.

### Task 5: Docs and live E2E

- [ ] AGENTS.md (a gotcha for the link and the fixer rule), plan ticks.
- [ ] Live E2E on the compose OpenSandbox stack (separate `-p` project):
  - an agent run in a worktree introduces a bug;
  - **Verify** goes red → the fix agent runs **on the same worktree branch and session** → green → review → approve;
  - auto-verify starts one on the next run.

  It uses real LLM calls, so it needs confirmation before running.

## Exit criteria

- Any completed agent run can be verified from its own context, and a fix never lands outside the agent's worktree.
- Auto-verify is opt-in, never loops on fix runs, and never fails a run.
- Suites green (known unrelated failures only); the live E2E passes.
