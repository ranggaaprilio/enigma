# Epic 01 - Runtime Provider Abstraction

## Goal

Extract a provider-neutral `SandboxRuntime` interface from `DaytonaRuntime`, close every
place where raw Daytona SDK objects leak to consumers, and add a factory that picks the
provider from `Workspace.runtime`. **Pure refactor**: after this epic Enigma behaves
exactly as today, but no module outside `services/runtime/` knows Daytona exists.

## User Value

None directly visible — this is the foundation that makes a local Docker sandbox (and any
future provider) possible without touching ten consumer services again. It also makes the
runtime layer honestly testable: the existing hand-rolled fakes become one conformance
suite.

## Current State

- `backend/app/services/daytona_runtime.py` is the only module importing `daytona`.
  It already defines provider-neutral DTOs `RuntimeCommandResult` (line 47) and
  `RuntimePreviewLink` (line 54), an error taxonomy (lines 26–44), and a
  `@lru_cache get_daytona_runtime()` singleton (line 354) that every consumer constructor
  takes.
- **Leak 1 — filesystem**: `get_sandbox()` (line 147) returns the raw SDK `Sandbox`, and
  consumers call `sandbox.fs.list_files / get_file_info / download_file / upload_file /
  create_folder / move_files / delete_file / find_files` directly:
  `services/files.py:118,145,161,169,213,214,229,231,250,255,277,291,299,324`,
  `services/agent_tools.py:147,158,197,199,207,209,215,218,219`,
  `services/repositories.py:642,659,660`, `services/dev_server.py:549,559`.
- **Leak 2 — git**: `services/repositories.py:217` uses `sandbox.git.clone(...,
  username="x-access-token", password=token)` and `:248` `sandbox.git.status(repo_path)`.
  Both already have exec-based CLI fallbacks in the same module (`_status_via_cli`), and
  `worktrees.py` is entirely CLI-based ("SDK git can't resolve worktree pointer files"
  per its docstring).
- **Leak 3 — PTY types**: `services/terminal.py:224` imports `PtySize` from `daytona`.
- Error names are provider-branded (`DaytonaUnavailableError` etc.) and caught by name
  across API modules.
- `Workspace.runtime` exists (`models/workspace.py:51`, default `"daytona"`) but is
  hardcoded at creation (`services/workspaces.py:131`) and never read for dispatch.
- Tests: `backend/tests/test_daytona_runtime.py` (`FakeFileSystem`, `FakeProcess`,
  `FakeSandbox`, `FakeClient`), `test_agent_editing_tools.py:27-52` (`FakeExecResult`,
  `FakeFs`, `FakeRuntime`), and `FakeRuntime`s in `test_agents.py` — each an ad-hoc partial
  spec of the same implicit interface.

## Scope

**In scope**

- New package `backend/app/services/runtime/` containing:
  - `base.py` — the `SandboxRuntime` protocol/ABC, `RuntimeFileSystem` protocol,
    `PtyHandle` protocol, `PtySize` dataclass, DTOs, provider-neutral errors.
  - `daytona.py` — `DaytonaRuntime` moved here, implementing the interface; the only
    `import daytona` in the codebase.
  - `factory.py` — `get_runtime(workspace) -> SandboxRuntime` keyed on
    `Workspace.runtime`, plus `get_runtime_by_name(name)` for health checks; per-provider
    singletons; `close_all()` for lifespan shutdown.
- Refactor all consumers off raw `Sandbox` objects.
- Provider-neutral error taxonomy with backwards-compatible aliases.
- One parametrized conformance test suite.

**Out of scope**

- Any Docker code (Epic 02).
- Any behaviour change to the Daytona path (timeouts, egress policy, naming — all frozen).
- Tier routing (`sandbox_for(workspace, intent)`) — that is
  `hybrid-runtime-migration/01`; if it has landed, this factory slots beneath its router;
  if not, nothing here precludes it.

## Backend Tasks

1. **Define the interface** in `services/runtime/base.py`, derived 1:1 from today's
   surface (no speculative methods):
   ```python
   class SandboxRuntime(Protocol):
       provider: str                      # "daytona" | "docker"
       @property
       def configured(self) -> bool: ...
       async def health(self) -> str: ...
       async def provision(self, workspace_id, owner_id, snapshot) -> str: ...  # returns sandbox_id
       async def status(self, workspace) -> str: ...      # ready|stopped|creating|error
       async def start(self, workspace) -> None: ...      # extracted from _ensure_started
       async def delete(self, workspace) -> None: ...
       async def exec(self, workspace, command, *, cwd=None, env=None, timeout=None) -> RuntimeCommandResult: ...
       async def stream_command(self, workspace, session_id, command, *, cwd, env, timeout, on_stdout, on_stderr) -> int: ...
       async def create_or_connect_pty(self, workspace, session_id, on_data, *, env, cols, rows, cwd) -> PtyHandle: ...
       async def kill_pty(self, workspace, session_id) -> None: ...
       async def signed_preview_url(self, workspace, port) -> str: ...
       async def preview_link(self, workspace, port) -> RuntimePreviewLink: ...
       def fs(self, workspace) -> RuntimeFileSystem: ...  # replaces get_sandbox().fs
       async def close(self) -> None: ...
   ```
   `RuntimeFileSystem`: `list_files`, `get_file_info`, `download_file`, `upload_file`,
   `create_folder`, `move_files`, `delete_file(recursive=...)`, `find_files` — signatures
   copied from current SDK usage so the Daytona implementation is a thin passthrough.
   `PtyHandle`: `send_input(data)`, `resize(PtySize)`, `disconnect()`.
