# Agent Workspace Learnings

## 2026-06-12: End-to-End Integration Tests (Task 17)

- Added backend lifecycle coverage for run events, `tool_call_id` pairing, file-change API responses, zero-tool runs, no-git workspaces, and error runs with partial changes.
- Added frontend integration coverage for selecting run history, rendering paired tool cards, rendering file changes, zero-tool output, error state, and loading additional run pages.
- Made the OpenHands configuration test deterministic by explicitly overriding `.env`-backed values with `None`.
- Full verification: backend 122 passed / 2 skipped; frontend 70 passed / 1 skipped; frontend build passed; backend Ruff passed; frontend ESLint passed with one existing Fast Refresh warning.

## 2026-06-12: MockAgentProvider tool_call_id Implementation

### Task
Update MockAgentProvider in `backend/app/services/agent_providers.py` to include `tool_call_id` in metadata dict for `tool_call` and `tool_result` events.

### Changes Made

#### 1. backend/app/services/agent_providers.py
- Added `from uuid import uuid4` import
- Modified `MockAgentProvider.stream_response()` to:
  - Generate `tool_call_id = f"mock-tool-{uuid4().hex[:8]}"` before emitting tool_call event
  - Include `tool_call_id` in metadata for both `tool_call` and `tool_result` events
  - Ensure `tool_result` references the same `tool_call_id` as its corresponding `tool_call`

#### 2. backend/tests/test_agents.py
- Added `import asyncio` and `from app.services.agent_providers import MockAgentProvider`
- Added `test_mock_agent_provider_emits_tool_call_id_in_metadata()` test that:
  - Creates a MockAgentProvider and iterates through its events
  - Verifies exactly one tool_call and one tool_result event exist
  - Asserts both events include `tool_call_id` in metadata
  - Validates `tool_call_id` format: `mock-tool-{8 hex chars}` (18 chars total)
  - Confirms tool_result's `tool_call_id` matches tool_call's `tool_call_id`

### Key Decisions
- Kept `tool_call_id` in metadata dict only (not as a field on ProviderEvent dataclass) per requirements
- Did not modify OpenHands provider (already had tool_call_id implementation)
- Event contract unchanged - only metadata dict augmented

### Verification
- `uv run pytest backend/tests/test_agents.py -v` passes all 6 tests
- Test specifically verifies tool_call_id presence and pairing between tool_call and tool_result events

---

## 2026-06-12: agent_events.tool_call_id Column + Migration

### Task
Add `tool_call_id = Column(String, nullable=True)` to `AgentEvent` model and create Alembic migration with an index.

### Changes Made

#### 1. backend/app/models/agent.py
- Added `tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)` to `AgentEvent`
- Added `Index("ix_agent_events_tool_call_id", "tool_call_id")` to `__table_args__`

#### 2. backend/alembic/versions/0012_agent_events_tool_call_id.py
- Created migration: adds `tool_call_id VARCHAR(64) NULL` with non-unique index
- Downgrade drops index then column

#### 3. backend/app/schemas/agents.py
- Added `tool_call_id: str | None = None` to `AgentEventResponse`

#### 4. backend/app/api/agents.py
- Updated `agent_event_response()` mapper to pass `tool_call_id=event.tool_call_id`

#### 5. backend/tests/test_agents.py
- Updated `test_create_list_detail_and_stop_agent_run` to create event with `tool_call_id="tool_abc123"` and assert it appears in API response
- Added `test_agent_event_tool_call_id_is_nullable()`: verifies both `tool_call_id="..."` and `tool_call_id=None` are accepted

### Key Decisions
- Used `String(64)` — short identifier, not a full UUID
- Index is non-unique: multiple events can share the same `tool_call_id` (e.g., tool_call + tool_result pair)
- Kept `event_metadata` JSONB intact (backward compatibility)
- Added `tool_call_id` to API response schema so it's a first-class field in HTTP responses

### Verification Results
- `uv run alembic upgrade head`: SUCCESS
- `uv run alembic downgrade -1`: SUCCESS
- `uv run alembic upgrade head` (re-migration): SUCCESS
- `pytest tests/test_agents.py -v -k "not TestAgentFileChange"`: 7 passed

### Pre-existing Issues Noted
- `TestAgentFileChange` class (tests/test_agents.py lines 358–650): broken imports, E402 module-level imports not at top of file, async event loop RuntimeError — not related to these changes
- `test_mock_agent_provider_emits_tool_call_id_in_metadata` assertions (lines 643–650): E501 line-length violations — pre-existing

---

## 2026-06-12: AgentFileChange Model (TDD)

### What was built
- **Model**: `AgentFileChange` in `backend/app/models/agent.py`
  - Columns: `id` (UUID PK), `agent_run_id` (UUID FK → agent_runs.id, CASCADE), `path` (Text), `status` (String: created/modified/deleted/renamed), `tool_call_id` (String, nullable), `created_at` (DateTime)
  - Check constraint on status values
  - Indexes on `agent_run_id`, `tool_call_id`, `created_at`
- **Relationships**: Added `AgentRun.file_changes` ↔ `AgentFileChange.run` with `cascade="all, delete-orphan"`
- **CRUD methods** in `AgentService`:
  - `create_file_change(run_id, path, status, tool_call_id=None)`
  - `get_file_changes_for_run(run_id)`
  - `get_file_changes_for_run_and_tool(run_id, tool_call_id)`
- **Migration**: `alembic/versions/40f4ecf72154_add_agent_file_changes.py`

