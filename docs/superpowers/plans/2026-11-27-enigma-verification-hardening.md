# Enigma — Verifiable Agent Loop: Hardening Verification Runs (Plan 5)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** A verification run can never leave a user stuck. A human can **stop** an active run. A run whose queue job vanished is **reaped**. A run that ends while its fix agent is working **takes the agent run down with it**, so nothing keeps editing the worktree or holding the workspace's single agent slot.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §6 ("Gate manusia batal/menolak → `rejected`; orchestrator berhenti").
**Previous plans:** Plan 2 (`close-the-loop`) known gaps; Plans 3–4.

## Current state (verified in code)

- **Lifecycle:** `ACTIVE_STATUSES = planning, testing, fixing, observing` (`services/verification/orchestrator.py`). Every write is a conditional `_transition`. `advance()` returns `DONE` once the row is no longer active, so inline `drive()` and the queue job both stop on their own the next time they read the row.
- **No stop:** `api/verification.py` has trigger/list/detail/approve/reject only.
- **No reaper for verification runs.** Queue mode enqueues `run_verification_job` with the deterministic id `verification_run:<id>` (`services/jobs.py`), and the job keeps itself alive with `Retry(defer=…)`.
  - If Redis is flushed, or arq drops the job, the run stays active forever and blocks every new verification run in the workspace (the one-active guard).
  - `services/reaper.py` handles only agent and command runs. `job_is_known` already answers "is this job still in arq?".
- **Orphan fix agent runs:**
  - `orchestrator.fail()` and `advance()`'s crash path move the run to `error` without looking at `agent_run_id`, so an in-flight fix agent run keeps running.
  - `_step_fix` persists `agent_run_id` with `WHERE agent_run_id IS NULL` only. A run stopped or failed between `fixer.start()` and that write leaves an agent run nobody tracks.
- **Inline task:** inline runs live in `run_registry` (`execution.start_verification_run`, keyed by run id), so `run_registry.get_task(run_id)` can cancel one.

## Design

**End-of-run hook (one place).** `VerificationOrchestrator.end(run_id, status, from_statuses) -> bool` replaces the bare `_transition` wherever a run is ended from outside the step: `fail`, the crash path, stop and the reaper.
- In one transaction it locks the row (`SELECT … FOR UPDATE`), reads `status` and `agent_run_id`, and does the conditional update.
- After the commit, if the run was `fixing` with an `agent_run_id`, it stops that agent run through `Fixer.stop(run)`. `AgentFixer.stop` calls `AgentService.stop_run(workspace_id, agent_run_id, run.created_by)`: idempotent, best effort and logged.

**Stop:** `POST /workspaces/{id}/verification/runs/{run_id}/stop`.
- **Permissions:** the same workspace scoping as approve/reject (404 outside the workspace). Linked Spec Kit runs are owner-only (403), like their gate.
- **Effect:** `end(run_id, "rejected", ACTIVE_STATUSES)`. Spec §6 maps a human cancel to `rejected`, so no new status and no constraint change are needed. Returns the run (200), or 409 when it is not active.
- **Inline:** when the run was `testing`/`observing` at stop time (read under the row lock), the registry task is cancelled, which also cancels the in-flight sandbox command. A `fixing` task is not cancelled; it exits on its next poll, and the hook stops its agent.
- **Queue:** the job exits on its next `advance` (≤ `VERIFICATION_POLL_SECONDS`). An in-flight test command finishes, but its writes are conditional and change nothing.
- **Linked runs:** `implement` stays `awaiting_review`, and the human can *Re-run verification* or *Request changes* (as after a reject).
- **Audit:** `verification_run_stopped`.

**Fix-start race:** `_step_fix` persists `agent_run_id` only `WHERE status = 'fixing' AND agent_run_id IS NULL`. If that matches nothing (the run was ended meanwhile), it stops the agent run it just created.

**Reaper (queue mode):** `reap_lost_verification_jobs(session_maker, settings, redis)` in the same `run_reaper` pass as agent/command lost jobs.
- **Candidates:** active runs whose `updated_at` is older than `RUN_UNCLAIMED_GRACE_SECONDS`, which covers the create→enqueue window.
- **Lost:** `job_is_known(..., verification_run_job_id(id))` is false. These are ended via `end(…, "error", ACTIVE_STATUSES)`, which also stops a fix agent run.
- **Redis errors:** a Redis error skips the run (never treated as a loss), as for agent runs.
- **Metric:** `reaper_lost_jobs_total{kind="verification"}` (a bounded label).

