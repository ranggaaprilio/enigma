# Agent Workspace: Dashboard Tab with Changed-File Tracking

## TL;DR

> **Quick Summary**: Replace the right sidebar agent panel with a full Agent dashboard tab that shows live process, current tool, run history, and files changed per run. Extend the backend event contract with `tool_call_id` column and `AgentFileChange` model for per-run file tracking.
>
> **Deliverables**:
> - AgentFileChange DB model + Alembic migration
> - `tool_call_id` nullable column on agent_events
> - Mock provider updated with tool_call_id
> - Baseline capture + file change computation in agent service
> - `/workspaces/{id}/agent/runs/{id}/file-changes` API endpoint
> - Right sidebar removed from DashboardShell
> - New Agent tab in DashboardRoute
> - Prompt composer, tool cards, run history, changed files list
> - WebSocket reconnection with exponential backoff
>
> **Estimated Effort**: Large
> **Parallel Execution**: YES - 4 waves
> **Critical Path**: Task 1 → Task 5 → Task 9 → Task 10 → Task 17

---

## Context

### Original Request
Replace the narrow right agent panel with a dedicated Agent dashboard tab. New view must show: live process, current tool, failures, run history, files changed by selected run. Extend event contract with tool_call_id and changed_files metadata.

### Interview Summary
**Key Discussions**:
- Changed files UX: Flat list with status badges (created/modified/deleted/renamed), no diff viewer
- Tool cards: Expandable cards with summary, click to reveal details
- History: Recent 20 runs per workspace, "load more" pagination
- Left sidebar "Agents" nav: Wire to activate new Agent tab
- File tracking: New AgentFileChange DB model with baseline before run, diff after
- Test strategy: TDD — RED → GREEN → REFACTOR
- tool_call_id: Nullable column on agent_events (not just JSONB)
- WebSocket: Add onclose reconnect with max 3 retries, no heartbeat/sequence tracking

**Research Findings**:
- DashboardShell.tsx: 3-column CSS grid (left sidebar 240/72px + main + right sidebar 320/72px). AgentPanel lines 399-681.
- DashboardRoute.tsx: 8 tabs, no "agent" tab. State persisted in localStorage.
- No shared WebSocket hook, no AgentContext — all state local to AgentPanel
- AgentRun/AgentEvent models exist. tool_call_id in OpenHands JSONB only.
- RepositoryService has GitFileStatus/GitStatus but not connected to agent runs
- WebSocket has no onclose handler at all currently
- Only one agent run per workspace at a time

### Metis Review
**Identified Gaps** (addressed):
- No-git workspace handling: Return empty file changes, no crash → Addressed in task scope
- tool_call_id as DB column (not just JSONB) → Addressed, nullable column on agent_events
- AgentFileChange needs FK to AgentRun AND optional tool_call_id → Addressed in model spec
- Mock provider must include tool_call_id → Addressed, explicit task
- WebSocket reconnection needed → Addressed, max 3 retries with backoff
- Empty state for Agent tab without workspace → Addressed, explicit task
- ProviderEvent dataclass must NOT be modified → Added as guardrail
- Cap file change display at 50 items with "N more" summary → Addressed in task

---

## Work Objectives

### Core Objective
Replace the right sidebar AgentPanel with a full Agent dashboard tab that provides comprehensive run visibility — live current-tool highlighting, expandable tool cards with paired tool_call/tool_result, run history with pagination, per-run changed-file tracking with status badges, and proper WebSocket reconnection.

### Concrete Deliverables
- `backend/app/models/agent.py` — AgentFileChange model added
- `backend/alembic/versions/` — Migration adding `agent_file_changes` table and `tool_call_id` column on `agent_events`
- `backend/app/schemas/agents.py` — AgentFileChangeResponse schema
- `backend/app/services/agents.py` — Baseline capture, file change computation, AgentFileChange CRUD
- `backend/app/services/agent_providers.py` — Mock provider updated with tool_call_id
- `backend/app/services/agent_tools.py` — File write tracking hooks
- `backend/app/api/agents.py` — `GET /workspaces/{id}/agent/runs/{id}/file-changes` endpoint
- `frontend/src/components/AgentTab.tsx` — New component
- `frontend/src/components/AgentTab/` — Sub-components (PromptComposer, ToolCard, RunHistory, ChangedFiles)
- `frontend/src/routes/DashboardRoute.tsx` — Agent tab added
- `frontend/src/components/DashboardShell.tsx` — Right sidebar removed, 2-column grid
- `frontend/src/lib/api.ts` — New API function for file changes

### Definition of Done
- [ ] `uv run pytest` — all backend tests pass including new model, service, and API tests
- [ ] `uv run ruff check .` — no new lint errors
- [ ] `pnpm test` — all frontend tests pass including new component tests
- [ ] `pnpm lint` — exit code 0
- [ ] `pnpm build` — no type errors
- [ ] Agent tab renders with prompt composer, run history, tool cards, and changed files
- [ ] Right sidebar completely removed from DashboardShell
- [ ] WebSocket reconnects on disconnect (max 3 retries)
- [ ] File changes API returns per-run changes with status badges

### Must Have
- AgentFileChange model with path, status, tool_call_id columns
- `tool_call_id` nullable column on `agent_events` table
- Mock provider includes `tool_call_id` in event metadata
- Baseline capture before run starts, diff after run completes
- Graceful handling of workspaces without git repo (empty file changes)
- Right sidebar removed from DashboardShell, simplified to 2-column grid
- Agent tab added to DashboardRoute with all 8+1 tabs
- Prompt composer with Run/Stop controls
- Expandable tool cards with paired tool_call/tool_result
- Changed files list with status badges (created/modified/deleted/renamed)
- Run history (20 per page) with "Load more" pagination
- WebSocket reconnection with exponential backoff, max 3 retries
- Empty state when no workspace is active
- "Agents" sidebar nav item activates Agent tab
- Stream disconnect errors shown separately from agent/tool errors
- Final status refresh from REST API after WebSocket closes on completed run

### Must NOT Have (Guardrails)
- NO diff viewer or file content preview
- NO shared WebSocket hook extraction (stay local to Agent tab)
- NO AgentContext or useAgent provider
- NO SSE alternative to WebSocket
- NO search/filter/sort on run history
- NO pagination on events (only runs get pagination)
- NO modification of ProviderEvent dataclass fields (add to metadata dict only)
- NO refactoring of 3-column grid beyond removing the right column
- NO file change streaming mid-run (capture at end only)
- NO copy-to-clipboard buttons on tool card content
- NO syntax highlighting in tool card output
- NO AgentFileChange with diff content or line counts (path + status only)

---

## Verification Strategy (MANDATORY)

> **ZERO HUMAN INTERVENTION** - ALL verification is agent-executed. No exceptions.

### Test Decision
- **Infrastructure exists**: YES
- **Automated tests**: YES (TDD)
- **Framework**: Backend: pytest (uv run pytest), Frontend: Vitest (pnpm test)
- **If TDD**: Each task follows RED (failing test) → GREEN (minimal impl) → REFACTOR

### QA Policy
Every task MUST include agent-executed QA scenarios.
Evidence saved to `.omo/evidence/task-{N}-{scenario-slug}.{ext}`.

- **Frontend/UI**: Use Bash — verify component renders, check DOM structure via test output
- **Backend/API**: Use Bash (curl) — Send requests, assert status + response fields
- **Database**: Use Bash — Run migrations, query schema

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Start Immediately - backend models + mocks):
├── Task 1: AgentFileChange model + CRUD tests (TDD) [deep]
├── Task 2: tool_call_id column on agent_events + migration [quick]
├── Task 3: Mock provider tool_call_id update + tests [quick]
└── Task 4: Baseline capture + file change computation service (TDD) [deep]

Wave 2 (After Wave 1 - API endpoints + frontend scaffolding):
├── Task 5: file-changes API endpoint (depends: 1) [unspecified-high]
├── Task 6: Agent file-changes schema + API tests (depends: 1, 5) [quick]
├── Task 7: Extract AgentPanel + remove right sidebar (depends: nothing) [visual-engineering]
├── Task 8: Agent tab scaffolding + empty state (depends: 7) [visual-engineering]
└── Task 9: api.ts file-changes function + types (depends: 5) [quick]

Wave 3 (After Wave 2 - frontend features):
├── Task 10: Prompt composer + Run/Stop controls (depends: 8, 9) [visual-engineering]
├── Task 11: Tool cards with paired tool_call/tool_result (depends: 8) [visual-engineering]
├── Task 12: Changed files list with status badges (depends: 8, 9, 5) [visual-engineering]
├── Task 13: Run history + pagination (depends: 8, 9) [visual-engineering]
└── Task 14: WebSocket reconnection + disconnect handling (depends: 8) [deep]