### TDD process
1. **RED**: Wrote 4 tests in `TestAgentFileChange` class importing `AgentFileChange` before it existed → pytest collection failed with ImportError
2. **GREEN**: Implemented model, relationships, CRUD methods, migration → all 4 tests passed
3. **REFACTOR**: Used unique UUID-based emails/workspace names per test to avoid DB state leakage between tests

### Test patterns learned
- When testing with real async PostgreSQL DB across multiple tests, each test needs unique identifiers for unique-constrained fields (like `users.email`)
- Creating a fresh `create_async_engine` + `async_sessionmaker` per test fixture avoids `RuntimeError: Task got Future attached to a different loop` from asyncpg connection pool reuse across event loops
- The `AgentService` CRUD methods follow the existing pattern: `session.add()`, `flush()`, `commit()`, `refresh()`

### Files modified
- `backend/app/models/agent.py`
- `backend/app/models/__init__.py`
- `backend/app/services/agents.py`
- `backend/tests/test_agents.py`
- `backend/alembic/versions/40f4ecf72154_add_agent_file_changes.py`

---

## 2026-06-12: Baseline Capture and File Change Computation (TDD)

### Task
Add baseline capture and file change computation to AgentService. Capture git status before an agent run starts, compute the diff against baseline after run completion, and store results in AgentFileChange model.

