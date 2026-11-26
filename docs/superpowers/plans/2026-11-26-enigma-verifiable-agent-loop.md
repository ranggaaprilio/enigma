# Enigma — Verifiable Agent Loop: Backend Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend foundation of the Verifiable Agent Loop — the OpenSandbox runtime adapter plus the verification loop (evidence store, test runner, observation, orchestrator) — as independently-testable units.

**Architecture:** The loop runs inside the existing `SandboxRuntime` abstraction. A new `opensandbox` provider implements `SandboxRuntime` (replacing the homegrown Docker provider over time). A new `VerificationOrchestrator` drives `test → observe → iterate`, persisting evidence in new `verification_run`/`verification_evidence` models. All execution stays behind `SandboxRuntime` — no new runtime.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, asyncpg, alembic, pytest-asyncio, structlog.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md`

## Global Constraints

- Backend is **async-only**: every DB/service call must be `await`-ed (`create_async_engine` + `async_session_maker`).
- All DB access uses async SQLAlchemy with `expire_on_commit=False`.
- Errors surfaced to consumers must be `SandboxRuntimeError` subclasses; messages must never embed provider URLs or credentials.
- No module-level dict of in-flight run state; `tests/test_run_runtime.py` greps for it — use `RunRegistry`/services.
- New `SandboxRuntime` provider gets its own module in `app/services/sandbox/` and is selected via `Workspace.runtime` in `factory.py`.
- Provider conformance is proven by adding a case to `tests/sandbox/test_conformance.py` and passing `tests/sandbox/test_boundary.py`.
- Migrations must be expand-only; after `0039`, any drop/rename/retype in `upgrade()` needs `# contract: <why>`.

## Review Focus

- **Unknown provider name** in `factory.get_runtime_by_name` → raise `RuntimeUnavailableError`, never a KeyError (the dispatcher catches it). Pinned in Task 1.
- **Evidence tied to a deleted lane/run** → FK integrity; a run's evidence must be queryable for the human gate even after the workspace sandbox is gone. Pinned in Task 2.
- **Test fail vs environment error** → a non-zero exit code is *test failure* (evidence status `failed`), while `CommandTimeoutError`/`SandboxRuntimeError` is *infrastructure error* (`error`). Pinned in Task 3.
- **Observation when preview unavailable** → must not abort the loop; record `observation_failed` and continue on test evidence. Pinned in Task 4.
- **Orchestrator concurrency guard** → obey the existing one-active-run-per-workspace guard; never start a loop when a run is active. Pinned in Task 5.

---

### Task 1: OpenSandbox runtime adapter

**Files:**
- Create: `backend/app/services/sandbox/opensandbox.py`
- Modify: `backend/app/services/sandbox/factory.py` (add to `_FACTORIES`)
- Modify: `backend/tests/sandbox/test_conformance.py` (add `"opensandbox"` case)
- Test: `backend/tests/sandbox/test_opensandbox.py`

**Interfaces:**
- Produces: `OpenSandboxRuntime` implementing `SandboxRuntime` (protocol in `base.py`): `provider="opensandbox"`, `configured`, `health`, `provision`, `status`, `start`, `delete`, `exec`, `stream_command`, `create_or_connect_pty`, `kill_pty`, `preview_link`, `expose_port`, `fs`, `close`.

- [ ] **Step 1: Write the failing conformance case**

Add `"opensandbox"` to the `case` fixture in `test_conformance.py`, backed by a fake (mirror `FakeSandboxRuntime`/`docker_runtime_with` pattern) so the contract proves without a live daemon.

```python
if request.param == "opensandbox":
    runtime = OpenSandboxRuntime(Settings(opensandbox_api_url="http://127.0.0.1:8080"))
    return runtime, make_workspace("osb-1", runtime="opensandbox")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/sandbox/test_conformance.py -v`
Expected: FAIL with `ImportError: cannot import name 'OpenSandboxRuntime'`.

- [ ] **Step 3: Implement `OpenSandboxRuntime` in `opensandbox.py`**