Wave 4 (After Wave 3 - integration + polish):
├── Task 15: Sidebar "Agents" nav wiring (depends: 8) [quick]
├── Task 16: Final WebSocket status sync (refresh after close) (depends: 14) [deep]
└── Task 17: Integration tests end-to-end (depends: all) [unspecified-high]

Wave FINAL (After ALL tasks — 4 parallel reviews):
├── Task F1: Plan compliance audit (oracle)
├── Task F2: Code quality review (unspecified-high)
├── Task F3: Real manual QA (unspecified-high)
└── Task F4: Scope fidelity check (deep)
→ Present results → Get explicit user okay

Critical Path: Task 1 → Task 5 → Task 9 → Task 10 → Task 17
Parallel Speedup: ~60% faster than sequential
Max Concurrent: 4 (Wave 1)
```

### Dependency Matrix

| Task | Blocked By | Blocks | Wave |
|------|-----------|--------|------|
| 1 | - | 5, 6 | 1 |
| 2 | - | 4 | 1 |
| 3 | - | 14 | 1 |
| 4 | 2 | 16 | 1 |
| 5 | 1 | 9, 12 | 2 |
| 6 | 1, 5 | 12 | 2 |
| 7 | - | 8 | 2 |
| 8 | 7 | 10, 11, 12, 13, 14, 15 | 2 |
| 9 | 5 | 10, 12, 13 | 2 |
| 10 | 8, 9 | 17 | 3 |
| 11 | 8 | 17 | 3 |
| 12 | 8, 9, 5 | 17 | 3 |
| 13 | 8, 9 | 17 | 3 |
| 14 | 8, 3 | 16 | 3 |
| 15 | 8 | 17 | 4 |
| 16 | 4, 14 | 17 | 4 |
| 17 | all | F1-F4 | 4 |
| F1 | 17 | - | F |
| F2 | 17 | - | F |
| F3 | 17 | - | F |
| F4 | 17 | - | F |

### Agent Dispatch Summary

- **Wave 1**: 4 — T1 `deep`, T2 `quick`, T3 `quick`, T4 `deep`
- **Wave 2**: 5 — T5 `unspecified-high`, T6 `quick`, T7 `visual-engineering`, T8 `visual-engineering`, T9 `quick`
- **Wave 3**: 5 — T10 `visual-engineering`, T11 `visual-engineering`, T12 `visual-engineering`, T13 `visual-engineering`, T14 `deep`
- **Wave 4**: 3 — T15 `quick`, T16 `deep`, T17 `unspecified-high`
- **FINAL**: 4 — F1 `oracle`, F2 `unspecified-high`, F3 `unspecified-high`, F4 `deep`

---

## TODOs

- [x] 1. AgentFileChange model + CRUD tests (TDD)

  **What to do**:
  - Write failing tests first for AgentFileChange model creation, query by run_id, and deletion
  - Create `AgentFileChange` model in `backend/app/models/agent.py` with columns: `id` (UUID PK), `agent_run_id` (UUID FK to agent_runs), `path` (Text, not nullable), `status` (String: created/modified/deleted/renamed), `tool_call_id` (String, nullable), `created_at` (DateTime)
  - Add relationship on `AgentRun` model: `file_changes = relationship("AgentFileChange", back_populates="run", cascade="all, delete-orphan")`
  - Add relationship on `AgentFileChange`: `run = relationship("AgentRun", back_populates="file_changes")`
  - Add CRUD methods to `AgentService`: `create_file_change()`, `get_file_changes_for_run(run_id)`, `get_file_changes_for_run_and_tool(run_id, tool_call_id)`
  - Run tests: `RED` (tests fail) → `GREEN` (implement model) → `REFACTOR` (clean up)

  **Must NOT do**:
  - Do NOT add diff content or line count columns to AgentFileChange
  - Do NOT modify AgentRun or AgentEvent model fields (only add relationships)
  - Do NOT create a separate service file — add CRUD to existing AgentService

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: TDD cycle with model design, relationships, and multiple CRUD methods
  - **Skills**: []
    - No specialized skills needed for Python model work

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 2, 3)
  - **Blocks**: Tasks 5, 6
  - **Blocked By**: None (can start immediately)

  **References**:

  **Pattern References** (existing code to follow):
  - `backend/app/models/agent.py` — Follow existing AgentRun/AgentEvent model pattern (UUID PK, timestamps, FK relationships). Copy the same `__tablename__`, column types, and relationship style.
  - `backend/app/services/agents.py` — Follow existing CRUD pattern in AgentService (async session, select queries). Method signatures like `get_runs_for_workspace()` show the query style.

  **API/Type References** (contracts to implement against):
  - `backend/app/models/agent.py:AgentRun` — FK target for `agent_run_id`. Note: AgentRun already has `workspace_id` FK.
  - `backend/app/models/agent.py:AgentEvent` — `event_metadata` JSONB field where `tool_call_id` currently lives (will add as separate column in Task 2).

  **Test References** (testing patterns to follow):
  - `backend/tests/test_agents.py` — Existing test structure with async client, fixtures, assertions. Follow this pattern for new model tests.

  **WHY Each Reference Matters**:
  - `backend/app/models/agent.py` — Must match exact column types, naming conventions, and relationship patterns. Deviations cause migration issues.
  - `backend/app/services/agents.py` — Must follow same async query patterns. Different session handling styles will cause runtime errors.

  **Acceptance Criteria**:

  **If TDD (tests enabled):**
  - [ ] Test file: `backend/tests/test_agents.py` — new `TestAgentFileChange` class with tests for create, query by run_id, query by run_id + tool_call_id, delete cascade
  - [ ] `uv run pytest backend/tests/test_agents.py::TestAgentFileChange -v` → PASS (4+ tests, 0 failures)

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Create AgentFileChange and query by run_id
    Tool: Bash
    Preconditions: Migration applied, test database available
    Steps:
      1. Run `uv run pytest backend/tests/test_agents.py::TestAgentFileChange::test_create_file_change -v`
      2. Assert exit code 0
      3. Assert output contains "PASSED"
    Expected Result: Test passes, AgentFileChange row created and queryable
    Failure Indicators: Test fails, import error, or no test found
    Evidence: .omo/evidence/task-1-create-filechange.txt

  Scenario: No-git workspace returns empty file changes without crash
    Tool: Bash
    Preconditions: Test database available
    Steps:
      1. Run `uv run pytest backend/tests/test_agents.py::TestAgentFileChange::test_empty_file_changes -v`
      2. Assert exit code 0
    Expected Result: Returns empty list, no crash
    Failure Indicators: Test fails or raises TypeError/AttributeError
    Evidence: .omo/evidence/task-1-empty-filechanges.txt
  ```

  **Commit**: YES
  - Message: `feat(backend): add AgentFileChange model + CRUD`
  - Files: `backend/app/models/agent.py`, `backend/app/services/agents.py`, `backend/tests/test_agents.py`
  - Pre-commit: `uv run pytest backend/tests/test_agents.py -v`