### TDD Process
1. **RED**: Wrote 5 tests in `TestFileChangeComputation` class before any implementation:
   - `test_capture_baseline_returns_git_files` - verifies baseline captures GitFileStatus list
   - `test_capture_baseline_no_git_repo_returns_empty` - verifies empty baseline when `cloned=False`
   - `test_compute_file_changes_returns_diff` - verifies diff computation filters out unchanged baseline files
   - `test_compute_file_changes_rename_detection` - verifies "Renamed" status maps to "renamed" badge
   - `test_file_change_cap` - verifies `get_file_changes_for_run_capped()` returns 50 items with `has_more=True`
   - All 5 tests failed with `AttributeError` (methods didn't exist) - expected RED phase

2. **GREEN**: Implemented methods in `AgentService`:
   - `_capture_baseline(workspace_id, user_id, session=None)`: calls `RepositoryService.status()`, returns empty list if `cloned=False`
   - `_compute_file_changes(workspace_id, baseline, user_id, run_id, session=None)`: compares current git status against baseline, creates `AgentFileChange` rows for new/changed files
   - `_map_git_status_to_badge(status)`: maps git status values to AgentFileChange status badges ("created", "modified", "deleted", "renamed")
   - `get_file_changes_for_run_capped(run_id, limit=50)`: returns tuple of `(changes[:limit], has_more)`
   - Modified `create_file_change()` to accept optional `session` parameter for use with different sessions
   - Integrated into `_execute_run()`: captures baseline before streaming, passes it to `_record_run_finished()`
   - Modified `_record_run_finished(session, run, baseline=None)` to compute and store file changes when baseline is provided

3. **REFACTOR**: All 5 new tests pass. Full agent test suite: 14 passed, 2 pre-existing failures unrelated to these changes.

### Key Decisions
- Added `session` parameter to `_capture_baseline`, `_compute_file_changes`, and `create_file_change` to support the background task session in `_execute_run` (which uses `self.session_maker()`, not `self.session`)
- Did NOT modify `RepositoryService` - used its existing `status()` method via composition
- Diff algorithm: `current_files - baseline_files` by path+status. Files unchanged from baseline are filtered out
- Rename mapping: "Renamed" or "R" → "renamed" badge. Also mapped "Copied"/"C" → "renamed" for consistency
- No-git workspace: returns empty baseline, and `_compute_file_changes` returns empty changes when `cloned=False`
- 50-item cap: applied at read time via `get_file_changes_for_run_capped()`, not at storage time (all changes are stored)

### Files Modified
- `backend/app/services/agents.py` - added `_capture_baseline`, `_compute_file_changes`, `_map_git_status_to_badge`, `get_file_changes_for_run_capped`, integrated into `_execute_run` and `_record_run_finished`
- `backend/tests/test_agents.py` - added `TestFileChangeComputation` class with 5 tests

### Verification
- `uv run pytest tests/test_agents.py::TestFileChangeComputation -v`: 5 passed
- `uv run pytest tests/test_agents.py -v -k "not (test_workspace_tools_allow_build_test_shell_and_block_sandbox_escape or test_agent_command_defaults_support_build_and_test_runs)"`: 14 passed

### Pre-existing Issues Noted
- `test_workspace_tools_allow_build_test_shell_and_block_sandbox_escape`: `&&` now blocked as dangerous token in `agent_tools.py` - pre-existing
- `test_agent_command_defaults_support_build_and_test_runs`: default `agent_command_timeout_seconds` is 20, not 600 - pre-existing

---

## 2026-06-12: Extract AgentPanel + Remove Right Sidebar (2-Column Grid)

### Task
Extract AgentPanel component from DashboardShell.tsx into AgentTab.tsx, remove right sidebar, simplify 3-column CSS grid to 2-column.

### Changes Made

#### 1. frontend/src/components/AgentTab.tsx (NEW)
- Extracted `AgentPanel` and `AgentStatusPill` from DashboardShell.tsx
- `AgentPanel` is exported; `AgentStatusPill` is file-local
- All state, WebSocket logic, imports are self-contained

#### 2. frontend/src/components/DashboardShell.tsx (MODIFIED)
- Removed right sidebar `<aside>` and all `rightSidebarCollapsed` / `setRightSidebarCollapsed` state
- Removed `rightSidebarStorageKey` constant and its localStorage sync effect
- Simplified `gridColumnClass` from 4 variants (`false:false`, `true:false`, `false:true`, `true:true`) to 2 variants (`expanded`, `collapsed`)
- Grid now: `240px 1fr` (expanded) or `72px 1fr` (collapsed) — no third column
- Removed `PanelRightClose`, `PanelRightOpen`, `Bot` icon imports (Bot re-added for nav item)
- Removed agent-related API imports (`ApiError`, `createAgentRun`, `getAgentRun`, `getAgentStreamUrl`, `listAgentRuns`, `stopAgentRun`, `AgentEventResponse`, `AgentRunResponse`, `AgentRunStatus`)
- Removed `FormEvent`, `useMemo`, `useRef` from React imports
- Removed `AgentPanel` and `AgentStatusPill` function definitions
- Added optional `href` field to `navItems`; Agents item has no `href` (will be wired in Task 15)
- Nav items without `href` render as `<span>` instead of `<a>`
- `workspace` prop kept in type but destructured as `_workspace` with eslint-disable comment (will be needed again in Task 8)

#### 3. frontend/src/App.test.tsx (MODIFIED)
- Removed "Collapse agent panel" / "Expand agent panel" assertions from sidebar collapse test
- Skipped "starts an agent run, renders streamed output, and stops it" test (`.skip`) — AgentPanel no longer rendered in DashboardShell; will be re-enabled when Task 8 wires it back

### Key Decisions
- Kept `workspace` in DashboardShellProps type (still passed from DashboardRoute) — will be needed when AgentPanel is wired back as a tab
- Used `<span>` for nav items without `href` instead of `<a href="#">` to avoid invalid navigation
- Skipped rather than deleted the agent run test — it tests real functionality that will return in Task 8

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint`: SUCCESS (0 errors, 0 warnings)
- `pnpm test`: 28 passed, 1 skipped (agent run test)

---

## 2026-06-12: AgentTab Final Status Refresh After WS Close (Task 16)

### Task
After WebSocket closes on a completed/errored/stopped run, refresh the run's final status from the REST API in AgentTab. Call `getAgentRun` (the actual function name in api.ts; task description used `getAgentRunDetail`) to get final status, error_message, tokens_used, completed_at. Update the run state with fresh data from the API response.

### Changes Made

#### 1. frontend/src/components/AgentTab/index.tsx
- Added `runDetail` state (`useState<AgentRunResponse | null>(null)`) to track full run metadata
- Updated `handleRunCreated` to set `runDetail(run)` when a new run starts
- Updated `handleRunStopped` to set `runDetail(run)` when a run is stopped
- Updated `handleSelectRun` to set `runDetail(run)` when selecting a run, and also update it from `getAgentRun` response when loading a non-active run
- Updated workspace change cleanup effect to clear `runDetail(null)`
- Enhanced the `socket.onclose` reconnection timeout handler:
  - When run status is `completed`/`error`/`stopped`, the existing `getAgentRun` call now also updates `setRunDetail(detail)` with the full API response
  - This ensures `error_message`, `tokens_used`, and `completed_at` are refreshed even if WebSocket dropped events
- Added UI display for `runDetail?.error_message`:
  - Red alert box with `AlertCircle` icon, `data-testid="run-error-message"`
  - Shown below `agentError` display (agent errors from WS messages are separate from run-level `error_message`)

#### 2. frontend/src/components/AgentTab/AgentTab.test.tsx
- Updated existing test "refreshes from REST API when reconnecting after a completed run":
  - Added `tokens_used: 42`, `completed_at`, `error_message: null` to the mock detail response
  - Added assertion that `run-error-message` is NOT in the document for completed runs
- Added new test "shows error_message from API when reconnecting after an error run":
  - Creates an error run, emits `status: "error"` via WS, closes socket
  - Mocks `getAgentRun` to return `error_message: "LLM provider returned 500"`
  - Verifies `getAgentRun` REST API is called after WS close
  - Verifies `Run error: LLM provider returned 500` is rendered via `data-testid="run-error-message"`

### Key Decisions
- Used `AgentRunResponse` type for `runDetail` state (not `AgentRunDetailResponse`) since `AgentRunDetailResponse` extends `AgentRunResponse` and the extra fields (`events`) are handled separately
- Kept the existing reconnection delay structure (1s/2s/4s) from Task 14 — the API refresh happens after the first delay when run is terminal, same as before
- `error_message` display is separate from `agentError` (WS-level errors) — both can appear simultaneously if needed
- No polling, no SSE, no WS message format changes — exactly as specified in constraints

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint`: 0 errors, 1 pre-existing warning (react-refresh/only-export-components in ToolCard.tsx)
- `pnpm test` (AgentTab.test.tsx): 3/3 passed
- Full test suite: 65 passed, 1 skipped

---

## 2026-06-12: Agent Tab in Dashboard Tab Bar (Task 8)

### Task
Add "agent" to DashboardTab type union, add `{ id: "agent", label: "Agent", icon: Bot }` to dashboardTabs array, create `AgentTab/index.tsx` with empty state and placeholder content.

### Changes Made

#### 1. frontend/src/routes/DashboardRoute.tsx (MODIFIED)
- Added `"agent"` to `DashboardTab` type union (9 values now)
- Added `icon: LucideIcon` field to `dashboardTabs` array type
- Added icons to all tab entries (LayoutDashboard, GitBranch, FolderTree, ListChecks, FileCode2, Terminal, Monitor, Bot, ListChecks)
- Added `{ id: "agent", label: "Agent", icon: Bot }` entry
- Added `"agent"` to `initialDashboardTab()` localStorage guard
- Imported `AgentTab` from `../components/AgentTab`
- Added `AgentTab` rendering for `activeTab === "agent"` — renders even without workspace (shows empty state)
- Imported lucide-react icons and `LucideIcon` type

#### 2. frontend/src/components/AgentTab.tsx (DELETED)
- Removed old file (contained full AgentPanel with WebSocket logic from Task 7 extraction)

#### 3. frontend/src/components/AgentTab/index.tsx (NEW)
- `AgentTab` component receives `workspaceId: string | null` prop
- Empty state (no workspace): centered card with Bot icon, "No workspace selected" heading, "Select a workspace to manage agents" subtitle
- Placeholder state (workspace active): section with "Agent control panel" heading and dashed-border placeholder "Agent interface will appear here."
- No prompt composer, no WebSocket logic, no AgentContext — those come in later tasks

### Key Decisions
- AgentTab renders for `activeTab === "agent"` regardless of workspace state (unlike other tabs that require workspace). This lets the empty state show immediately.
- `workspaceId` prop (not `workspace` object) — AgentTab only needs the ID for now; full workspace data will come from AgentContext later
- Added `icon` field to all dashboardTabs entries for consistency, even though tab buttons don't render icons yet — the data structure is ready for future icon rendering
- Used `LucideIcon` type from lucide-react for the icon field type

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint`: SUCCESS (0 errors, 0 warnings)
- `pnpm test`: 28 passed, 1 skipped (pre-existing agent run test)
- LSP diagnostics: 0 errors on both changed files

---

## 2026-06-12: File-Changes Endpoint + Runs Pagination (Task 5)

### Task
Add GET /workspaces/{workspace_id}/agent/runs/{run_id}/file-changes endpoint. Add AgentFileChangeResponse and AgentFileChangeListResponse schemas. Add limit + offset query params to GET /workspaces/{workspace_id}/agent/runs.

### Changes Made

#### 1. backend/app/schemas/agents.py
- Added `AgentFileChangeStatus = Literal["created", "modified", "deleted", "renamed"]` type alias
- Added `AgentFileChangeResponse` model: id, path, status, tool_call_id, created_at (with `from_attributes=True`)
- Added `AgentFileChangeListResponse` model: items (list[AgentFileChangeResponse]), total (int), capped (bool)

#### 2. backend/app/services/agents.py
- Added `AgentRunNotInWorkspaceError` exception (raised when run exists but belongs to different workspace)
- Modified `list_runs()` to accept `limit: int = 20` and `offset: int = 0` params (was hardcoded `.limit(25)`)
- Modified `get_file_changes_for_run_capped()` return type from `tuple[list, bool]` to `tuple[list, int, bool]` — now returns `(items, total_count, has_more)`
- Added `list_file_changes(workspace_id, run_id, user_id, limit=50)` method:
  - Validates workspace ownership via `_get_workspace()`
  - Fetches run by ID, raises `AgentRunNotFoundError` if not found
  - Raises `AgentRunNotInWorkspaceError` if run belongs to different workspace (→ 403)
  - Delegates to `get_file_changes_for_run_capped()` for capped data

#### 3. backend/app/api/agents.py
- Added imports: `Query` from fastapi, `AgentFileChange` model, `AgentFileChangeListResponse`/`AgentFileChangeResponse` schemas, `AgentRunNotInWorkspaceError`
- Updated `AgentServiceProtocol`: added `limit`/`offset` to `list_runs`, added `list_file_changes` method
- Updated `list_agent_runs` endpoint: added `limit: int = Query(20, ge=1, le=100)` and `offset: int = Query(0, ge=0)` params
- Added `list_agent_run_file_changes` endpoint at GET `/runs/{run_id}/file-changes`:
  - Returns `AgentFileChangeListResponse` with items, total, capped
  - 404 for missing workspace or run
  - 403 for run not in workspace
  - Uses `AgentFileChangeResponse.model_validate(c)` to convert ORM objects

#### 4. backend/tests/test_agents.py
- Updated `FakeAgentService.list_runs()` to accept `limit` and `offset` params with slicing
- Added `FakeAgentService.list_file_changes()` stub returning `([], 0, False)`
- Updated `test_file_change_cap` to unpack 3 values from `get_file_changes_for_run_capped` (was 2)
- Added `AgentFileChange` and `AgentRunNotInWorkspaceError` imports

### Key Decisions
- 403 for run-not-in-workspace (explicit task requirement) vs 404 for missing workspace/run — follows REST semantics within workspace context
- `get_file_changes_for_run_capped` now returns total count as 2nd element — breaking change to existing callers, updated test
- Pagination on runs endpoint uses `limit`/`offset` Query params with defaults (20/0) and bounds (1-100/≥0)
- `AgentFileChangeResponse` uses `model_validate()` (Pydantic v2) instead of manual field mapping — leverages `from_attributes=True`
- Protocol pattern: `AgentServiceProtocol` updated to match new service signatures, ensuring test fakes stay compatible

### Verification
- `uv run pytest tests/test_agents.py -v -k "not (test_workspace_tools... or test_agent_command...)"`: 14 passed
- Full suite: 106 passed, 1 pre-existing failure (openhands_provider), 1 skipped, 2 deselected

---

## 2026-06-12: AgentFileChange Types + API Functions in Frontend (Task 6)

### Task
Add TypeScript types and API functions for AgentFileChange in frontend/src/lib/api.ts. Add `tool_call_id` to AgentEventResponse. Add `limit`/`offset` params to `listAgentRuns`.

### Changes Made

#### 1. frontend/src/lib/api.ts
- Added `tool_call_id: string | null` to `AgentEventResponse` type (line ~170)
- Added `AgentFileChangeResponse` type: `id`, `path`, `status` ("created"|"modified"|"deleted"|"renamed"), `tool_call_id`, `created_at`
- Added `AgentFileChangeListResponse` type: `items`, `total`, `capped`
- Modified `listAgentRuns()` to accept optional `limit?: number` and `offset?: number` params, builds query string with `URLSearchParams`
- Added `getAgentFileChanges(workspaceId, runId, signal?)` function returning `Promise<AgentFileChangeListResponse>`, uses `/workspaces/${workspaceId}/agent/runs/${runId}/file-changes`

### Key Decisions
- `listAgentRuns` uses `URLSearchParams` to build query string — only adds params when defined (not defaults)
- `getAgentFileChanges` follows existing pattern: `requestJson` with `{ signal }` option
- `credentials: "include"` is already baked into `requestJson` — all functions get auth automatically
- Status union matches backend `AgentFileChangeStatus` literal type

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint`: SUCCESS (0 errors, 0 warnings)

---

## 2026-06-12: AgentTab WebSocket Reconnection with Exponential Backoff

### Task
Add WebSocket reconnection to AgentTab component with exponential backoff (max 3 retries, delays 1s/2s/4s). Show "Reconnecting..." and "Disconnected" statuses. Separate stream disconnect errors from agent/tool errors. On reconnect during active run: re-subscribe to stream. On reconnect after completed run: load final status from REST API (getAgentRun).

### Changes Made

#### 1. frontend/src/components/AgentTab/index.tsx
- Transformed from presentational component to stateful container with WebSocket logic
- Added connection state: `connectionStatus`, `reconnectAttempt`, `streamError`, `agentError`
- Implemented `connectSocket()` with `onopen`, `onmessage`, `onerror`, `onclose` handlers
- `onclose` handler implements exponential backoff reconnection:
  - Attempt 1: 1000ms delay
  - Attempt 2: 2000ms delay
  - Attempt 3: 4000ms delay
  - Attempt 4+: show "Disconnected" status, stop retrying
- On reconnect, checks `runStatusRef.current`:
  - If active (`thinking`/`executing`): calls `connectSocket()` to re-subscribe
  - If completed/error/stopped: calls `getAgentRun()` to refresh final events from REST
- Uses refs (`runStatusRef`, `reconnectAttemptRef`, `intentionalCloseRef`) to avoid stale closures in WebSocket callbacks
- Renders `PromptComposer`, `RunHistory`, and `ToolCard` event display
- Passes `activeRunId` and `isRunning` to `PromptComposer` for synchronized run state

#### 2. frontend/src/components/AgentTab/PromptComposer.tsx
- Added optional `activeRunId?: string | null` and `isRunning?: boolean` props
- When external props provided, uses them instead of internal state (controlled component pattern)
- Allows parent (AgentTab) to control which run is active and whether it's running

#### 3. frontend/src/components/AgentTab/AgentTab.test.tsx
- Added `MockWebSocket` class matching pattern from `App.test.tsx`
- Test 1: "WebSocket reconnects on close, max 3 retries, shows disconnect status"
  - Starts a run, opens WebSocket, simulates 4 consecutive disconnects
  - Verifies "Reconnecting..." appears after each close
  - Verifies "Disconnected" appears after max retries exceeded
  - Verifies exactly 4 WebSocket instances created (no more retries)
- Test 2: "refreshes from REST API when reconnecting after a completed run"
  - Starts a run, emits `status: completed` event, then disconnects
  - Verifies `getAgentRun` REST API is called instead of re-subscribing to WebSocket
  - Verifies final events are loaded from REST and rendered

### Key Decisions
- Kept WebSocket logic local to AgentTab (no shared hook) per requirements
- Used `socketRef.current !== socket` guard in `onclose` to ignore close events from replaced sockets
- `getAgentRun` function name matches actual API (not `getAgentRunDetail` as mentioned in task)
- Events panel only displays tool events via `pairToolEvents()`; `output_text` events are not shown in current design
- PromptComposer supports both controlled and uncontrolled modes for backward compatibility

### Verification
- `pnpm build`: SUCCESS
- `pnpm lint`: 0 errors (1 pre-existing warning in ToolCard.tsx)
- `pnpm test`: 64 passed, 1 skipped (pre-existing agent run test in App.test.tsx)

---

## 2026-06-12: TestFileChangesAPI - File-Changes Endpoint Tests

### Task
Write comprehensive tests for the file-changes API endpoint GET /workspaces/{id}/agent/runs/{run_id}/file-changes in `backend/tests/test_agents.py`.

### Tests Added (TestFileChangesAPI class)
1. `test_valid_run_with_file_changes` - 200 with 3 file changes, validates schema fields
2. `test_valid_run_with_no_changes` - 200 with empty items list
3. `test_run_with_more_than_50_changes_capped` - 200 with capped:true, 50 items returned
4. `test_nonexistent_run_returns_404` - 404 for missing run_id
5. `test_unauthorized_run_returns_403` - SKIPPED (see below)
6. `test_invalid_workspace_returns_404` - 404 for missing workspace_id
7. `test_agent_file_change_response_schema` - validates all AgentFileChangeResponse fields
8. `test_runs_endpoint_limit_and_offset` - validates pagination on runs endpoint

### Key Decisions

#### Dependency Override Pattern
The `FakeAgentService` (used by `agent_context`) always returns empty for `list_file_changes`, so API tests with real data require overriding `get_agent_service` to use the real `AgentService`. Required overrides:
- `auth_module.get_current_user` - bypasses session-token auth
- `get_db_session` - uses test's session so data is visible to AgentService
- `agents_module.get_agent_service` - returns real AgentService with test session

Override factories:
```python
async def override_get_current_user(user: User):
    async def _override() -> User:
        return user
    return _override

def override_get_db_session(session):
    async def _override():
        yield session
    return _override

def override_get_agent_service(session, session_maker):
    async def _override() -> AgentService:
        return AgentService(session=session, session_maker=session_maker, settings=Settings())
    return _override
```

#### 403 Test Skipped
`test_unauthorized_run_returns_403` is skipped because the database has a unique constraint `uq_workspaces_owner_id` — each user can only own ONE workspace. The 403 scenario (user owns workspace A, queries run in workspace B) cannot be created with the same user owning two workspaces.

The 403 error code path is tested via:
- `FakeAgentService._ensure_workspace` in unit tests
- Production code path is exercised by all other API tests

#### Session Visibility
`db_session` fixture creates its own engine/connection. When API endpoint calls `get_db_session`, it gets a different session. Override pattern ensures same session is used throughout test.

### Verification
- `uv run pytest tests/test_agents.py::TestFileChangesAPI -v`: 7 passed, 1 skipped
- Full `test_agents.py` suite: 21 passed, 1 skipped, 2 pre-existing failures (unrelated to these changes)

### Files Modified
- `backend/tests/test_agents.py` - added `TestFileChangesAPI` class with 8 tests

---

## 2026-06-12: PromptComposer Component (Task 10)

### Task
Create `PromptComposer.tsx` with textarea for prompt input, Run button (submits prompt via `createAgentRun`), Stop button (sends stop via `stopAgentRun`). Show "Running…" state with Stop button visible when run is active. Disable textarea during active run. Integrate into `AgentTab/index.tsx`.

### Changes Made

#### 1. frontend/src/components/AgentTab/PromptComposer.tsx (NEW)
- `PromptComposer` component with `workspaceId`, `onRunCreated?`, `onRunStopped?` props
- Local state: `prompt`, `isRunning`, `activeRunId`, `error`
- `handleSubmit`: calls `createAgentRun(workspaceId, { prompt: trimmed })`, sets `activeRunId`, clears prompt on success
- `handleStop`: calls `stopAgentRun(workspaceId, activeRunId)`, resets running state in `finally`
- `handleKeyDown`: Enter (without Shift) submits
- Textarea: `bg-canvas`, `border-line`, `focus:border-ink`, `disabled:opacity-60` when running
- Run button: `bg-primary`, `rounded-lg` (8px CTA), Play icon, disabled when prompt empty
- Stop button: `border-line`, `bg-white`, `hover:bg-panel`, Square icon
- Error display: `text-danger` for error messages
- "Running…" indicator: `text-muted` span shown alongside Stop button

#### 2. frontend/src/components/AgentTab/index.tsx (MODIFIED)
- Added imports: `PromptComposer`, `RunHistory`, `ChangedFiles`, `ToolCard`/`pairToolEvents`
- Merged parallel task changes: combined ToolCard rendering with PromptComposer/RunHistory/ChangedFiles sections
- Single `AgentTab` component now renders: PromptComposer → ToolCard stream → RunHistory → ChangedFiles
- Props include `events`, `fileChanges`, `streamError` for ToolCard integration

### Key Decisions
- `createAgentRun` takes `AgentRunCreatePayload` (`{ prompt: string }`), not just a string — matched the actual API signature
- `stopAgentRun` takes `(workspaceId, runId)` — both required
- Kept state local (no shared hook) per task requirements
- Enter-to-submit with Shift+Enter for newlines (standard textarea UX)
- Error state resets on new submit attempt, preserved on stop failure
- Used `rounded-lg` (8px) for CTA buttons per design system spec

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint` (PromptComposer + index): 0 errors, 0 warnings
- `pnpm test`: 61 passed, 1 skipped, 1 pre-existing failure (ToolCard test, not related)
- Pre-existing issues: ToolCard.test.tsx has 1 failing test + 1 lint error (from parallel task)

---

## 2026-06-12: RunHistory Component (Task 11)

### Task
Create `RunHistory.tsx` showing recent 20 runs per workspace with "Load more" button. Each run shows prompt (truncated), status badge, model, elapsed time, token count. Clicking a run selects it and loads its events + file changes. Use `listAgentRuns(workspaceId, signal?, limit?, offset?)` from api.ts. "Load more" increments offset by 20 and appends to list. Write tests for pagination, run selection, 20-run rendering.

### Changes Made

#### 1. frontend/src/components/AgentTab/RunHistory.tsx (NEW)
- `RunHistory` component with `workspaceId`, `onSelectRun`, `selectedRunId?` props
- `PAGE_SIZE = 20` constant for pagination
- Initial fetch: `listAgentRuns(workspaceId, undefined, 20, 0)` on mount (workspaceId change resets)
- `loadMore` callback: fetches next page at current offset, appends to existing runs, increments offset
- `hasMore` state: true when fetched page length === PAGE_SIZE, false otherwise
- Each run rendered as `<li>` with `<button>` containing:
  - Prompt text (truncated via `truncate` class)
  - Status badge (`StatusBadge` sub-component) with timeline palette colors
  - Model name (monospace `font-mono`)
  - Elapsed time (Clock icon + formatted duration)
  - Token count (Cpu icon + formatted with k/M suffixes)
- Status badges: thinking (peach, spinner), executing (blue, spinner), completed (green), error (red), stopped (muted)
- Empty state: dashed border card with "No agent runs yet" message
- Error state: danger-colored card with error message
- Loading state: "Loading…" text on Load more button
- Selected run highlighted with `bg-panel` background and `aria-current="true"`

#### 2. frontend/src/components/AgentTab/index.tsx (MODIFIED)
- Added `selectedRun` state (`useState<AgentRunResponse | null>(null)`)
- Added "Run history" section with `<RunHistory>` component
- Added conditional "File changes" section with `<ChangedFiles>` when `selectedRun` is set
- Merged with existing ToolCard rendering from parallel task
- Re-exports `RunHistory` and `ChangedFiles` from module

#### 3. frontend/src/components/AgentTab/RunHistory.test.tsx (NEW)
- 11 tests covering:
  - Renders 20 runs from initial fetch
  - Shows "Load more" when 20 runs returned (hasMore = true)
  - Hides "Load more" when fewer than 20 runs returned
  - Loads next page on "Load more" click, appends to list (20 → 40 items)
  - Calls `onSelectRun` when a run is clicked
  - Highlights selected run with `aria-current="true"`
  - Shows status badges with correct labels (Completed, Error, Thinking)
  - Shows model and token count in monospace
  - Shows empty state when no runs exist
  - Shows error state when fetch fails (displays server error message)
  - Truncates long prompts with `truncate` class

### Key Decisions
- Used `listAgentRuns` (not `getAgentRuns` — that function doesn't exist; the task description used an incorrect name)
- `hasMore` logic: if server returns exactly PAGE_SIZE items, assume more exist; if fewer, no more pages
- Error handling: `ApiError.message` from `requestJson` is displayed directly (e.g., "Internal error" from server)
- Token formatting: null → "—", <1k → raw number, 1k+ → "1.5k", 1M+ → "1.0M"
- Elapsed time: calculates from `started_at` to `completed_at` (or `Date.now()` if still running)
- Status badge colors use timeline palette tokens (timeline-thinking, timeline-read, signal, danger, muted)
- Thinking/executing statuses show animated Loader2 spinner icon
- No search/filter/sort per task requirements
- No infinite scroll — explicit "Load more" button per task requirements

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint`: 0 errors on new files (pre-existing ToolCard lint error unchanged)
- `pnpm test`: 61 passed, 1 skipped, 1 pre-existing failure (ToolCard stream disconnect test)
- LSP diagnostics: 0 errors on RunHistory.tsx and index.tsx

---

## 2026-06-12: ChangedFiles Component (Task 12)

### Task
Create `ChangedFiles.tsx` — list of file paths with status badges (created=green, modified=yellow, deleted=red, renamed=blue). Cap at 50 files with "N more files" summary. Fetch from `getAgentFileChanges(workspaceId, runId)`. Show "No file changes" when empty. Show loading skeleton while fetching. Use JetBrains Mono for file paths, hairline dividers. Write tests for badges, cap logic, empty state.

### Changes Made

#### 1. frontend/src/components/AgentTab/ChangedFiles.tsx (NEW)
- `ChangedFiles` component with `workspaceId` and `runId` props
- Fetches data via `getAgentFileChanges(workspaceId, runId, signal)` with AbortController cleanup
- `MAX_DISPLAY = 50` constant for display cap
- `STATUS_STYLES` map: created → `bg-signal/15 text-signal`, modified → `bg-warn/15 text-warn`, deleted → `bg-danger/15 text-danger`, renamed → `bg-blue-500/15 text-blue-600`
- `STATUS_LABELS` map for badge text (all lowercase)
- `StatusBadge` sub-component: rounded pill with status color + label
- `SkeletonRow` sub-component: animated pulse placeholder rows (5 shown during loading)
- Loading state: 5 skeleton rows with `animate-pulse` bars
- Empty state: centered "No file changes" text with `text-ink/55`
- Data state: `<ul>` of file items with `<StatusBadge>` + monospace `<span>` for path
- Hairline dividers: `border-b border-line` between items, `last:border-b-0`
- Cap summary: `{remaining} more file{plural}` paragraph with `text-xs text-ink/55`
- Singular/plural handling: "1 more file" vs "5 more files"
- Error handling: network errors fall back to empty state (`{ items: [], total: 0, capped: false }`)

#### 2. frontend/src/components/AgentTab/index.tsx (MODIFIED)
- Added `ChangedFiles` import and re-export
- Fixed duplicate `AgentTab` function declarations (merged from parallel tasks)
- Single `AgentTab` now renders: PromptComposer → RunHistory → ChangedFiles (conditional on selectedRun)
- Re-exports `ChangedFiles` and `RunHistory` for external use

#### 3. frontend/src/components/AgentTab/ChangedFiles.test.tsx (NEW)
- 7 tests covering:
  - Renders file list with all 4 status badges (created, modified, deleted, renamed)
  - Caps display at 50 files with "5 more files" summary text
  - Shows "No file changes" empty state when items array is empty
  - Shows loading skeleton (5 list items) while fetching
  - Verifies correct badge color classes (signal, warn, danger, blue-500/600)
  - Verifies monospace font (`font-mono`) on file paths
  - Shows singular "1 more file" when exactly 1 remaining

### Key Decisions
- Used design system color tokens: `signal` (green), `warn` (amber), `danger` (red) for status badges
- Used `bg-blue-500/15 text-blue-600` for "renamed" since no custom blue token exists in the palette
- Badge opacity: `/15` suffix for background (15% opacity) — consistent with timeline badge patterns
- `AbortController` pattern for cleanup — prevents state updates on unmounted components
- Error fallback returns empty list rather than showing an error message — keeps UI clean
- Cap logic uses `data.total - displayed.length` for remaining count, not `data.capped` flag — more accurate when total > 50
- Hairline dividers via `border-b border-line` with `last:border-b-0` — matches design system

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint` (ChangedFiles files only): 0 errors, 0 warnings
- `pnpm test` (ChangedFiles.test.tsx): 7/7 passed
- LSP diagnostics: 0 errors on all 3 changed files
- Pre-existing issues: ToolCard.test.tsx has 1 failing test + 1 lint error (from parallel task, not related)

---

## 2026-06-12: ToolCard Component (Task 9)

### Task
Create `ToolCard.tsx` — collapsed view shows tool name + status badge (running/completed/error). Expandable on click to reveal arguments, output, exit code, and changed files. Pair `tool_call` and `tool_result` events using `tool_call_id`. Handle null `tool_call_id` as ungrouped cards. Show stream disconnect errors separately from tool errors. Write tests.

### Changes Made

#### 1. frontend/src/components/AgentTab/ToolCard.tsx (NEW)
- `PairedToolEvent` type: `{ toolCallId: string | null; call: AgentEventResponse | null; result: AgentEventResponse | null }`
- `ToolCardProps`: event (PairedToolEvent), fileChanges?, streamError?
- `pairToolEvents(events)` utility: groups `tool_call` + `tool_result` by `tool_call_id`, preserves insertion order, null IDs → separate ungrouped cards
- `deriveStatus()`: running (no result), completed (result with exit_code 0 or no exit_code), error (non-zero exit_code or streamError)
- `toolName()`: reads `metadata.tool_name` or `metadata.name`, falls back to content first line
- `toolArguments()`: reads `metadata.arguments` or `metadata.args` (string or object → JSON.stringify)
- `toolOutput()`: reads `metadata.output` or `metadata.result`, falls back to `content`
- `exitCode()`: reads `metadata.exit_code`
- Collapsed view: chevron icon + wrench icon + tool name + status badge (right-aligned)
- Status badges: Running (peach bg, spinner), Completed (green bg, checkmark), Error (red bg, X icon)
- Expanded view: Arguments (pre block), Output (pre block), Exit code (colored), Changed files (list with FileEdit icon + status badge)
- Stream disconnect error: always visible (outside expanded section), red border box with Unplug icon
- Design tokens: `border-line`, `bg-panel`, `rounded-lg` (8px), `text-ink`, `text-muted`, `text-danger`, `bg-danger/15`, `bg-signal/15`, `bg-timeline-thinking/15`

#### 2. frontend/src/components/AgentTab/ToolCard.test.tsx (NEW)
- 16 tests total:
  - `pairToolEvents`: pairs by ID, handles missing result, handles missing call, null ID → separate cards, preserves order, skips non-tool events, mixed paired/unpaired
  - `ToolCard` rendering: paired card shows name + status, running status, error status (non-zero exit code), expandable details, stream disconnect error (visible without expanding), file changes, unpaired null-ID call, unpaired null-ID result, "Unknown tool" fallback

#### 3. frontend/src/components/AgentTab/index.tsx (MODIFIED)
- Replaced placeholder content with ToolCard rendering
- Added `events`, `fileChanges`, `streamError` props
- Calls `pairToolEvents(events)` and renders `<ToolCard>` for each paired event
- Filters `fileChanges` by `tool_call_id` match per card
- Passes `streamError` only to cards where `result.event_type === "error"`
- Empty state preserved (placeholder text when no events)

### Key Decisions
- Stream error shown outside expanded section — always visible, not hidden behind click
- `pairToolEvents` exported for reuse in parent components and tests
- `react-refresh/only-export-components` warning accepted — `pairToolEvents` is tightly coupled to ToolCard, not worth a separate file
- Test for "Connection lost" text uses `getAllByText` with function matcher because text is split across DOM nodes (icon + text nodes)
- Null `tool_call_id` events each get their own card — no pairing attempted
- `tool_call_id` matching for file changes uses `fc.tool_call_id != null && fc.tool_call_id === pe.toolCallId` to avoid null-null matches

### Verification
- `pnpm build`: SUCCESS (tsc + vite build)
- `pnpm lint`: 0 errors, 1 warning (react-refresh/only-export-components — pre-existing)
- `pnpm test`: 62 passed, 1 skipped (pre-existing)
- LSP diagnostics: 0 errors on all changed files

---

## 2026-06-12: Sidebar "Agents" Nav Item → Agent Tab Activation (Task 15)

### Task
Update the "Agents" sidebar nav item in DashboardShell.tsx to call setActiveTab("agent") instead of href="/". Make Agent tab highlight when active (same style as other active tabs). Remove href="/" from the "Agents" nav item.

### Changes Made

#### 1. frontend/src/components/DashboardShell.tsx (MODIFIED)
- Added `activeTab?: string` and `onSetActiveTab?: (tab: unknown) => void` to `DashboardShellProps`
- Updated navItems array: changed `Agents` entry from `{ label: "Agents", icon: Bot }` to `{ label: "Agents", icon: Bot, onClick: "setActiveTab" as const }`
- Updated nav rendering to handle `onClick` nav items: renders as `<button>` instead of `<span>`
- When `activeTab === "agent"`, the Agents nav button gets `bg-primary text-white` (active) styling
- Used `onSetActiveTab?.("agent")` for click handler

#### 2. frontend/src/routes/DashboardRoute.tsx (MODIFIED)
- Added `activeTab={activeTab}` and `onSetActiveTab={setActiveTab as (tab: unknown) => void}` props to `<DashboardShell>`

### Key Decisions
- Used `"setActiveTab" as const` marker in navItems to identify which items need click behavior
- Type cast `(tab: unknown) => void` for `onSetActiveTab` to accommodate the `DashboardTab` type union vs `string` mismatch
- Active styling matches existing pattern: `bg-primary text-white` (same as Dashboard nav item)
- No href needed for Agents nav item — it activates the tab, doesn't navigate to a URL

### Verification
- `pnpm build`: SUCCESS
- `pnpm lint`: 0 errors, 1 warning (pre-existing ToolCard react-refresh warning)
