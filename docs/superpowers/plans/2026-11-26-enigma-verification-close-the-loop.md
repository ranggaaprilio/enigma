# Enigma — Verifiable Agent Loop: Close the Loop (Plan 2)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Steps use checkbox (`- [ ]`) syntax for tracking — tick them as they land.

**Goal:** Make the *Iterate* step real. When a test fails and iterations remain, an agent fixes the code using the failure evidence and the loop re-tests, until the tests pass or the budget is spent. The run then parks at the human gate. Verification runs execute asynchronously and survive restarts in both `inline` and `queue` mode.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §3 (loop), §6 (errors).
**Previous plan:** `2026-11-26-enigma-verifiable-agent-loop.md` (Tasks 1–5) plus Phase 6 (PR #21).

## Why (findings from the current code)

- `orchestrator.py:140`: *Iterate* only re-runs the test command. No agent fixes anything between attempts.
- `POST /verification/runs` drives the whole loop inside the HTTP request. With an agent fix step a run can take tens of minutes.
- `PlanCriteria` lives only in memory. A run cannot be resumed after a restart, and a worker cannot pick it up.
- `reconciliation.py` does not cover `verification_runs`. If the process dies mid-loop, the run stays `testing` forever, and every new run in that workspace gets a 409.
- Test evidence stores `{command, exit_code}` only. The agent (and the human) never see the failure output, although `ExecResult.stdout` has it.

## Design

**A resumable state machine instead of one long coroutine.** `advance(run_id)` performs exactly one step, chosen from the persisted `status` and `iteration`, and returns `DONE` or `WAIT`:

```
planning ─► testing ─┬─ passed ─────────────────────────► observing ─► review
                     ├─ failed/error, iteration < max ─► fixing ─(agent run terminal)─► testing
                     └─ failed/error, budget spent ─────────────────────► observing ─► review
```

- `fixing` creates **one** agent run. Its prompt holds `plan_ref`, the test command and the tail of the failing output, and it is attributed to `created_by`. The run id is stored on the verification run. While that agent run is active, `advance` returns `WAIT`. When it reaches a terminal status, a `fix` evidence `{agent_run_id, agent_status}` is recorded, `iteration` is incremented, and the loop goes back to `testing`.
- **Inline mode:** an `asyncio` task registered in `run_registry` (so the shutdown drain covers it, as with Spec Kit finalizers) loops `advance`, sleeping `VERIFICATION_POLL_SECONDS` on `WAIT`.
- **Queue mode:** an arq job `run_verification_job` calls `advance` and raises `Retry(defer=VERIFICATION_POLL_SECONDS)` on `WAIT`. A worker slot is never held while an agent run waits for a slot of its own, which avoids a deadlock with a small `max_jobs`. This mirrors `finalize_spec_phase_job`.
- Every transition stays a conditional UPDATE. A step that loses the race writes nothing, so a redelivered job or a duplicate task is harmless.

**Failure handling (never wedge a workspace):**

- **Agent refused** (`AgentRunAlreadyActiveError`, budget, `RunCapacityExceededError`, `AgentConfigurationError`, `lifecycle.draining`): record `fix` evidence with status `error` and the reason, skip further fixing, and continue to `observing` → `review`. The human sees why it did not converge.
- **Agent run ends `error`/`stopped`:** record the evidence and re-test anyway (the agent may have partially fixed things). The iteration still counts.
- **Unexpected exception:** the run goes to `error` (the existing crash-safety path).
- **Process death, inline mode:** a startup sweep moves active verification runs to `error`, mirroring `reconcile_interrupted_runs`.
- **Process death, queue mode:** arq redelivers the job and `advance` resumes from the persisted state.

## Global Constraints

- Async-only DB access. No module-level in-flight state (`tests/test_run_runtime.py`). Use `run_registry`.
- Migration `0041` must be **expand-only** (`tests/test_migration_safety.py`). Widening the `status`/`kind` CHECK constraints means drop + recreate. If the guard flags it, justify it with `# contract:` and say why widening is safe.
- Agent runs go through `AgentService.create_run` only (budget, caps, one-active guard, request id), never around it.
- Errors surfaced through the API stay generic: no provider URLs, paths or credentials.

## Review Focus

- **Resumability:** `advance` must be idempotent per state. Replaying it on any persisted state never creates a second agent run for the same iteration. Pin this with a test that calls `advance` twice in `fixing`.
- **Queue deadlock:** the queue job never awaits an agent run inline. Pin this by asserting `Retry` is raised while the agent run is active.
- **Wedge-freedom:** every refusal or exception path ends in `review` or `error`, never in an active status. Pin each refusal type.
- **Output tail is bounded** (8 KiB). It is stored in JSONB and shown to humans. It may contain secrets printed by tests, so it is no more exposed than the terminal already is.

---

### Task 1: Persist criteria, author and loop state (migration `0041`)

**Files:** `backend/app/models/verification.py`, `backend/alembic/versions/0041_verification_loop_state.py`, `backend/tests/test_verification_models.py`

- [x] **Step 1:** Write failing model tests for the new columns: `criteria` JSONB (not null, default `{}`), `created_by` (FK `users`, nullable, `ON DELETE SET NULL`), `iteration` int (default 0), `agent_run_id` (FK `agent_runs`, nullable, `ON DELETE SET NULL`). Also test that status `fixing` and evidence kind `fix` are accepted.
- [x] **Step 2:** Run them to verify they fail.
- [x] **Step 3:** Implement the model changes and migration `0041` (expand-only, `down_revision="0040_verification_evidence"`).
- [x] **Step 4:** `alembic upgrade head` + downgrade/upgrade round-trip; the model tests and `test_migration_safety.py` pass.
- [x] **Step 5:** Commit `feat(verification): persist loop criteria and state`.

### Task 2: Test evidence keeps the failure output

**Files:** `backend/app/services/verification/test_runner.py`, `backend/tests/test_verification_test_runner.py`

- [x] **Step 1:** Write failing tests: `detail.output` holds the last 8 KiB of `stdout` (with a `truncated` flag), and it is present for both `passed` and `failed`.
- [x] **Step 2–4:** Red → implement → green.
- [x] **Step 5:** Commit `feat(verification): keep test output tail as evidence`.

### Task 3: Resumable `advance()` state machine (no agent yet)

**Files:** `backend/app/services/verification/orchestrator.py`, `backend/tests/test_verification_orchestrator.py`

- [x] **Step 1:** Write failing tests for `advance` driving `planning → testing → observing → review` step by step from persisted `criteria`. Cover idempotency (replay on each state), `max_iterations` re-test without an agent (current behaviour preserved), and crash → `error`.
- [x] **Step 2–4:** Red → implement `advance` + `drive_inline(run_id)` (loop with poll sleep) → green. `run()` becomes a thin wrapper, so the existing tests stay green.
- [x] **Step 5:** Commit `refactor(verification): drive the loop as a resumable state machine`.

### Task 4: Agent-driven fix step

**Files:** `backend/app/services/verification/fixer.py` (new: prompt builder + agent-run handoff), `orchestrator.py`, `backend/tests/test_verification_fixer.py`

- [x] **Step 1:** Write failing tests for:
  - a failed test with iterations left → `fixing`, one agent run created with the prompt (plan_ref, command, output tail) and attributed to `created_by`
  - `WAIT` while that agent run is active
  - on completion: `fix` evidence + `iteration + 1` + back to `testing`
  - a replay in `fixing` creates no second agent run
  - each refusal type → `fix` evidence `error` → `review`
  - an agent `error`/`stopped` → re-test
- [x] **Step 2–4:** Red → implement → green. Use `Settings(agent_allow_mock_provider=True)` in tests (AGENTS.md gotcha 13).
- [x] **Step 5:** Commit `feat(verification): let an agent fix failing tests between iterations`.

### Task 5: Asynchronous execution in inline and queue mode

**Files:** `backend/app/api/verification.py`, `backend/app/services/jobs.py`, `backend/app/worker.py`, `backend/app/core/config.py` (`VERIFICATION_POLL_SECONDS`), `backend/tests/test_verification_api.py`, `backend/tests/test_verification_jobs.py`

- [x] **Step 1:** Write failing tests:
  - `POST /runs` persists `criteria` + `created_by` and returns **202** with the run at `planning`; the loop runs afterwards
  - inline: the task is registered in `run_registry`
  - queue: `enqueue_verification_run` is called; `run_verification_job` raises `Retry` on `WAIT` and returns on `DONE`
  - 409 is still raised for a second active run, now checked at create time
  - 503 while draining
- [x] **Step 2–4:** Red → implement → green.
- [x] **Step 5:** Commit `feat(verification): run verification asynchronously`.

### Task 6: Startup reconciliation for inline verification runs

**Files:** `backend/app/services/reconciliation.py`, `backend/tests/test_reconciliation.py`

- [x] **Step 1:** Write a failing test: in inline mode, startup moves `planning`/`testing`/`fixing`/`observing` runs to `error`. Queue mode skips the sweep (the API does not own the runs).
- [x] **Step 2–4:** Red → implement → green.
- [x] **Step 5:** Commit `fix(verification): end interrupted runs on startup`.

### Task 7: Frontend — live progress and fix evidence

**Files:** `frontend/src/lib/api.ts`, `frontend/src/components/VerificationPanel.tsx`, `VerificationPanel.test.tsx`

- [x] **Step 1:** Write failing tests:
  - create returns `planning`, and the panel polls `GET /runs/{id}` while the status is active (fake timers), stopping at `review`
  - a `fixing` pill
  - a `fix` evidence card showing the agent run status, linked to the Agent view
  - the test output tail in a collapsible `<pre>`
- [x] **Step 2–4:** Red → implement → green (`tsc -b`, `eslint`, `vitest`, `vite build`).
- [x] **Step 5:** Commit `feat(verification): show live progress and fix evidence`.

### Task 8: Docs

- [x] Add an AGENTS.md gotcha (verification runs: resumable `advance`, never await an agent run inside a queue job, `0041`), and tick this plan's checkboxes.
- [x] Commit `docs: document the verification fix loop`.

## Known gaps (from review, not fixed here)

- **Orphaned fix agent run on a crash between steps.** `AgentService.create_run` commits on its own session, so storing `agent_run_id` cannot be atomic with it. If the process dies between the two, the agent run keeps going unlinked. The replay is then refused ("already has an active agent run"), and the run goes to the gate with that reason. Closing this needs `create_run` to accept the caller's session or a link column on `agent_runs`.
- **Lost queue job.** If the arq job disappears (for example, Redis is flushed), a queue-mode run stays active. The agent-run reaper has no verification counterpart yet.

## Out of scope (later plans)

- Worktree targeting: the fix agent and the tests run in the workspace's base checkout, as the tests already do.
- Stopping a verification run from the UI.
- Spec Kit integration (fire after `implement`, gate in the SpecPanel).
- Serving screenshot images.
- Docker → OpenSandbox cutover.