- [x] 2. Add tool_call_id column on agent_events + migration

  **What to do**:
  - Add `tool_call_id = Column(String, nullable=True)` to `AgentEvent` model in `backend/app/models/agent.py`
  - Create Alembic migration: `uv run alembic revision -m "add_tool_call_id_to_agent_events"`
  - Migration adds `tool_call_id` column as `VARCHAR NULL` to `agent_events` table
  - Add index on `tool_call_id` column for pairing queries
  - Verify migration: `uv run alembic upgrade head`
  - Verify rollback: `uv run alembic downgrade -1`

  **Must NOT do**:
  - Do NOT remove `tool_call_id` from `event_metadata` JSONB — backward compatibility
  - Do NOT make `tool_call_id` non-nullable — existing events won't have it
  - Do NOT modify `ProviderEvent` dataclass fields

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Single column addition + standard Alembic migration
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 3)
  - **Blocks**: Task 4
  - **Blocked By**: None (can start immediately)

  **References**:

  **Pattern References**:
  - `backend/alembic/versions/` — Follow existing migration naming and structure patterns. Check the most recent migration for style.
  - `backend/app/models/agent.py:AgentEvent` — Where the new column goes. Current model has `event_metadata` JSONB that informally holds `tool_call_id`.

  **Test References**:
  - `backend/tests/test_agents.py` — Add test that creates an AgentEvent with `tool_call_id` set and verifies it's queryable.

  **WHY Each Reference Matters**:
  - Alembic versions dir — Must match migration style (async engine, op.add_column, op.create_index). Wrong style = migration failure.
  - AgentEvent model — Must add column adjacent to existing columns, following the same pattern. Wrong placement causes confusion.

  **Acceptance Criteria**:

  - [ ] `tool_call_id` column exists on `agent_events` table as nullable VARCHAR
  - [ ] Index exists on `tool_call_id` column
  - [ ] `uv run alembic upgrade head` succeeds
  - [ ] `uv run alembic downgrade -1` succeeds
  - [ ] `uv run alembic upgrade head` succeeds (re-migrate)

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Migration adds tool_call_id column successfully
    Tool: Bash
    Preconditions: Database running, current migrations applied
    Steps:
      1. Run `cd backend && uv run alembic upgrade head`
      2. Assert exit code 0
      3. Run `uv run python -c "from app.models.agent import AgentEvent; print(hasattr(AgentEvent, 'tool_call_id'))"`
      4. Assert output contains "True"
    Expected Result: Column exists, model attribute accessible
    Failure Indicators: Migration fails or column not found
    Evidence: .omo/evidence/task-2-migration-success.txt

  Scenario: Migration rollback removes tool_call_id column
    Tool: Bash
    Preconditions: Migration applied
    Steps:
      1. Run `cd backend && uv run alembic downgrade -1`
      2. Assert exit code 0
      3. Run `uv run alembic upgrade head`
      4. Assert exit code 0
    Expected Result: Rollback succeeds, re-migration succeeds
    Failure Indicators: Downgrade fails or re-migration fails
    Evidence: .omo/evidence/task-2-rollback-success.txt
  ```

  **Commit**: YES
  - Message: `feat(backend): add tool_call_id column to agent_events migration`
  - Files: `backend/app/models/agent.py`, `backend/alembic/versions/`
  - Pre-commit: `cd backend && uv run alembic upgrade head && uv run ruff check .`

- [x] 3. Mock provider tool_call_id update + tests

  **What to do**:
  - Update `MockAgentProvider` in `backend/app/services/agent_providers.py` to include `tool_call_id` in `metadata` dict for `tool_call` and `tool_result` event types
  - Generate `tool_call_id` as `f"mock-tool-{uuid4().hex[:8]}"` for each tool call
  - Ensure `tool_result` events reference the same `tool_call_id` as their corresponding `tool_call` events
  - Write tests: Mock provider events include `tool_call_id` in metadata, tool_result `tool_call_id` matches preceding tool_call `tool_call_id`

  **Must NOT do**:
  - Do NOT add `tool_call_id` as a field on `ProviderEvent` dataclass — keep it in `metadata` dict
  - Do NOT modify OpenHands provider — it already emits `tool_call_id`
  - Do NOT change the event contract for existing consumers

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Small localized change to one provider file + test
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 1, 2)
  - **Blocks**: Task 14 (needs mock provider with tool_call_id for frontend tests)
  - **Blocked By**: None (can start immediately)

  **References**:

  **Pattern References**:
  - `backend/app/services/agent_providers.py:MockAgentProvider` — Current implementation emits `tool_call` and `tool_result` events without `tool_call_id` in metadata. Follow the same structure but add `metadata={"tool_call_id": ...}`.

  **Test References**:
  - `backend/tests/test_agents.py` — Add tests verifying Mock provider events include `tool_call_id` and they're properly paired.

  **WHY Each Reference Matters**:
  - `MockAgentProvider` — Exact file and class to modify. Must follow the same event generation loop pattern. Wrong structure breaks test consumers.

  **Acceptance Criteria**:

  - [ ] `MockAgentProvider` includes `tool_call_id` in `metadata` for `tool_call` events
  - [ ] `MockAgentProvider` includes matching `tool_call_id` in `metadata` for `tool_result` events
  - [ ] `uv run pytest backend/tests/test_agents.py -v` → all tests pass
  - [ ] `uv run ruff check backend/app/services/agent_providers.py` → no new errors

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Mock provider emits tool_call_id in tool_call events
    Tool: Bash
    Preconditions: Backend tests runnable
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py -v -k "mock" 2>&1 | tail -20`
      2. Assert exit code 0
      3. Assert output shows PASSED for mock provider tests
    Expected Result: Mock provider includes tool_call_id in metadata
    Failure Indicators: Test fails or tool_call_id missing from metadata
    Evidence: .omo/evidence/task-3-mock-toolcallid.txt

  Scenario: tool_result tool_call_id matches tool_call
    Tool: Bash
    Preconditions: Backend tests runnable
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py -v -k "tool_call_id pairing" 2>&1 | tail -10`
      2. Assert exit code 0
    Expected Result: Paired events have matching tool_call_id values
    Failure Indicators: tool_result has different or null tool_call_id
    Evidence: .omo/evidence/task-3-pairing-test.txt
  ```

  **Commit**: YES
  - Message: `fix(backend): update Mock provider with tool_call_id`
  - Files: `backend/app/services/agent_providers.py`, `backend/tests/test_agents.py`
  - Pre-commit: `cd backend && uv run pytest backend/tests/test_agents.py -v && uv run ruff check .`

- [x] 4. Baseline capture + file change computation service (TDD)

  **What to do**:
  - Write failing tests for baseline capture and file change computation
  - Add `_capture_baseline(workspace_id)` method to AgentService: captures git status before run starts via `RepositoryService.status()`
  - Add `_compute_file_changes(workspace_id, baseline)` method: compares current git status against baseline, returns list of `AgentFileChange` objects
  - Handle workspace with no git repo: `RepositoryService.status()` returns `GitStatus(cloned=False)` — return empty baseline, skip diff
  - Handle file rename: map `R` status in `parse_porcelain_status()` to "renamed" status badge
  - Integrate into `_execute_run()`: call baseline capture before streaming, compute diff in `_record_run_finished()` or after run completion
  - Store `AgentFileChange` rows via `AgentService.create_file_change()`
  - Cap file change display at 50 items (store all, but indicate cap in API response)

  **Must NOT do**:
  - Do NOT add real-time file change streaming (compute at end only)
  - Do NOT store diff content or line counts in AgentFileChange
  - Do NOT add inotify/watchman file watching
  - Do NOT modify RepositoryService — just call its existing `status()` method

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: TDD cycle with complex logic (git state comparison, edge cases, integration with run lifecycle)
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (blocked by Task 2 for model reference)
  - **Parallel Group**: Wave 1 (sequential after Task 2)
  - **Blocks**: Task 16
  - **Blocked By**: Task 2 (needs migration to exist for model fields)

  **References**:

  **Pattern References**:
  - `backend/app/services/repositories.py:RepositoryService` — Has `status()` method returning `GitStatus`. Use this for baseline and diff. Note: returns `GitStatus(cloned=False)` when no repo.
  - `backend/app/services/agents.py:AgentService._execute_run()` — The run lifecycle method. Baseline capture goes before the streaming starts. File change computation goes after run completes.
  - `backend/app/services/agents.py:_record_run_finished()` — Called when run reaches terminal state. This is where file change storage should happen.

  **API/Type References**:
  - `backend/app/models/agent.py:AgentFileChange` — Created in Task 1. Columns: `id`, `agent_run_id`, `path`, `status`, `tool_call_id`, `created_at`.
  - `backend/app/services/repositories.py:GitStatus` and `GitFileStatus` — Data classes returned by `RepositoryService.status()`. `GitFileStatus` has `path` and `status` fields.

  **Test References**:
  - `backend/tests/test_agents.py` — Add `TestFileChangeComputation` class with tests for: baseline capture, diff computation, no-git-workspace handling, rename detection, 50-item cap.

  **WHY Each Reference Matters**:
  - `RepositoryService` — The exact API for getting git status. Must call `status()` correctly with workspace session.
  - `_execute_run()` — Must insert baseline capture at the right point (before streaming starts, after sandbox init).
  - `_record_run_finished()` — Must insert diff computation before marking run as completed.

  **Acceptance Criteria**:

  **If TDD:**
  - [ ] Test file: `backend/tests/test_agents.py` — `TestFileChangeComputation` class
  - [ ] `uv run pytest backend/tests/test_agents.py::TestFileChangeComputation -v` → PASS (5+ tests)

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Compute file changes after run completes
    Tool: Bash
    Preconditions: Backend services running, test database available
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py::TestFileChangeComputation::test_compute_file_changes -v`
      2. Assert exit code 0
    Expected Result: File changes computed correctly between baseline and post-run git status
    Failure Indicators: Test fails, wrong status mapping, or missing files
    Evidence: .omo/evidence/task-4-filechange-computation.txt

  Scenario: Workspace without git repo returns empty file changes
    Tool: Bash
    Preconditions: Test database available
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py::TestFileChangeComputation::test_no_git_workspace -v`
      2. Assert exit code 0
    Expected Result: Returns empty list, no crash, no RuntimeError
    Failure Indicators: Test raises exception or returns non-list
    Evidence: .omo/evidence/task-4-no-git-workspace.txt

  Scenario: Run that errors immediately has empty file changes
    Tool: Bash
    Preconditions: Test database available
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py::TestFileChangeComputation::test_error_run_empty_changes -v`
      2. Assert exit code 0
    Expected Result: Baseline exists but file_changes list is empty
    Failure Indicators: Test fails or pre-condition assertion fails
    Evidence: .omo/evidence/task-4-error-run.txt
  ```

  **Commit**: YES
  - Message: `feat(backend): add baseline capture + file change computation`
  - Files: `backend/app/services/agents.py`, `backend/tests/test_agents.py`
  - Pre-commit: `cd backend && uv run pytest backend/tests/test_agents.py -v`