Implement the `SandboxRuntime` protocol by mapping to the OpenSandbox Python SDK (`opensandbox` package) per the spike report:
- `exec` → `commands.run()` with `opts.timeout`; return `RuntimeCommandResult(exit_code, stdout, stderr)`.
- `stream_command` → `commands.run(handlers=ExecutionHandlers(on_stdout=…, on_stderr=…))`.
- `fs` → wrap `FilesystemAdapter`; **implement `find_files` as content search via `exec("grep -r <pattern> <path>")`**, since `search()` is glob-based, not grep.
- `preview_link` → `get_endpoint(port)` returning URL + empty token (host-mapped proxy).
- `expose_port` → call `get_endpoint(port)` idempotently; never raise for unexposable port.
- Map `SandboxException` family → `SandboxRuntimeError` subclasses.
- Config via `Settings` (add `opensandbox_api_url`, `opensandbox_api_key` to config in a later step if needed; for this task read from `Settings` attrs that exist — add minimal settings fields in `core/config.py`).

- [ ] **Step 4: Add `"opensandbox"` to `_FACTORIES` in `factory.py`**

```python
_FACTORIES: dict[str, Callable[[Settings], SandboxRuntime]] = {
    "daytona": DaytonaRuntime,
    "docker": DockerRuntime,
    "opensandbox": OpenSandboxRuntime,
}
```

- [ ] **Step 5: Run conformance + boundary to verify they pass**

Run: `uv run pytest tests/sandbox/test_conformance.py tests/sandbox/test_boundary.py -v`
Expected: PASS (all providers incl. `opensandbox`).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sandbox/opensandbox.py backend/app/services/sandbox/factory.py backend/tests/sandbox/test_conformance.py
git commit -m "feat(sandbox): add OpenSandbox runtime provider"
```

---

### Task 2: Evidence store models

**Files:**
- Create: `backend/app/models/verification.py`
- Create: `backend/alembic/versions/0040_verification_evidence.py`
- Create: `backend/tests/test_verification_models.py`
- Modify: `backend/app/models/__init__.py` (re-export)

**Interfaces:**
- Produces:
  - `VerificationRun` ORM (id, workspace_id FK, plan_ref, status: `planning|testing|observing|review|approved|rejected|error`, created_at, updated_at).
  - `VerificationEvidence` ORM (id, run_id FK, kind: `test|observation|screenshot`, status: `passed|failed|observation_failed|error`, detail JSON, path, created_at).

- [ ] **Step 1: Write the failing model test**

```python
async def test_verification_run_and_evidence_round_trip(async_session):
    run = VerificationRun(workspace_id=w.id, plan_ref="spec:1", status="testing")
    async_session.add(run); await async_session.flush()
    async_session.add(VerificationEvidence(run_id=run.id, kind="test", status="passed", detail={"exit_code": 0}))
    await async_session.commit()
    assert run.evidence[0].kind == "test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_verification_models.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement models in `verification.py`**

Follow existing ORM patterns (`models/workspace.py` style): async SQLAlchemy declarative models, `Mapped`/`mapped_column`, relationship `VerificationRun.evidence`, `ForeignKey` to `workspaces.id`.

- [ ] **Step 4: Create alembic migration `0040_verification_evidence`**

```bash
uv run alembic revision -m "verification evidence"
```
Populate `upgrade()`/`downgrade()` for the two tables. Expand-only; no drop/rename of existing columns.

- [ ] **Step 5: Run migration + test**

Run: `uv run alembic upgrade head && uv run pytest tests/test_verification_models.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/verification.py backend/alembic/versions/0040_verification_evidence.py backend/tests/test_verification_models.py
git commit -m "feat(verification): add verification run and evidence models"
```

---

### Task 3: Test runner adapter

**Files:**
- Create: `backend/app/services/verification/test_runner.py`
- Test: `backend/tests/test_verification_test_runner.py`

**Interfaces:**
- Consumes: `SandboxRuntime` (from `get_sandbox_runtime()`), `VerificationRun`/`VerificationEvidence` (Task 2).
- Produces: `run_tests(workspace, run, command) -> VerificationEvidence` — executes via `runtime.exec`, maps timeout to `error`, non-zero exit to `failed` status.

- [ ] **Step 1: Write the failing test**