2. **Key change — `provision` returns an id, not an SDK object.** Update
   `services/workspaces.py:142` to store the returned id in `workspace.sandbox_id`.
3. **Move + adapt `DaytonaRuntime`** to `services/runtime/daytona.py`. `get_sandbox()`
   becomes private (`_get_sandbox`). Keep `services/daytona_runtime.py` as a deprecation
   shim re-exporting names for one release, then delete.
4. **Provider-neutral errors** in `base.py`: `SandboxRuntimeError`,
   `RuntimeUnavailableError`, `SandboxNotFoundError`, `ProvisioningError`,
   `CommandTimeoutError`. Keep `Daytona*` names as subclass aliases; update `except`
   sites (`api/workspaces.py`, `api/health.py`, services) to the neutral names.
5. **Refactor filesystem consumers** (`files.py`, `agent_tools.py`, `repositories.py`,
   `dev_server.py`) to `runtime.fs(workspace).<op>(...)`. Mechanical; no logic changes.
6. **Collapse SDK git to CLI**: replace `sandbox.git.clone` (`repositories.py:217`) with
   an exec of `git clone` using a `GIT_ASKPASS`/credential-helper env so the token never
   appears in argv or logs; replace `sandbox.git.status` (`:248`) by promoting the
   existing `_status_via_cli` to the only path. Keep `sanitize_git_output` everywhere.
7. **Decouple PTY types**: define `PtySize(rows, cols)` in `base.py`; `terminal.py:224`
   imports it from there; the Daytona adapter converts to the SDK's `PtySize` internally.
8. **Factory + lifespan**: `get_runtime(workspace)` dispatches on `workspace.runtime`
   (unknown value → `RuntimeUnavailableError` with a clear message). `main.py:35` and the
   preview-proxy lifespan call `factory.close_all()`. `api/health.py:41-45` uses
   `get_runtime_by_name("daytona")`.
9. **CI guard**: a test (or ruff rule) asserting `import daytona` appears only under
   `services/runtime/`.
10. **Conformance suite**: `tests/runtime/test_conformance.py` parametrized over provider
    fixtures (Daytona fake now; Docker fake in Epic 02) exercising exec, fs round-trip,
    status mapping, error taxonomy, PTY handle contract. Port the assertions from the
    `test_daytona_runtime.py` and `test_agent_editing_tools.py` fakes.

## Frontend Tasks

None. (API shapes unchanged; the `runtime` field already exists in workspace schemas at
`schemas/workspaces.py:80,103`.)

## Database Tasks

None required. Optional (recommended, zero-risk): Alembic migration renaming
`workspaces.daytona_sandbox_id` → `sandbox_id` with its unique index; the ORM alias
property (`models/workspace.py:82`) already prepares for this. If deferred, defer to
Epic 06 and keep writing through the alias.

## Security Requirements

- Git credentials move from SDK kwargs to askpass/env — verify the token is absent from
  process lists (`ps`), sandbox logs, and structlog output; extend the
  `sanitize_git_output` tests.
- The agent command safety model in `agent_tools.py` (denylist/allowlist, shell-operator
  blocking, path confinement, output truncation) is untouched — assert its tests still
  pass unmodified.
- Error messages surfaced to the API must not embed provider URLs or keys.

## Acceptance Criteria

- `grep -rn "import daytona" backend/app | grep -v services/runtime` returns nothing.
- `grep -rn "get_sandbox\|sandbox\.fs\.\|sandbox\.git\." backend/app --include="*.py"`
  outside `services/runtime/` returns nothing.
- Full backend test suite passes with **no test-expectation changes** except import paths
  and error-name updates.
- `DAYTONA_INTEGRATION_TESTS=1` suite (`test_daytona_integration.py`) passes unchanged
  against a real sandbox: clone → status → agent file tools → terminal PTY → preview link.
- The conformance suite runs green against the Daytona fake and is provider-parametrized
  (ready to accept a second fixture).
- A workspace with `runtime="bogus"` gets a 4xx/503 with a clear "unknown runtime" error,
  not a 500.

## Dependencies

- None on other epics or series. **Sequencing note**: if
  `hybrid-runtime-migration/01-runtime-tiering` starts around the same time, agree on the
  split first — tier router *above*, provider factory *below* — and share the
  `services/runtime/` package layout.

## Future Enhancements

- Remove the `Daytona*` error aliases entirely once callers are migrated.
- Structured runtime capability flags (e.g. `supports_signed_preview`,
  `supports_snapshots`) if providers ever diverge in features rather than mechanics.