- [x] 5. File-changes API endpoint

  **What to do**:
  - Add `GET /workspaces/{workspace_id}/agent/runs/{run_id}/file-changes` endpoint to `backend/app/api/agents.py`
  - Add `AgentFileChangeResponse` schema to `backend/app/schemas/agents.py`: `id`, `path`, `status`, `tool_call_id`, `created_at`
  - Add `AgentFileChangeListResponse` schema: `items: list[AgentFileChangeResponse]`, `total: int`, `capped: bool` (True if >50 items)
  - Endpoint calls `AgentService.get_file_changes_for_run(run_id)`, limits display to 50 items
  - Add `limit` + `offset` query params to existing `GET /workspaces/{workspace_id}/agent/runs` endpoint (default limit=20, default offset=0)
  - Ensure path validation: workspace must exist, run must belong to workspace

  **Must NOT do**:
  - Do NOT add file content or diff to the response (path + status only)
  - Do NOT add search/filter/sort to the file-changes endpoint
  - Do NOT expose internal AgentFileChange ORM model directly — use Pydantic schema

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: API endpoint design with schema, validation, and pagination
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (blocked by Task 1 for model)
  - **Parallel Group**: Wave 2 (after Wave 1)
  - **Blocks**: Tasks 9, 12
  - **Blocked By**: Task 1 (AgentFileChange model)

  **References**:

  **Pattern References**:
  - `backend/app/api/agents.py` — Follow existing endpoint patterns: `get_workspace_agent_runs`, `get_agent_run_detail`. Same path prefix, auth/validation, response schema style.
  - `backend/app/schemas/agents.py` — Follow `AgentRunResponse`, `AgentEventResponse` patterns for `AgentFileChangeResponse`.

  **Test References**:
  - `backend/tests/test_agents.py` — Follow existing API test patterns: async client, fixture creation, endpoint assertions.

  **WHY Each Reference Matters**:
  - `agents.py` API router — Must follow exact same path patterns and dependency injection. Wrong path → 404.
  - `agents.py` schemas — Must match existing field naming conventions. Inconsistent naming → frontend integration errors.

  **Acceptance Criteria**:

  - [ ] `GET /workspaces/{id}/agent/runs/{run_id}/file-changes` returns 200 with `AgentFileChangeListResponse`
  - [ ] `GET /workspaces/{id}/agent/runs?limit=20&offset=0` returns paginated runs
  - [ ] 404 returned for non-existent workspace or run
  - [ ] 403 returned if run doesn't belong to workspace
  - [ ] `uv run ruff check backend/app/api/agents.py backend/app/schemas/agents.py` → no errors

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: File-changes endpoint returns correct structure
    Tool: Bash (curl)
    Preconditions: Backend server running, test data seeded
    Steps:
      1. `curl -s http://localhost:8000/api/v1/workspaces/{id}/agent/runs/{run_id}/file-changes | python3 -m json.tool`
      2. Assert response has `items` array, `total` integer, `capped` boolean
    Expected Result: 200 response with expected JSON structure
    Failure Indicators: 404, 403, or missing fields
    Evidence: .omo/evidence/task-5-api-filechanges.txt

  Scenario: Non-existent run returns 404
    Tool: Bash (curl)
    Preconditions: Backend server running
    Steps:
      1. `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/v1/workspaces/{id}/agent/runs/nonexistent/file-changes`
      2. Assert HTTP status 404
    Expected Result: 404 Not Found
    Failure Indicators: 200 or 500 status code
    Evidence: .omo/evidence/task-5-api-404.txt
  ```

  **Commit**: YES
  - Message: `feat(backend): add file-changes API endpoint`
  - Files: `backend/app/api/agents.py`, `backend/app/schemas/agents.py`
  - Pre-commit: `cd backend && uv run ruff check . && uv run pytest -v`

- [x] 6. File-changes schema + API tests

  **What to do**:
  - Write comprehensive tests for the file-changes API endpoint
  - Test cases: valid run with changes, valid run with no changes, run with >50 changes (capped response), non-existent run (404), unauthorized run (403), invalid workspace (404)
  - Test `limit` and `offset` params on runs endpoint
  - Verify `AgentFileChangeResponse` schema serialization
  - Verify `capped: true` when changes exceed 50

  **Must NOT do**:
  - Do NOT test file content or diff output (not in response)
  - Do NOT test sorting or filtering (not implemented)

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Test writing following established patterns
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 1 and 5)
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 12
  - **Blocked By**: Tasks 1, 5

  **References**:
  - `backend/tests/test_agents.py` — Follow existing test class patterns, fixture creation, and assertion style.

  **Acceptance Criteria**:

  - [ ] `uv run pytest backend/tests/test_agents.py::TestFileChangesAPI -v` → PASS (5+ tests)
  - [ ] Tests cover: valid changes, empty changes, capped changes, 404, 403

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: All file-changes API tests pass
    Tool: Bash
    Preconditions: Backend services running
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py::TestFileChangesAPI -v`
      2. Assert exit code 0
      3. Assert output shows 5+ PASSED tests
    Expected Result: All file-changes API tests pass
    Failure Indicators: Any test FAIL or ERROR
    Evidence: .omo/evidence/task-6-api-tests.txt
  ```

  **Commit**: YES (group with Task 5)
  - Message: `test(backend): add file-changes schema and API tests`
  - Files: `backend/tests/test_agents.py`
  - Pre-commit: `cd backend && uv run pytest backend/tests/test_agents.py -v`

- [x] 7. Extract AgentPanel + remove right sidebar

  **What to do**:
  - Extract the `AgentPanel` component (lines 399-681) from `DashboardShell.tsx` into `frontend/src/components/AgentTab.tsx` (temporary, will be rebuilt)
  - Remove the right sidebar `<aside>` and all `rightCollapsed` / `setRightCollapsed` state from `DashboardShell.tsx`
  - Simplify the 3-column CSS grid to 2-column: `grid-template-columns: 240px 1fr` when left sidebar expanded, `72px 1fr` when collapsed
  - Use `ast_grep_search` to find ALL references to `rightCollapsed` and `setRightCollapsed` first
  - Use `lsp_find_references` on `AgentPanel` before extracting
  - Remove the "Agents" sidebar item's current `href="/"` — it will be wired in Task 15
  - Verify `pnpm build` passes after changes

  **Must NOT do**:
  - Do NOT redesign the layout — just remove the right column
  - Do NOT touch the left sidebar or main content area
  - Do NOT add Agent tab yet (Task 8) — just remove the right sidebar
  - Do NOT extract into a shared hook or context (stay local)
  - Do NOT change any tab behavior

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: CSS grid restructuring + component extraction — visual layout changes
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 8
  - **Blocked By**: None (can start immediately)

  **References**:

  **Pattern References**:
  - `frontend/src/components/DashboardShell.tsx:72-81` — Grid class logic that switches between 4 layout variants based on `leftCollapsed` + `rightCollapsed`.
  - `frontend/src/components/DashboardShell.tsx:399-681` — AgentPanel component to extract.

  **API/Type References**:
  - `frontend/src/routes/DashboardRoute.tsx` — `DashboardTab` type union and `dashboardTabs` array. Will need `"agent"` added later (Task 8).

  **WHY Each Reference Matters**:
  - Grid logic at lines 72-81 — Must simplify from 4 variants to 2 variants. Wrong CSS → broken layout.
  - AgentPanel at lines 399-681 — Must extract cleanly. Missing state or imports → build failure.

  **Acceptance Criteria**:

  - [ ] `pnpm build` succeeds with no errors
  - [ ] No `rightCollapsed` references remain in DashboardShell.tsx
  - [ ] No right `<aside>` element in DashboardShell.tsx
  - [ ] Layout renders as 2-column grid (left sidebar + main content)
  - [ ] `pnpm lint` passes with no new warnings

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Right sidebar removed, layout still renders
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
      3. Run `grep -c "rightCollapsed" src/components/DashboardShell.tsx` → expect 0
      4. Run `pnpm test -- --run` → all existing tests pass
    Expected Result: Build passes, no rightCollapsed references, existing tests pass
    Failure Indicators: Build fails or rightCollapsed references remain
    Evidence: .omo/evidence/task-7-sidebar-removed.txt

  Scenario: Existing tabs still work after layout change
    Tool: Bash
    Preconditions: All tests pass
    Steps:
      1. Run `cd frontend && pnpm test -- --run 2>&1 | tail -10`
      2. Assert exit code 0
    Expected Result: All existing dashboard tests pass
    Failure Indicators: Any test regression
    Evidence: .omo/evidence/task-7-existing-tests.txt
  ```

  **Commit**: YES
  - Message: `refactor(frontend): extract AgentPanel + remove right sidebar`
  - Files: `frontend/src/components/DashboardShell.tsx`, `frontend/src/components/AgentTab.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint`