**Frontend:** `VerificationRunView` (shared by the Verify panel and the SpecPanel) shows **Stop** while the run is active. On success it re-fetches, and a 409 shows the server's message.

## Global Constraints

- No migration: `rejected` and `error` already exist.
- Async-only, no module-level state, conditional writes. The row lock is a transaction-scoped `FOR UPDATE`, which is PgBouncer-safe.
- Fix agent runs are stopped through `AgentService.stop_run`, never by writing the agent row directly, so the events, the lease and the `stopped` control message stay consistent.

## Review Focus

- **No orphan:** every path that ends a run while `fixing` (stop, fail, crash, reaper, fix-start race) stops the agent run exactly once and never stops an unrelated run.
- **No false reaping:** a run whose job is queued, deferred or in progress is never reaped, and neither is one inside the grace window.
- **Authorization:** stop is workspace-scoped, and linked runs are owner-only.

---

### Task 1: One end-of-run hook that stops the fix agent

**Files:** `services/verification/orchestrator.py`, `services/verification/fixer.py`, `tests/test_verification_orchestrator*.py`.

- [x] Write failing tests:
  - `end()` on a `fixing` run with an `agent_run_id` → run ended, `Fixer.stop` called once with that run.
  - `end()` on `testing` → no stop.
  - `end()` on a terminal run → `False`, no stop.
  - `fail()` and a crashing step now go through it (a fake fixer records stops).
  - Fix-start race: a run ended between `start()` and the id write → the new agent run is stopped and `agent_run_id` stays null.
  - `AgentFixer.stop` → `AgentService.stop_run(workspace, agent_run_id, created_by)`. Errors are swallowed and logged.
- [x] Implement, green.

### Task 2: Stop endpoint

**Files:** `api/verification.py`, `services/verification/execution.py` (inline cancel helper), `tests/test_verification_api.py`.

- [x] Write failing tests:
  - active → 200, `rejected`, audit `verification_run_stopped`
  - review/terminal → 409
  - another workspace's run → 404
  - linked run by a non-owner → 403
  - inline `testing` run → its registry task is cancelled; `fixing` → not cancelled
- [x] Implement, green.

### Task 3: Reap verification runs whose queue job is lost

**Files:** `services/reaper.py`, `core/metrics.py` (label value only, if needed), `tests/test_reaper*.py`.

- [x] Write failing tests:
  - a stale active run with an unknown job → `error`, its fix agent stopped
  - a known job → untouched
  - inside the grace window → untouched
  - Redis error → skipped
  - a terminal run → untouched
  - `run_reaper` includes it when given `redis`
- [x] Implement, green.

### Task 4: Frontend Stop

**Files:** `lib/api.ts` (`stopVerificationRun`), `components/VerificationPanel.tsx`, tests.

- [x] Write failing tests:
  - Stop shown only for active statuses
  - clicking it calls the API and refreshes
  - a 409 shows the message
  - the SpecPanel's implement view shows Stop for an active linked run
- [x] Implement, green (`tsc -b`, `eslint`, `vitest`, `vite build`).

### Task 5: Docs

- [x] AGENTS.md note 30 (stop → `rejected`; end hook stops the fix agent; verification lost-job reaper), and tick this plan.

## Review fixes (applied)

- **The fix agent stop needs no user.** `AgentFixer.stop` now calls the new `AgentService.stop_active_run(run_id)`. This is the permission-free core of `stop_run`, which now delegates to it. A fix agent is stopped even when the verification run's author has lost drive access or been deleted (`created_by` → NULL). Before this fix, the stop failed and was only logged.
- **Stop is rate-limited** with `SANDBOX_RATE_LIMIT_DEP`, like the other sandbox actions.
- **Known and documented:**
  - In queue mode, a test or observation step that is already running finishes after a stop. Its evidence is appended, but its status write is a no-op.
  - No test drives `end()` concurrently with a step. The reviewers checked the lock order by reading the code (one row lock, no cycle with the fix advisory lock or the `spec_phases` lock).

## Out of scope

- Killing an in-flight test command in queue mode (it finishes or times out; its writes are no-ops).
- Screenshot capture/rendering (next plan), the OpenSandbox production hardening (§4.3).