```python
async def test_run_tests_maps_exit_codes():
    # fake runtime: echo handler returns exit_code=0 => passed; "fail" => exit 3 => failed
    ...
    ev = await run_tests(workspace, run, "echo hi")
    assert ev.status == "passed"
    ev2 = await run_tests(workspace, run, "fail")
    assert ev2.status == "failed"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_verification_test_runner.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `run_tests` in `test_runner.py`**

Use `SandboxRuntime.exec`; catch `CommandTimeoutError` → status `error`; catch other `SandboxRuntimeError` → status `error`; `exit_code == 0` → `passed`, else → `failed`. Persist as `VerificationEvidence(kind="test")`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_verification_test_runner.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/verification/test_runner.py backend/tests/test_verification_test_runner.py
git commit -m "feat(verification): add test runner adapter"
```

---

### Task 4: Observation interface

**Files:**
- Create: `backend/app/services/verification/observation.py`
- Test: `backend/tests/test_verification_observation.py`

**Interfaces:**
- Consumes: `SandboxRuntime.preview_link`, `VerificationRun`/`VerificationEvidence` (Task 2), the existing preview proxy.
- Produces: `observe(workspace, run, port) -> VerificationEvidence` — requests preview link, captures structured snapshot + description (for agent) and screenshot (for human); on preview failure records `observation_failed`.

- [ ] **Step 1: Write the failing test**

```python
async def test_observe_records_snapshot_and_screenshot():
    ev = await observe(workspace, run, 5173)
    assert ev.kind == "observation"
    assert "snapshot" in ev.detail and "screenshot" in ev.detail

async def test_observe_preview_failure_does_not_raise():
    # preview_link raises SandboxRuntimeError
    ev = await observe(workspace, run, 5173)
    assert ev.status == "observation_failed"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_verification_observation.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `observe` in `observation.py`**

Call `runtime.preview_link(workspace, port)`; capture a structured snapshot (DOM/HTML or state) + a short description string + a screenshot artifact. Wrap in try/except so a preview failure records `observation_failed` and returns, never raising. Persist `VerificationEvidence(kind="observation"|"screenshot")`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_verification_observation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/verification/observation.py backend/tests/test_verification_observation.py
git commit -m "feat(verification): add observation interface"
```

---

### Task 5: Verification orchestrator

**Files:**
- Create: `backend/app/services/verification/orchestrator.py`
- Test: `backend/tests/test_verification_orchestrator.py`

**Interfaces:**
- Consumes: Task 3 `run_tests`, Task 4 `observe`, `SandboxRuntime`, evidence models, existing run-registry / one-active-run-per-workspace guard.
- Produces: `VerificationOrchestrator` with `run(workspace, run_id)`: executes `test → observe → iterate` until pass criteria met, then sets run status `review`; finalizes to `approved`/`rejected` on human gate (set via `approve(run_id)`/`reject(run_id)`).

- [ ] **Step 1: Write the failing test**

```python
async def test_orchestrator_loop_and_gate():
    orch = VerificationOrchestrator(plan_criteria=...)
    await orch.run(workspace, run)
    assert run.status == "review"
    await orch.approve(run.id)
    assert run.status == "approved"
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/test_verification_orchestrator.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `VerificationOrchestrator`**

Drive the loop using Task 3/4; respect the one-active-run-per-workspace guard (reuse `RunLock` pattern from `services/runtime.py` — do not add a module-level dict). Use `pg_advisory_xact_lock` around the run transition. Set status `planning → testing → observing → review → approved/rejected`; `approve`/`reject` update only if current status is `review` (conditional UPDATE, mirroring `AgentService._transition`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_verification_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/verification/orchestrator.py backend/tests/test_verification_orchestrator.py
git commit -m "feat(verification): add orchestration loop"
```

---

## Out of scope (later plans)

- Frontend Human Evidence View (`frontend/`).
- API routes to trigger runs and submit human gates (`backend/app/api/`).
- Full cutover from the Docker provider to OpenSandbox (Task 1 adds the provider; removal of `docker.py` etc. is a separate migration plan gated on conformance).
- Spec Kit integration (remains the source of `Plan`/gates).

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-11-26-enigma-verifiable-agent-loop.md`. This plan has **5 tasks with tight interface dependencies** (each later task consumes the previous one's interfaces), and a mistake in the adapter would ripple into the loop — so **subagent-driven execution is recommended** for independent review per task.