- [x] 8. Agent tab scaffolding + empty state

  **What to do**:
  - Add `"agent"` to `DashboardTab` type union in `DashboardRoute.tsx`
  - Add `{ id: "agent", label: "Agent", icon: Bot }` to `dashboardTabs` array
  - Create `frontend/src/components/AgentTab/` directory with `index.tsx` main component
  - AgentTab: receives `workspaceId` prop, shows empty state when no workspace active
  - Empty state: "Select a workspace to manage agents" with workspace selector highlight
  - When workspace active: show placeholder content for subsequent tasks
  - Tab state persisted in localStorage via existing mechanism

  **Must NOT do**:
  - Do NOT add prompt composer yet (Task 10)
  - Do NOT add WebSocket logic yet (Task 14)
  - Do NOT create AgentContext or useAgent hook
  - Do NOT modify other tabs or left sidebar structure

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: UI component creation, tab system integration
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Task 7)
  - **Parallel Group**: Wave 2
  - **Blocks**: Tasks 10, 11, 12, 13, 14, 15
  - **Blocked By**: Task 7

  **References**:

  **Pattern References**:
  - `frontend/src/routes/DashboardRoute.tsx` — Follow exact pattern for tab type and `dashboardTabs` array.

  **API/Type References**:
  - `frontend/src/routes/DashboardRoute.tsx:DashboardTab` — Type union to extend with "agent".

  **WHY Each Reference Matters**:
  - `DashboardRoute.tsx` — Must match exact tab structure pattern. Wrong `id` or `icon` → TypeScript error or rendering failure.

  **Acceptance Criteria**:

  - [ ] `"agent"` appears in `DashboardTab` type union
  - [ ] `{ id: "agent", label: "Agent", icon: Bot }` in `dashboardTabs` array
  - [ ] `pnpm build` succeeds
  - [ ] Agent tab renders with empty state when no workspace selected
  - [ ] Agent tab renders placeholder content when workspace is active
  - [ ] `pnpm test` passes

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Agent tab type and component compile correctly
    Tool: Bash
    Preconditions: Frontend code written
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
    Expected Result: Build succeeds with no type errors
    Failure Indicators: Build fails with TypeScript errors
    Evidence: .omo/evidence/task-8-tab-scaffolding.txt

  Scenario: Dashboard renders correctly with Agent tab
    Tool: Bash
    Preconditions: All tests pass
    Steps:
      1. Run `cd frontend && pnpm test -- --run 2>&1 | tail -10`
      2. Assert exit code 0
    Expected Result: All existing tests pass, no regressions
    Failure Indicators: Any test failure
    Evidence: .omo/evidence/task-8-tests-pass.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): add Agent tab + empty state`
  - Files: `frontend/src/routes/DashboardRoute.tsx`, `frontend/src/components/AgentTab/index.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint`

- [x] 9. api.ts file-changes function + types

  **What to do**:
  - Add TypeScript types for `AgentFileChange`, `AgentFileChangeListResponse` in `frontend/src/lib/api.ts`
  - Add `getAgentFileChanges(workspaceId: string, runId: string): Promise<AgentFileChangeListResponse>` function
  - Add `limit` and `offset` params to existing `getAgentRuns` function (if not already present)
  - Add `tool_call_id` field to `AgentEventResponse` type (nullable string)
  - Follow existing patterns: `credentials: "include"` for auth, typed response functions

  **Must NOT do**:
  - Do NOT create a new API client file — add to existing `api.ts`
  - Do NOT add file content or diff types — only path + status
  - Do NOT add WebSocket functions yet (Task 14)

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Adding types and a fetch function to existing file
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Task 5 for endpoint)
  - **Parallel Group**: Wave 2
  - **Blocks**: Tasks 10, 12, 13
  - **Blocked By**: Task 5

  **References**:

  **Pattern References**:
  - `frontend/src/lib/api.ts:676-708` — Agent API functions section. Follow exact pattern: typed function, URL construction, `credentials: "include"`.
  - `frontend/src/lib/api.ts:170-199` — Agent type definitions section. Follow same interface/structure pattern.

  **WHY Each Reference Matters**:
  - `api.ts` lines 676-708 — Must follow exact function pattern for auth consistency.
  - `api.ts` lines 170-199 — Must match backend schema types exactly for type safety. Mismatch → runtime errors.

  **Acceptance Criteria**:

  - [ ] `AgentFileChange` type defined with `id`, `path`, `status`, `tool_call_id`, `created_at` fields
  - [ ] `AgentFileChangeListResponse` type with `items`, `total`, `capped` fields
  - [ ] `getAgentFileChanges()` function added
  - [ ] `tool_call_id` added to `AgentEventResponse` type (nullable)
  - [ ] `pnpm build` succeeds (no type errors)
  - [ ] `pnpm lint` passes

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: TypeScript types compile correctly
    Tool: Bash
    Preconditions: Frontend code written
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
    Expected Result: Build succeeds with no type errors
    Failure Indicators: Build fails with type errors
    Evidence: .omo/evidence/task-9-types-build.txt

  Scenario: Lint passes with new additions
    Tool: Bash
    Preconditions: All changes made
    Steps:
      1. Run `cd frontend && pnpm lint 2>&1 | tail -5`
      2. Assert exit code 0
    Expected Result: No new lint errors
    Failure Indicators: ESLint errors on new code
    Evidence: .omo/evidence/task-9-lint.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): add file-changes API function + types`
  - Files: `frontend/src/lib/api.ts`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint`

- [x] 10. Prompt composer + Run/Stop controls

  **What to do**:
  - Create `frontend/src/components/AgentTab/PromptComposer.tsx`
  - Prompt composer: textarea for prompt input, Run button (submits prompt), Stop button (sends stop request)
  - Run button calls `createAgentRun(workspaceId, prompt)` from `api.ts`
  - Stop button calls `stopAgentRun(workspaceId, runId)` from `api.ts`
  - Show "Running..." state with Stop button visible when run is active
  - Disable textarea and show Stop button during active run
  - Auto-scroll to new run in history after creation
  - Use Tailwind utilities only: cream canvas background, hairline borders, primary (#f54e00) for Run button, 8px border-radius on CTA
  - Use Lucide icons: `Play` for Run, `Square` for Stop

  **Must NOT do**:
  - Do NOT add Command palette or slash commands
  - Do NOT add prompt history/suggestions dropdown
  - Do NOT create a shared useAgent hook (keep state local)
  - Do NOT use any component library (Tailwind only)

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: UI component with interaction state, Tailwind styling
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 8, 9)
  - **Parallel Group**: Wave 3
  - **Blocks**: Task 17
  - **Blocked By**: Tasks 8, 9

  **References**:

  **Pattern References**:
  - `frontend/src/components/DashboardShell.tsx:399-450` (original AgentPanel) — Has prompt form UI, Run/Stop buttons. Extract and improve the pattern.
  - `frontend/src/lib/api.ts:676-708` — `createAgentRun()` and `stopAgentRun()` functions to call.

  **API/Type References**:
  - `frontend/src/lib/api.ts` — `createAgentRun(workspaceId: string, prompt: string)` and `stopAgentRun(workspaceId: string, runId: string)` function signatures.

  **WHY Each Reference Matters**:
  - Original AgentPanel — Contains the exact prompt form pattern, button styling, and state management to reference.
  - `api.ts` — Must call the right functions with correct parameters. Wrong function → API error.

  **Acceptance Criteria**:

  - [ ] PromptComposer renders with textarea, Run button (Play icon), Stop button (Square icon)
  - [ ] Run button calls `createAgentRun()` and switches to "Running" state
  - [ ] Stop button calls `stopAgentRun()` and switches back to idle
  - [ ] Textarea disabled during active run
  - [ ] `pnpm build` succeeds
  - [ ] `pnpm lint` passes

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Prompt composer renders and creates run
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
      3. Verify PromptComposer.tsx exists in AgentTab/ directory
    Expected Result: Build succeeds, component created
    Failure Indicators: Build fails, TypeScript errors
    Evidence: .omo/evidence/task-10-prompt-composer.txt

  Scenario: Lint passes with new component
    Tool: Bash
    Preconditions: All code written
    Steps:
      1. Run `cd frontend && pnpm lint 2>&1 | tail -5`
      2. Assert exit code 0
    Expected Result: No new lint warnings
    Failure Indicators: ESLint warnings on PromptComposer
    Evidence: .omo/evidence/task-10-lint.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): prompt composer + Run/Stop controls`
  - Files: `frontend/src/components/AgentTab/PromptComposer.tsx`, `frontend/src/components/AgentTab/index.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint`

