# Epic 01 - Runtime Provider Abstraction

## Goal

Extract a provider-neutral `SandboxRuntime` interface from `DaytonaRuntime`, close every
place where raw Daytona SDK objects leak to consumers, and add a factory that picks the
provider from `Workspace.runtime`. **Pure refactor**: after this epic Enigma behaves
exactly as today, but no module outside `services/sandbox/` knows Daytona exists.
(`services/runtime.py` is already taken by the horizontal-scale run registry/event bus —
see the index's "Package name" note.)

## User Value

None directly visible — this is the foundation that makes a local Docker sandbox (and any
future provider) possible without touching ten consumer services again. It also makes the
runtime layer honestly testable: the existing hand-rolled fakes become one conformance
suite.

## Current State

- `backend/app/services/daytona_runtime.py` is the only module importing `daytona`
  (plus `terminal.py:6` for `PtySize`). It already defines provider-neutral DTOs
  `RuntimeCommandResult` (line 48) and `RuntimePreviewLink` (line 55), an error taxonomy
  (lines 27–45), and a `@lru_cache get_daytona_runtime()` singleton (line 365) that every
  consumer constructor takes — 31 modules import from it, and `worker.py` carries the
  instance in `ctx["daytona_runtime"]` (231, 330).
- **Leak 1 — filesystem**: `get_sandbox()` (line 154) returns the raw SDK `Sandbox`, and
  consumers call `sandbox.fs.list_files / get_file_info / download_file / upload_file /
  create_folder / move_files / delete_file / find_files / set_file_permissions` directly:
  `services/files.py:118,145,161,169,213,214,229,231,250,255,277,291,299,324,337`,
  `services/agent_tools.py:149,160,199,201,209,211,217,220,221`,
  `services/repositories.py:326–332,358,415,440,468,538,561,588,639,675,783,824–826,840,874,890–892`,
  `services/dev_server.py:254,462,470`, `services/workspace_services.py:1103,1120,1129`,
  `services/workspace_init.py:115,122,131,140,142,147`,
  `services/workspace_env.py:181,183,189,190`. `provision()` itself returns the SDK
  object and `services/workspaces.py:215-218` reads `sandbox.id` off it.
- **Leak 2 — git**: down to a single call, `sandbox.git.status(repo_path)` at
  `services/repositories.py:368`. Clone/pull/fetch/push already go through the git CLI
  via `exec` with per-command auth (token header or SSH deploy key, `_run_ssh_git`;
  `services/git_providers.py`, `services/ssh_keys.py`), and `worktrees.py` is entirely
  CLI-based ("SDK git can't resolve worktree pointer files" per its docstring).
- **Leak 3 — PTY types**: `services/terminal.py:6` imports `PtySize` from `daytona`
  (used at 230). The handle returned by `create_or_connect_pty` is the SDK's PTY session
  object (`send_input`, `resize`, `disconnect` are the only methods `terminal.py` uses).
- **Leak 4 — column access**: `workspace.daytona_sandbox_id` is read directly (instead
  of the `sandbox_id` alias, `models/workspace.py:115`) in `preview_proxy.py:173,226,269`,
  `preview.py:288`, `workspace_services.py:959`, `run_detection.py:604,649`,
  `terminal.py:56`, `editor.py:75`, `dev_server.py:479`, `commands.py:404`,
  `workspaces.py:218,249`.
- Error names are provider-branded (`DaytonaUnavailableError` etc.) and caught by name
  in 15 modules (`api/workspaces.py` alone has 15 occurrences, `api/workspace_services.py`
  10). `sandbox_probe.py:36,64` and `commands.py:127` type-annotate `DaytonaRuntime`.
- `signed_preview_url` (line 305) has **no callers**: the editor moved to the proxy's
  stable `editor-<alias>` host with a grant cookie (`editor_alias.py`, migration `0029`).
- `Workspace.runtime` exists (`models/workspace.py:68`, default `"daytona"`) but is
  hardcoded at creation (`services/workspaces.py:180`) and never read for dispatch.
- `api/health.py:49-54` builds a throwaway `DaytonaRuntime(get_settings())` per probe and
  reports it as the `daytona` field of `HealthResponse`; `/live` and `/ready` do not
  touch the runtime.
- Tests: `backend/tests/test_daytona_runtime.py` (`FakeFileSystem`, `FakeProcess`,
  `FakeSandbox`, `FakeClient`), `test_agent_editing_tools.py:27-52` (`FakeExecResult`,
  `FakeFs`, `FakeRuntime`), and `FakeFs`/`FakeSandbox`/`FakeRuntime` triples in
  `test_workspace_init.py`, `test_workspace_env.py`, `test_workspace_services.py`,
  `test_dev_server.py`, plus `FakeRuntime`s in `test_files.py`, `test_worktrees.py`,
  `test_specs.py`, the four `test_preview_proxy*.py` files and `FakeDaytona` in
  `test_worker_jobs.py` — each an ad-hoc partial spec of the same implicit interface.

## Scope

**In scope**

- New package `backend/app/services/sandbox/` containing:
  - `base.py` — the `SandboxRuntime` protocol/ABC, `RuntimeFileSystem` protocol,
    `PtyHandle` protocol, `PtySize` dataclass, DTOs, provider-neutral errors.
  - `daytona.py` — `DaytonaRuntime` moved here, implementing the interface; the only
    `import daytona` in the codebase.
  - `factory.py` — `get_runtime(workspace) -> SandboxRuntime` keyed on
    `Workspace.runtime`, plus `get_runtime_by_name(name)` for health checks; per-provider
    singletons; `close_all()` for lifespan shutdown (API, worker **and** preview proxy).
- Refactor all consumers off raw `Sandbox` objects and off `daytona_sandbox_id`.
- Provider-neutral error taxonomy with backwards-compatible aliases.
- One parametrized conformance test suite.

**Out of scope**

- Any Docker code (Epic 02).
- Any behaviour change to the Daytona path (timeouts, egress policy, naming — all frozen).
- Tier routing (`sandbox_for(workspace, intent)`) — that is
  `hybrid-runtime-migration/01`; if it has landed, this factory slots beneath its router;
  if not, nothing here precludes it.

## Backend Tasks

1. **Define the interface** in `services/sandbox/base.py`, derived 1:1 from today's
   surface (no speculative methods — and drop `signed_preview_url`, which nothing calls):
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
       async def preview_link(self, workspace, port) -> RuntimePreviewLink: ...
       def fs(self, workspace) -> RuntimeFileSystem: ...  # replaces get_sandbox().fs
       async def close(self) -> None: ...
   ```
   `RuntimeFileSystem`: `list_files`, `get_file_info`, `download_file`, `upload_file`,
   `create_folder`, `move_files`, `delete_file(recursive=...)`, `find_files`,
   `set_file_permissions(path, mode=...)` — signatures copied from current SDK usage so
   the Daytona implementation is a thin passthrough. `PtyHandle`: `send_input(data)`,
   `resize(PtySize)`, `disconnect()`.
2. **Key change — `provision` returns an id, not an SDK object.** Update
   `services/workspaces.py:215-218` to store the returned id in `workspace.sandbox_id`.
3. **Move + adapt `DaytonaRuntime`** to `services/sandbox/daytona.py`. `get_sandbox()`
   becomes private (`_get_sandbox`). Keep `services/daytona_runtime.py` as a deprecation
   shim re-exporting names for one release, then delete.
4. **Provider-neutral errors** in `base.py`: `SandboxRuntimeError`,
   `RuntimeUnavailableError`, `SandboxNotFoundError`, `ProvisioningError`,
   `CommandTimeoutError`. Keep `Daytona*` names as subclass aliases; update the 15
   `except` modules (`api/workspaces.py`, `api/workspace_services.py`, `api/dev_server.py`,
   `api/preview.py`, `api/health.py`, `preview_proxy.py`, and the services) to the
   neutral names, and the `DaytonaRuntime` type annotations in `sandbox_probe.py`,
   `commands.py` (`daytona_runtime` kwarg → `sandbox_runtime`) and `preview_proxy.py:305`.
5. **Refactor filesystem consumers** (`files.py`, `agent_tools.py`, `repositories.py`,
   `dev_server.py`, `workspace_services.py`, `workspace_init.py`, `workspace_env.py`) to
   `runtime.fs(workspace).<op>(...)`. Mechanical; no logic changes. In the same pass,
   replace the 11 direct `workspace.daytona_sandbox_id` reads with `workspace.sandbox_id`.
6. **Collapse the last SDK git call**: replace `sandbox.git.status` (`repositories.py:368`)
   by promoting the existing CLI status path to the only path. Clone/pull/fetch/push are
   already CLI with per-command auth — leave them alone; keep `sanitize_git_output`
   everywhere.
7. **Decouple PTY types**: define `PtySize(rows, cols)` in `base.py`; `terminal.py:6`
   imports it from there; the Daytona adapter converts to the SDK's `PtySize` internally.
8. **Factory + lifespan**: `get_runtime(workspace)` dispatches on `workspace.runtime`
   (unknown value → `RuntimeUnavailableError` with a clear message). `main.py:94`,
   `worker.py:395` (and its `ctx["daytona_runtime"]` → `ctx["sandbox_runtime"]`) and the
   preview-proxy lifespan (`preview_proxy.py:341`) call `factory.close_all()`.
   `api/health.py:49-54` uses `get_runtime_by_name("daytona")` and keeps the `daytona`
   response field (existing monitors read it).
9. **CI guard**: a source-grep test asserting `import daytona` appears only under
   `services/sandbox/` — same shape as
   `tests/test_run_runtime.py::test_no_call_site_reaches_into_process_local_registries`.
10. **Conformance suite**: `tests/sandbox/test_conformance.py` parametrized over provider
    fixtures (Daytona fake now; Docker fake in Epic 02) exercising exec, fs round-trip,
    status mapping, error taxonomy, PTY handle contract. Port the assertions from the
    `test_daytona_runtime.py` and `test_agent_editing_tools.py` fakes.
11. **Collapse the test fakes**: the ~27 ad-hoc fakes listed under Current State become
    one shared `tests/sandbox/fakes.py` (`FakeSandboxRuntime` + `FakeFileSystem`) that the
    other test modules import. This is the one place the "no test-expectation changes"
    rule bends: fakes that expose `.fs` on a `FakeSandbox` must expose `fs(workspace)` on
    the runtime instead.

## Frontend Tasks

None. (API shapes unchanged; the `runtime` field already exists in workspace schemas at
`schemas/workspaces.py:123,157` with `default_runtime` validators at 138–140, 166–168.)

## Database Tasks

None. The `daytona_sandbox_id` → `sandbox_id` rename is a **contract** change (a column
rename fails `tests/test_migration_safety.py` unless marked `# contract:`), so it is
deferred to Epic 06 as an expand/contract pair; this epic writes through the ORM alias
(`models/workspace.py:115`).

## Security Requirements

- Git credentials already travel per-command (token header / SSH deploy key) and never
  through SDK kwargs — keep it that way; re-run the `sanitize_git_output` tests after the
  `git.status` collapse.
- The agent command safety model in `agent_tools.py` (denylist/allowlist, shell-operator
  blocking, path confinement, output truncation) is untouched — assert its tests still
  pass unmodified.
- Error messages surfaced to the API must not embed provider URLs or keys.

## Acceptance Criteria

- `grep -rn "import daytona" backend/app | grep -v services/sandbox` returns nothing.
- `grep -rn "get_sandbox\|sandbox\.fs\.\|sandbox\.git\.\|daytona_sandbox_id" backend/app --include="*.py"`
  outside `services/sandbox/` and `models/workspace.py` returns nothing.
- Full backend test suite passes with **no test-expectation changes** except import paths,
  error-name updates and the fake consolidation in task 11. `tests/test_run_runtime.py`,
  `tests/test_preview_proxy_scaling.py` and `tests/test_data_layer.py` stay green
  untouched (no new process-local state, no session held across a WebSocket `accept()`).
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
  `services/sandbox/` package layout (that series' draft also says `services/runtime/`;
  both must move off the name `services/runtime.py` already owns).

## Future Enhancements

- Remove the `Daytona*` error aliases entirely once callers are migrated.
- Structured runtime capability flags (e.g. `supports_signed_preview`,
  `supports_snapshots`) if providers ever diverge in features rather than mechanics.