- [x] 11. Tool cards with paired tool_call/tool_result

  **What to do**:
  - Create `frontend/src/components/AgentTab/ToolCard.tsx`
  - Tool card: collapsed view shows tool name + status badge (🟢 running, ✅ completed, ❌ error)
  - Expandable: click to reveal tool arguments, output, exit code, and changed files
  - Pair `tool_call` and `tool_result` events using `tool_call_id` — group them into a single card
  - Handle events without `tool_call_id` gracefully (show as ungrouped cards, no pairing)
  - Show stream connection failures separately from agent/tool failures
  - Use Tailwind: hairline border, card background `panel` color, 8px border-radius, Lucide icons
  - Write test: paired events render as single card, unpaired events render separately, missing tool_call_id handled

  **Must NOT do**:
  - Do NOT add JSON viewer or syntax highlighting (plain `<pre>` block)
  - Do NOT add copy-to-clipboard buttons
  - Do NOT add expand/collapse animation (just show/hide)
  - Do NOT create a shared useAgent hook

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: Interactive UI component with nested state and pairing logic
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Task 8)
  - **Parallel Group**: Wave 3
  - **Blocks**: Task 17
  - **Blocked By**: Task 8

  **References**:

  **Pattern References**:
  - `frontend/src/components/DashboardShell.tsx:450-550` (original AgentPanel tools section) — Has tool rendering UI. Extract and improve the pattern with expandable cards.

  **API/Type References**:
  - `frontend/src/lib/api.ts:170-199` — `AgentEventResponse` type now includes `tool_call_id` (nullable). Use this for pairing.

  **WHY Each Reference Matters**:
  - Original tools section — Contains the basic tool rendering pattern to improve upon.
  - `AgentEventResponse` — Must use `tool_call_id` for pairing logic. Wrong type → pairing fails.

  **Acceptance Criteria**:

  - [ ] Tool cards render with status badge per tool name
  - [ ] Paired events (same `tool_call_id`) render as single expandable card
  - [ ] Events without `tool_call_id` render as separate ungrouped cards
  - [ ] Expanded view shows arguments, output, exit code
  - [ ] Stream disconnect errors shown separately from tool errors
  - [ ] `pnpm build` and `pnpm test` pass

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Tool cards render with paired events
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
      3. Run `pnpm test -- --run 2>&1 | tail -10`
      4. Assert all tests pass
    Expected Result: Build and tests pass, ToolCard component created
    Failure Indicators: Build fails or test failures
    Evidence: .omo/evidence/task-11-tool-cards.txt

  Scenario: Events without tool_call_id still render
    Tool: Bash
    Preconditions: Frontend tests pass
    Steps:
      1. Verify ToolCard component handles null tool_call_id without crash
      2. Run `cd frontend && pnpm test -- --run 2>&1 | tail -10`
    Expected Result: Ungrouped events render as individual cards
    Failure Indicators: TypeError or rendering crash on null tool_call_id
    Evidence: .omo/evidence/task-11-null-toolcallid.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): tool cards with paired tool_call/tool_result`
  - Files: `frontend/src/components/AgentTab/ToolCard.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint && pnpm test`

- [x] 12. Changed files list with status badges

  **What to do**:
  - Create `frontend/src/components/AgentTab/ChangedFiles.tsx`
  - List of file paths with status badges: 🟢 created, 🟡 modified, 🔴 deleted, 🔵 renamed
  - Cap display at 50 files with "N more files" summary text
  - Fetch data from `getAgentFileChanges(workspaceId, runId)` (from Task 9)
  - Show "No file changes" when list is empty
  - Show loading skeleton while fetching
  - Use Tailwind: monospace font (JetBrains Mono) for file paths, hairline dividers
  - Write test: renders file list with badges, caps at 50, shows empty state

  **Must NOT do**:
  - Do NOT add clickable file paths (no file preview)
  - Do NOT add diff viewer or file content
  - Do NOT add pagination for files (cap at 50 with summary)
  - Do NOT add search/filter for files

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: UI component with data fetching, badge rendering, cap logic
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 8, 9, 5)
  - **Parallel Group**: Wave 3
  - **Blocks**: Task 17
  - **Blocked By**: Tasks 8, 9, 5

  **References**:

  **Pattern References**:
  - `frontend/src/lib/api.ts` — `AgentFileChange` type with `path`, `status`, `tool_call_id` fields. Use for rendering.
  - `frontend/src/lib/api.ts` — `getAgentFileChanges()` function to call.

  **API/Type References**:
  - Backend `AgentFileChangeListResponse` — `items` array, `total` integer, `capped` boolean.

  **WHY Each Reference Matters**:
  - `AgentFileChange` type — Must match fields for correct rendering. `status` field values must map to badge colors.
  - `getAgentFileChanges()` — Must call with correct workspace and run IDs. Wrong params → 404.

  **Acceptance Criteria**:

  - [ ] Changed files list renders with status badges (created=🟢, modified=🟡, deleted=🔴, renamed=🔵)
  - [ ] Display capped at 50 files with "N more files" summary
  - [ ] "No file changes" message when list is empty
  - [ ] Loading skeleton while fetching
  - [ ] `pnpm build` and `pnpm test` pass

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Changed files list renders with badges
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
    Expected Result: Component created, build succeeds
    Failure Indicators: Build fails
    Evidence: .omo/evidence/task-12-changed-files.txt

  Scenario: List caps at 50 files and shows empty state
    Tool: Bash
    Preconditions: Frontend tests pass
    Steps:
      1. Run `cd frontend && pnpm test -- --run 2>&1 | tail -10`
      2. Assert all tests pass
    Expected Result: Cap logic and empty state work correctly
    Failure Indicators: Test failures on cap logic
    Evidence: .omo/evidence/task-12-cap-logic.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): changed files list with status badges`
  - Files: `frontend/src/components/AgentTab/ChangedFiles.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint && pnpm test`

- [x] 13. Run history with pagination

  **What to do**:
  - Create `frontend/src/components/AgentTab/RunHistory.tsx`
  - Show recent 20 runs per workspace with "Load more" button
  - Each run shows: prompt (truncated), status badge, model, elapsed time, token count
  - Clicking a run selects it and loads its events + file changes
  - Use `getAgentRuns(workspaceId, { limit: 20, offset })` from `api.ts`
  - "Load more" increments offset by 20 and appends to list
  - Use Tailwind: card layout, monospace for model/tokens, hairline dividers
  - Write test: renders 20 runs, "Load more" loads next page, clicking selects run

  **Must NOT do**:
  - Do NOT add search/filter/sort to run history
  - Do NOT add infinite scroll (explicit "Load more" button)
  - Do NOT add run deletion or retry actions

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: List component with pagination state and click handling
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 8, 9)
  - **Parallel Group**: Wave 3
  - **Blocks**: Task 17
  - **Blocked By**: Tasks 8, 9

  **References**:

  **Pattern References**:
  - `frontend/src/lib/api.ts` — `getAgentRuns()` function, `AgentRunResponse` type. Follow pagination pattern with `limit` and `offset`.

  **API/Type References**:
  - `frontend/src/lib/api.ts` — `AgentRunResponse` type with `id`, `prompt`, `status`, `model`, `tokens_used`, `started_at`, `completed_at` fields.

  **WHY Each Reference Matters**:
  - `getAgentRuns()` — Must call with correct pagination params. Wrong offset → duplicate or missing runs.
  - `AgentRunResponse` — Must render correct fields. Missing `status` field → broken badge rendering.

  **Acceptance Criteria**:

  - [ ] Run history shows recent 20 runs per workspace
  - [ ] "Load more" button loads next 20 runs
  - [ ] Each run shows prompt, status badge, model, elapsed time, tokens
  - [ ] Clicking a run selects it (loads events + file changes)
  - [ ] `pnpm build` and `pnpm test` pass

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Run history renders and paginates
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
      3. Run `pnpm test -- --run 2>&1 | tail -10`
    Expected Result: Build and tests pass, RunHistory component created
    Failure Indicators: Build fails or test failures
    Evidence: .omo/evidence/task-13-run-history.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): run history with pagination`
  - Files: `frontend/src/components/AgentTab/RunHistory.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint && pnpm test`

- [x] 14. WebSocket reconnection + disconnect handling

  **What to do**:
  - Add `onclose` handler to WebSocket in AgentTab component with exponential backoff reconnection (max 3 retries)
  - Show "Reconnecting..." status when WebSocket disconnects and attempting reconnect
  - Show "Disconnected" status when max retries exceeded
  - Show disconnected state separately from agent/tool errors (stream disconnect vs agent error)
  - On reconnect: re-subscribe to stream if run is still active
  - On reconnect after completed run: load final status from REST API (`getAgentRunDetail`)
  - Add reconnection delay: 1s, 2s, 4s (3 retries max)
  - Write test: WebSocket reconnects on close, max 3 retries, shows disconnect status

  **Must NOT do**:
  - Do NOT add SSE as WebSocket alternative
  - Do NOT add heartbeat or sequence number protocol
  - Do NOT extract into a shared hook (keep local to AgentTab)
  - Do NOT add `visibilitychange` handler (scope creep)

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Complex async state management with backoff algorithm and error handling
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 8, 3)
  - **Parallel Group**: Wave 3
  - **Blocks**: Task 16
  - **Blocked By**: Task 8, Task 3 (Mock provider needs tool_call_id for tests)

  **References**:

  **Pattern References**:
  - `frontend/src/components/DashboardShell.tsx:450-520` (original AgentPanel WS logic) — Has WebSocket creation with `socketRef`. No `onclose` handler. Improve this pattern.

  **API/Type References**:
  - `frontend/src/lib/api.ts` — `getAgentRunDetail()` for refreshing final status after reconnection.

  **WHY Each Reference Matters**:
  - Original WS logic — Current implementation has no reconnection. Must add `onclose`, `onerror`, and reconnect logic without breaking existing stream processing.
  - `getAgentRunDetail()` — Must call after reconnect to get final run status. Without this, stale data after reconnection.

  **Acceptance Criteria**:

  - [ ] WebSocket `onclose` handler attempts reconnect with backoff (1s, 2s, 4s)
  - [ ] "Reconnecting..." status shown during reconnect attempts
  - [ ] "Disconnected" status shown after max 3 retries
  - [ ] Stream disconnect errors shown separately from agent/tool errors
  - [ ] On reconnect during active run: re-subscribe to stream
  - [ ] On reconnect after completed run: refresh from REST API
  - [ ] `pnpm build` and `pnpm test` pass

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: WebSocket reconnects on disconnect
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
    Expected Result: Build succeeds, reconnection logic present
    Failure Indicators: Build fails, TypeScript errors
    Evidence: .omo/evidence/task-14-ws-reconnect.txt

  Scenario: Max reconnect retries respected
    Tool: Bash
    Preconditions: Frontend tests pass
    Steps:
      1. Run `cd frontend && pnpm test -- --run 2>&1 | tail -10`
      2. Assert all tests pass
    Expected Result: Reconnection stops after 3 failed attempts
    Failure Indicators: Test failure on retry count
    Evidence: .omo/evidence/task-14-max-retries.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): WebSocket reconnection with exponential backoff`
  - Files: `frontend/src/components/AgentTab/index.tsx`, `frontend/src/components/AgentTab/PromptComposer.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint && pnpm test`

- [x] 15. Sidebar "Agents" nav wiring

  **What to do**:
  - Update the "Agents" sidebar nav item in `DashboardShell.tsx` to call `setActiveTab("agent")` instead of `href="/"`
  - Find the `Bot` icon nav item and change its `onClick` to activate the Agent tab
  - Ensure the Agent tab highlights when active (same style as other active tabs)
  - Remove the `href="/"` from the "Agents" nav item

  **Must NOT do**:
  - Do NOT add any new sidebar items
  - Do NOT change the left sidebar structure or styling
  - Do NOT change any other nav item behavior

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: Small, targeted change to a single nav item
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Task 8)
  - **Parallel Group**: Wave 4
  - **Blocks**: Task 17
  - **Blocked By**: Task 8

  **References**:

  **Pattern References**:
  - `frontend/src/components/DashboardShell.tsx:64` — "Agents" nav item currently links to `href="/"`. Change to `onClick={() => setActiveTab("agent")}`.
  - Other nav items in DashboardShell — Follow their `onClick` pattern for tab activation.

  **WHY Each Reference Matters**:
  - Line 64 — The exact nav item to modify. Wrong handler → Agent tab doesn't activate.

  **Acceptance Criteria**:

  - [ ] "Agents" sidebar nav item activates Agent tab on click
  - [ ] Agent tab highlights when active (matches other tab styles)
  - [ ] No `href="/"` on "Agents" nav item
  - [ ] `pnpm build` and `pnpm lint` pass

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Sidebar "Agents" nav activates Agent tab
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
      3. Verify no `href="/"` on "Agents" nav item in DashboardShell.tsx
    Expected Result: Build passes, nav item wired to Agent tab
    Failure Indicators: Build fails or href still present
    Evidence: .omo/evidence/task-15-nav-wiring.txt
  ```

  **Commit**: YES
  - Message: `fix(frontend): wire Agents sidebar nav to Agent tab`
  - Files: `frontend/src/components/DashboardShell.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint`

- [x] 16. Final WebSocket status sync (refresh after close)

  **What to do**:
  - After WebSocket closes on a completed/errored/stopped run, refresh the run's final status from the REST API
  - Call `getAgentRunDetail(workspaceId, runId)` to get final `status`, `error_message`, `tokens_used`, `completed_at`
  - Update the run state with fresh data from the API response
  - This ensures the UI shows accurate final status even if WebSocket dropped events
  - Write test: completed run gets refreshed from API after WS close, error run shows error_message

  **Must NOT do**:
  - Do NOT add polling or periodic refresh
  - Do NOT add SSE as fallback
  - Do NOT change the WebSocket message format
  - Do NOT modify the backend status update logic

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: Async state synchronization logic with error handling
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on Tasks 4, 14)
  - **Parallel Group**: Wave 4
  - **Blocks**: Task 17
  - **Blocked By**: Tasks 4, 14

  **References**:

  **Pattern References**:
  - `frontend/src/lib/api.ts` — `getAgentRunDetail()` function for refreshing final status.

  **API/Type References**:
  - `frontend/src/lib/api.ts` — `AgentRunResponse` type with `status`, `error_message`, `tokens_used`, `completed_at` fields.
  - Backend `/workspaces/{id}/agent/runs/{id}` endpoint — Returns complete run detail including final status.

  **WHY Each Reference Matters**:
  - `getAgentRunDetail()` — Must call after WS close to get accurate final state. Stale state → incorrect UI.
  - `AgentRunResponse` — Must use correct fields to update local state. Mismatch → UI flicker or missing data.

  **Acceptance Criteria**:

  - [ ] After WebSocket closes, run detail is refreshed from REST API
  - [ ] Final status, error_message, tokens_used, completed_at are updated
  - [ ] Works for completed, errored, and stopped runs
  - [ ] `pnpm build` and `pnpm test` pass

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Final status refreshed after WebSocket close
    Tool: Bash
    Preconditions: Frontend build passes
    Steps:
      1. Run `cd frontend && pnpm build 2>&1 | tail -5`
      2. Assert exit code 0
      3. Run `pnpm test -- --run 2>&1 | tail -10`
    Expected Result: Build and tests pass, refresh logic present
    Failure Indicators: Build fails or tests fail
    Evidence: .omo/evidence/task-16-ws-status-sync.txt

  Scenario: Error run shows error message after refresh
    Tool: Bash
    Preconditions: Frontend tests pass
    Steps:
      1. Verify AgentTab refreshes error_message from API on error run
      2. Run `cd frontend && pnpm test -- --run 2>&1 | tail -10`
    Expected Result: Error run shows proper error_message after WS close
    Failure Indicators: Error message missing or stale
    Evidence: .omo/evidence/task-16-error-refresh.txt
  ```

  **Commit**: YES
  - Message: `feat(frontend): final WebSocket status sync after close`
  - Files: `frontend/src/components/AgentTab/index.tsx`
  - Pre-commit: `cd frontend && pnpm build && pnpm lint && pnpm test`

- [x] 17. Integration tests end-to-end

  **What to do**:
  - Write backend integration tests: create run → execute → capture file changes → list file changes via API → verify response structure
  - Write frontend integration tests: render AgentTab → start run → see tool cards → see file changes → load more runs
  - Test cross-cutting: create run with Mock provider → verify tool_call_id pairing in tool cards → verify file changes appear
  - Test edge cases: run with 0 tool calls (only output_text), workspace without git repo, error run with partial file changes
  - Run full verification: `cd backend && uv run pytest -v`, `cd frontend && pnpm test`, `cd frontend && pnpm build`, `cd frontend && pnpm lint`, `cd backend && uv run ruff check .`

  **Must NOT do**:
  - Do NOT add end-to-end browser tests (Playwright) — these are unit/integration tests only
  - Do NOT modify production code to make tests work
  - Do NOT add new features — only test existing features

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: Integration testing across frontend + backend, requires understanding both systems
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: NO (depends on all tasks)
  - **Parallel Group**: Wave 4
  - **Blocks**: F1-F4
  - **Blocked By**: All previous tasks

  **References**:

  **Pattern References**:
  - `backend/tests/test_agents.py` — Existing backend test patterns for integration tests.
  - `frontend/src/__tests__/` or `frontend/src/test/setup.ts` — Frontend test patterns.

  **Test References**:
  - Backend verification: `cd backend && uv run pytest -v && uv run ruff check .`
  - Frontend verification: `cd frontend && pnpm test && pnpm lint && pnpm build`

  **Why Each Reference Matters**:
  - Existing test patterns — Must follow same fixtures, assertion styles, and async patterns. Different styles → CI failures.

  **Acceptance Criteria**:

  - [ ] `cd backend && uv run pytest -v` → all tests pass
  - [ ] `cd backend && uv run ruff check .` → no new errors
  - [ ] `cd frontend && pnpm test` → all tests pass
  - [ ] `cd frontend && pnpm lint` → exit code 0
  - [ ] `cd frontend && pnpm build` → no type errors
  - [ ] Integration test for full run lifecycle passes
  - [ ] Edge case tests pass (0 tool calls, no git repo, error run)

  **QA Scenarios (MANDATORY):**

  ```
  Scenario: Full backend test suite passes
    Tool: Bash
    Preconditions: All code changes complete
    Steps:
      1. Run `cd backend && uv run pytest -v 2>&1 | tail -20`
      2. Assert exit code 0
      3. Run `uv run ruff check . 2>&1 | tail -5`
      4. Assert exit code 0
    Expected Result: All backend tests pass, no new lint errors
    Failure Indicators: Any test failure or lint error
    Evidence: .omo/evidence/task-17-backend-tests.txt

  Scenario: Full frontend test suite passes
    Tool: Bash
    Preconditions: All code changes complete
    Steps:
      1. Run `cd frontend && pnpm test 2>&1 | tail -10`
      2. Assert exit code 0
      3. Run `pnpm lint 2>&1 | tail -5`
      4. Assert exit code 0
      5. Run `pnpm build 2>&1 | tail -5`
      6. Assert exit code 0
    Expected Result: All frontend tests pass, lint clean, build succeeds
    Failure Indicators: Any test failure, lint error, or build error
    Evidence: .omo/evidence/task-17-frontend-tests.txt

  Scenario: Edge case - run with 0 tool calls
    Tool: Bash
    Preconditions: Backend tests runnable
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py -v -k "zero_tool" 2>&1 | tail -10`
      2. Assert all edge case tests pass
    Expected Result: Runs with no tool calls are handled gracefully
    Failure Indicators: Test failures or unhandled edge cases
    Evidence: .omo/evidence/task-17-edge-zero-tool.txt

  Scenario: Edge case - workspace without git repo
    Tool: Bash
    Preconditions: Backend tests runnable
    Steps:
      1. Run `cd backend && uv run pytest backend/tests/test_agents.py -v -k "no_git" 2>&1 | tail -10`
      2. Assert all no-git tests pass
    Expected Result: Returns empty file changes, no crash
    Failure Indicators: RuntimeError or empty responses
    Evidence: .omo/evidence/task-17-edge-no-git.txt
  ```

  **Commit**: YES
  - Message: `test: integration tests end-to-end`
  - Files: `backend/tests/test_agents.py`, `frontend/src/__tests__/`
  - Pre-commit: `cd backend && uv run pytest -v && cd ../frontend && pnpm test && pnpm build`

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
>
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.**

- [ ] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists (read file, curl endpoint, run command). For each "Must NOT Have": search codebase for forbidden patterns — reject with file:line if found. Check evidence files exist in `.omo/evidence/`. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [ ] F2. **Code Quality Review** — `unspecified-high`
  Run `tsc --noEmit` + linter + `pnpm test`. Review all changed files for: `as any`/`@ts-ignore`, empty catches, console.log in prod, commented-out code, unused imports. Check AI slop: excessive comments, over-abstraction, generic names (data/result/item/temp).
  Output: `Build [PASS/FAIL] | Lint [PASS/FAIL] | Tests [N pass/N fail] | Files [N clean/N issues] | VERDICT`

- [ ] F3. **Real Manual QA** — `unspecified-high`
  Start from clean state. Execute EVERY QA scenario from EVERY task — follow exact steps, capture evidence. Test cross-task integration (features working together, not isolation). Test edge cases: no workspace selected, run with 0 tool calls, run with 100+ file changes, WebSocket disconnect. Save to `.omo/evidence/final-qa/`.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [ ] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", read actual diff (git log/diff). Verify 1:1 — everything in spec was built (no missing), nothing beyond spec was built (no creep). Check "Must NOT do" compliance. Detect cross-task contamination: Task N touching Task M's files. Flag unaccounted changes.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

- **1**: `feat(backend): add AgentFileChange model + CRUD` - backend/app/models/agent.py, backend/tests/test_agents.py
- **2**: `feat(backend): add tool_call_id column to agent_events migration` - backend/alembic/versions/
- **3**: `fix(backend): update Mock provider with tool_call_id` - backend/app/services/agent_providers.py, backend/tests/test_agents.py
- **4**: `feat(backend): add baseline capture + file change computation` - backend/app/services/agents.py
- **5**: `feat(backend): add file-changes API endpoint` - backend/app/api/agents.py, backend/app/schemas/agents.py
- **6**: `test(backend): add file-changes schema + API tests` - backend/tests/test_agents.py
- **7**: `refactor(frontend): extract AgentPanel + remove right sidebar` - frontend/src/components/DashboardShell.tsx, frontend/src/components/AgentTab.tsx
- **8**: `feat(frontend): add Agent tab + empty state` - frontend/src/routes/DashboardRoute.tsx, frontend/src/components/AgentTab/
- **9**: `feat(frontend): add file-changes API function + types` - frontend/src/lib/api.ts
- **10**: `feat(frontend): prompt composer + Run/Stop controls` - frontend/src/components/AgentTab/PromptComposer.tsx
- **11**: `feat(frontend): tool cards with paired events` - frontend/src/components/AgentTab/ToolCard.tsx
- **12**: `feat(frontend): changed files list with status badges` - frontend/src/components/AgentTab/ChangedFiles.tsx
- **13**: `feat(frontend): run history with pagination` - frontend/src/components/AgentTab/RunHistory.tsx
- **14**: `feat(frontend): WebSocket reconnection with backoff` - frontend/src/components/AgentTab/
- **15**: `fix(frontend): wire Agents sidebar nav to Agent tab` - frontend/src/components/DashboardShell.tsx
- **16**: `feat(frontend): final status refresh after WebSocket close` - frontend/src/components/AgentTab/
- **17**: `test: integration tests end-to-end` - backend/tests/, frontend/src/__tests__/

---

## Success Criteria

### Verification Commands
```bash
cd backend && uv run pytest -v           # Expected: all tests pass
cd backend && uv run ruff check .         # Expected: no new errors
cd backend && uv run alembic upgrade head # Expected: migration succeeds
cd frontend && pnpm test                  # Expected: all tests pass
cd frontend && pnpm lint                  # Expected: exit code 0
cd frontend && pnpm build                 # Expected: no type errors
```

### Final Checklist
- [ ] All "Must Have" items present and verified
- [ ] All "Must NOT Have" items absent from codebase
- [ ] All backend tests pass
- [ ] All frontend tests pass
- [ ] Frontend builds without errors
- [ ] Right sidebar removed from DashboardShell
- [ ] Agent tab functional with all sub-features
- [ ] WebSocket reconnects on disconnect
- [ ] File changes API returns per-run data
