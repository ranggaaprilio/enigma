# Enigma — Loop Additions: Prepare, API Probes, Review Queue (Plan 11, Track B)

> **For agentic workers:** Every step below is one PR and can be executed cold: read its context brief, do its tasks with TDD (red → green → refactor), run its verification, meet its exit criteria. Tick the `- [x]` boxes as they land. Steps here only add or change behaviour; removals are Track A (`2026-11-28-enigma-lean-cut.md`), agent roles are Track C (`2026-11-28-enigma-agent-roles.md`). The cross-track dependency map and the list of who owns each shared file, column and constant is spec §14 — read it before touching anything another step also names.

> **If Track D steps D1 and D2 are merged** (`2026-11-28-enigma-single-team.md`, Plan 13 — recommended before this track): Enigma has one execution mode. Ignore every task here that mentions queue mode, a job deferring with `Retry`, awaiting inside a job, the reaper, `worker_job_timeout_seconds` or a redelivered job; keep its inline half (a `run_registry` task, conditional status writes, and the startup sweep in `services/reconciliation.py` covering the new state).

**Goal:** The loop can stand up its own environment, prove a backend with no UI, start the app it observes, and put what needs a human in front of them.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §3 (loop, with Prepare), §9.3–9.4, §11 (Prepare), §12 (API probes), §13 (agent roles — Track C, `2026-11-28-enigma-agent-roles.md`, Plan 12, which builds on B3–B6).

## Baseline (verified 2026-11-28, on `feature/verify-agent-runs` = `main` + Plan 9, PR #37)

- **Observe never starts the app.** `VerificationOrchestrator` (`services/verification/orchestrator.py`, observe step near line 375) calls `observe(workspace, run, criteria.observe_port, …)` only when `observe_port` is set; `services/verification/observation.py` asks `runtime.preview_link(workspace, port)`, does one `GET` of the root, extracts `<title>`, stores a snapshot and a screenshot. If nothing listens, the evidence is `observation_failed`.
- **Criteria** are `PlanCriteria(test_command, observe_port, cwd, timeout, max_iterations)`, persisted on the run as `criteria` (`criteria_to_row` / `criteria_from_row`).
- **Evidence** kinds are `("test", "observation", "screenshot", "fix")`, statuses `("passed", "failed", "observation_failed", "error")`, both enforced by CHECK constraints (`models/verification.py`).
- **Running services:** `WorkspaceServiceManager` (`services/workspace_services.py`). The primary service is the workspace's enabled `role="dev"` command, else the built-in mise `dev` row. Three facts that shape B1 and B5: `ensure_running(workspace_id, user_id)` takes no service id and relaunches every *desired* row (~192); `ensure_running_primary` never starts a row with `desired=False` (~412–416); only `run_primary(workspace_id, user_id, worktree_id)` starts a stopped primary, and it sets `desired`, `dev_autostart` and `dev_worktree_id` and kills a run on another worktree (~493–512). `_sync_definitions` and `_registry_command` only see `enabled` commands (~1015–1040), so a disabled command never gets a service row.
- **Owner-only services:** `CommandService._get_workspace` (`commands.py` ~604–608) and `WorkspaceServiceManager._get_workspace` (~956–962) require `workspace.owner_id == user_id`, while agent and verification runs also accept team leads and members (`services/access.py`), and `verification_runs.created_by` is nullable. A loop step that calls them with the run's creator fails for a non-owner.
- **Agent command policy:** `services/agent_tools.py` `DANGEROUS_COMMANDS` blocks `chmod chown curl dd docker docker-compose git mkfs mount npm pnpm rm sudo su uv wget yarn`; `DANGEROUS_TOKENS` blocks the shell operators `&&`, `||`, `;`, `|`, `>`, `>>`, `<`, `$(` and the backtick. Settings: `agent_command_allowlist_enabled`, `agent_command_allowlist`, `agent_command_deny_dangerous` (`core/config.py` ~204), applied in `services/agents.py` ~1045. With these, an agent cannot install anything. How validation works (`agent_tools.py` ~448–497) decides B3's design: a validated command is split and **re-quoted** with `shlex.quote` and so never reaches a shell as a pipeline; any `/` in the program name is rejected (`path_in_argv0`), so `./configure` or `~/.local/bin/mise` fail; the denylist looks at the first word only; only the unrestricted branch (allowlist off **and** `deny_dangerous` off) passes the raw string to the shell.
- **Agent tools** (`services/openhands_tools.py`): list/read/write file, run command, apply patch, search code, git status, git diff. Nothing reaches the commands registry. The tool list is static in `services/openhands_provider.py` (~157–165); `stream_response(prompt, tools, stop_event)` knows no mode; executors see only `WorkspaceTools` through `DaytonaToolBridge`, which carries no DB session, user id or run id. Only the OpenHands provider has these tools (not `mock`, not `openai`).
- **Agent runs:** `AgentService.create_run(workspace_id, user_id, prompt, working_dir, worktree_id, session_id)`; `agent_runs` has no mode/role column.
- **Commands:** `custom_commands` has `role` ∈ `install|migrate|dev|test|lint|seed|custom`, `kind`, `cwd`, `port`, `enabled`, `health_path`; created through `CommandService.create_command`.
- **Auto-verify** exists (`workspaces.auto_verify_agent_runs`, default `False`; `AgentService._auto_verify`, `schemas/workspaces.py`).
- **Sandbox floor** (OpenSandbox): non-root, `CapEff 0`, `NoNewPrivs 1`; no `apt`, no Docker inside. **Feasibility proven 2026-11-28:** in `enigma-sandbox:latest` under `--cap-drop=ALL --security-opt no-new-privileges`, PostgreSQL 16.16 installed with micromamba, `initdb`, started on `127.0.0.1:5432` and answered SQL in 10 s total. Not yet proven: the same through an OpenSandbox server's egress policy, other services, and data surviving pause/resume.
- Migration head `0045_verify_agent_runs`; new ones start at `0046` and must be expand-only (`tests/test_migration_safety.py`). The guard flags `create_check_constraint` and constraint drops (`OPERATIONS` list, ~35), so **every migration below that adds or widens a CHECK needs a `# contract: <why>` comment line** — the marker is matched anywhere in the file (`CONTRACT_MARKER`, ~25, ~206), as `0041` did. Never edit the guard.
- **Verification run statuses** are fixed by `ck_verification_runs_status`: `planning|testing|fixing|observing|review|approved|rejected|error`; `ACTIVE_STATUSES` is in `orchestrator.py` (~50).
- **Observation fetch** (`observation.py` ~180–189): `client.get(url, headers=link.headers)`, following redirects on the same origin with the headers attached. On OpenSandbox the preview URL is `http://<host>:<execd port>/proxy/<port>` — **the same origin as execd's own API** — and the headers carry the execd token (`sandbox/opensandbox.py` ~961–973). `check_preview_path` (`app/preview_proxy.py:1190`) is the existing dot-segment guard for that reason.
- **Auto-verify** fires for every completed agent run (`agents.py:1244`) and needs the workspace flag plus criteria (`services/verification/agent_link.py` ~169–181).

## Invariants (run after every step)

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && pnpm lint && pnpm test && pnpm build
```

- If `pnpm` fails with a workspace error locally, run `./node_modules/.bin/eslint .`, `./node_modules/.bin/vitest run`, `./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build`.
- Everything runs through `SandboxRuntime` (`tests/sandbox/test_boundary.py`). Inside a sandbox the backend never execs a bare command name (absolute paths or shell builtins) and never guards a launch with `pgrep -f`.
- Both execution modes stay correct: inline and `RUN_EXECUTION_MODE=queue`. Never await an agent run inside a queue job (defer with `Retry`), as the verification job already does.
- New status writes are conditional UPDATEs; persist before you publish; no module-level in-flight dicts (`tests/test_run_runtime.py`).
- Tests that drive a run without an LLM pass `Settings(agent_allow_mock_provider=True)` and pin settings explicitly (never a bare `Settings()`).
- Output of tests, commands, probes and observations never goes into a PR body (`services/spec_pull_request.py`).
- Each step updates `AGENTS.md` (a new numbered gotcha per step that adds a rule).

## Step map

| Step | Adds | Depends on | Model tier |
|---|---|---|---|
| B1 | Observe starts the dev service | — | default |
| B2 | Auto-verify on by default for new workspaces | — | default |
| B3 | Setup runs: the `devops` role for agent runs | — | strongest |
| B4 | `register_command` tool | B3 | strongest |
| B5 | Setup proof and environment gate | B4 | strongest |
| B6 | API probes (backend observation) | B1 | strongest |
| B7 | Frontend: probes, setup, proposed commands | B5, B6 | default |
| B8 | Review queue as the home page | — (shares `DashboardOverviewRoute.tsx` with Track A A3) | default |

B1, B2, B3 and B8 are independent of each other. B3 → B4 → B5 is serial. B6 can run beside B3–B5 (both touch `services/verification/`: B5 adds a run kind, B6 adds criteria fields — rebase, do not merge blindly).

Rollback: revert the PR. Every migration here is additive (new nullable columns or columns with a default), so a reverted release runs against the newer schema.

---

## B1 — Observe starts the dev service

**Context.** A verification run with an `observe_port` assumes a human already started the app. The loop must do it: before observing, make sure the workspace's primary (`role="dev"`) service is running in the run's checkout, then wait, bounded, for the port to listen.

- [ ] Red: an orchestrator test where the primary service is stopped and `observe_port` is set → the service is asked to run before `observe` is called; a test where starting fails or the port never listens → evidence `observation` / `observation_failed` with a reason, the run still reaches `review` (spec §6).
- [ ] Add a small seam (`services/verification/app_runner.py`, a protocol plus the real implementation over `WorkspaceServiceManager`) so the orchestrator stays testable without a sandbox; inject it like `Fixer`.
- [ ] The real implementation calls `run_primary(workspace_id, user_id, worktree_id)` — the only method that starts a stopped primary (see Baseline). It is owner-only: add a workspace-object entry point for system callers, or call it as `workspace.owner_id` and audit the run's real creator. Do not loosen the HTTP routes' owner check.
- [ ] Target the run's checkout: the source agent run's worktree (Plan 9, `verification_runs.source_agent_run_id`) or the Spec Kit feature worktree, else the base checkout.
- [ ] `run_primary` has side effects on the human's own preview: it sets `desired`, `dev_autostart` and `dev_worktree_id` and kills a primary running on another worktree. Decide and test one behaviour: restore the previous `dev_worktree_id`/`desired` after observing, or leave the service on the verified worktree and say so in the evidence description. Write the choice in `AGENTS.md`.
- [ ] Wait for the listen table, not for a process name (see Exec hygiene in `AGENTS.md`); bound it with a new setting `VERIFICATION_APP_START_TIMEOUT_SECONDS` (default 60).
- [ ] A workspace with no dev service command behaves as today (observe is attempted, may record `observation_failed`).

**Verify.** Invariants; live: in a local OpenSandbox workspace, stop the dev service, start a verification with `observe_port`, see a real snapshot and screenshot.

**Exit.** A human never has to start the app for Observe; a failed start is evidence, not a failed run.

## B2 — Auto-verify on by default for new workspaces

**Context.** Plan 9 shipped auto-verify opt-in. The spec says "works" means observed, so a completed run without evidence should be the exception. Auto-verify already does nothing when a workspace has no criteria, so defaulting it on is safe for workspaces without a test command.

- [ ] Red: creating a workspace without the field stores `auto_verify_agent_runs = True`; an explicit `false` is kept; existing rows are untouched.
- [ ] `WorkspaceCreateRequest` (`schemas/workspaces.py` ~23–35) has no such field today: add an optional `auto_verify_agent_runs` and set `True` when it is absent where the row is built (`services/workspaces.py` ~231, and the team route). Leave the model default (`models/workspace.py` ~101) at `False` so rows created by any other path, and existing rows, do not change. If a column `server_default` change is wanted, it is a separate expand migration — changing a default is not a tightening, but check `tests/test_migration_safety.py` accepts it.
- [ ] Frontend: the workspace settings toggle reflects the stored value; copy says it runs only when a test command exists.

**Verify.** Invariants.

**Exit.** New workspaces verify completed agent runs without being asked; old workspaces behave as before.

## B3 — Setup runs: the `devops` role

**Context.** To prepare an environment the agent must install runtimes and dependencies and run shell pipelines, which the standard command policy forbids. The sandbox is the security boundary (hardened, private egress denied), so a *setup* run gets a wider policy. Standard runs do not change.

- [ ] Migration `0046_agent_run_role`: `agent_runs.role` `String(16)`, `server_default="developer"`, not null, CHECK `role in ('developer','devops')` **named `ck_agent_runs_role`** (Track C widens it by that name). Add `role` to `AgentRunResponse` (`schemas/agents.py`), which has no such field today; Track C's floor view reads it. The column is named `role`, not `mode`, on purpose: Track C adds `qa` and `reviewer` to the same column, and a rename later would be a contract migration. **A "setup run" is a run with `role="devops"`**; `developer` is every run that exists today. Additive, but the new CHECK needs the `# contract:` marker (see Baseline).
- [ ] `AgentService.create_run(..., role="developer")`; the role is stamped on the row and read from the row by the worker (never from a job argument, like `request_id`).
- [ ] Policy: a setup run executes the **raw command string** in the sandbox shell, the same branch the unrestricted mode already uses (`run_command`, ~448). Validating argv cannot work here: it re-quotes operators into literals and rejects any path in the program name (see Baseline). Be explicit in code comments and `AGENTS.md` about what this means: **in setup mode the sandbox floor and the egress policy are the only boundary.** A first-word denylist cannot stop `true && sudo x`; `sudo`, `apt` and Docker fail because the sandbox has no root and no daemon, not because Enigma filtered them. Keep a best-effort refusal of a command whose *first word* is `sudo`, `su`, `docker`, `docker-compose`, `mount`, `mkfs`, `dd` or `git` purely as a fast, clear error for the agent, and name it advisory. Selected in `services/agents.py` where the tools are built (~1045) from the run's role. Put the per-role choices (prompt preamble, command policy, tool set) in one small module, `services/agent_roles.py`, with two entries; Track C step C1 adds entries there and nowhere else.
- [ ] Allowlist: when `agent_command_allowlist_enabled` is on, the operator has chosen a closed command set, which raw execution would void. A setup run is then **refused** at `create_run` with a clear message (`AgentConfigurationError`-style, HTTP 409), and the docs say setup mode needs the allowlist off. Test it.
- [ ] Daytona: setup mode leans on the OpenSandbox floor. Before enabling it for `runtime="daytona"`, confirm what a Daytona sandbox's user can do (root? `sudo`?) and write the answer in the PR; if it is root-capable, the instructions' "no `apt`" rule is a convention there, not a boundary.
- [ ] Separate, longer command timeout for setup (`AGENT_SETUP_COMMAND_TIMEOUT_SECONDS`, default 600): installs outlast the standard timeout.
- [ ] Setup instructions prepended to the prompt for `role="devops"`: inspect the repository; install what it needs **in user space** (mise first, then language package managers, prebuilt binaries such as micromamba for services like Postgres); never use `sudo`, `apt` or Docker; translate `docker compose` services into plain processes bound to `127.0.0.1`; write `mise.toml` in the repository; report anything that needs a system package as "needs the image".
- [ ] API: `POST /workspaces/{id}/agent/setup-runs` (owner/driver, sandbox rate limit, audited as `agent_setup_run_started`). Budget, caps, the one-active-run guard and the draining refusal all apply because it goes through `create_run`.
- [ ] Tests: policy unit tests for both roles (each denied program and token), role round-trip inline and queue, a `developer` run still cannot `curl`.

**Verify.** Invariants; `uv run alembic upgrade head`; live: a setup run on a repository that needs Postgres installs and starts it (micromamba path proven in the Baseline).

**Exit.** A setup (`devops`) run can install and start user-space software; a `developer` run's policy is exactly what it was.

## B4 — `register_command` tool

**Context.** The setup agent must record *how to run this project* where the loop and the UI read it: the commands registry. It must not touch the database; it calls a tool that goes through `CommandService.create_command`, so validation and audit apply.

- [ ] Migration `0047_command_proposed_by`: `custom_commands.proposed_by_agent_run_id` (nullable FK `agent_runs`, `ON DELETE SET NULL`, indexed). Expand-only.
- [ ] Plumbing first (this is most of the step): the tool list is static and executors have no database access (see Baseline). Change `services/openhands_provider.py` (build the tool list per run, from the run's role profile), `services/agent_tools.py` / `DaytonaToolBridge` (carry a narrow `register_command` callback, not a session), and `services/agents.py` ~1040 (create that callback with the run id, workspace and a fresh session from `async_session_maker` per call — never hold a session for the run's lifetime). The `mock` and `openai` providers do not get the tool; a setup run on them is refused at `create_run`.
- [ ] `CommandService.create_command` is owner-only (see Baseline): add a workspace-object entry point for this system caller, and record the run's creator as the audit actor.
- [ ] Tool `register_command(name, command, role, kind, cwd?, port?, health_path?)` defined in `services/openhands_tools.py`, offered **only** to `role="devops"`. `role` limited to `install|migrate|seed|dev|test|custom`. The **app** is the one `role="dev"`, `kind="service"` command. A **support service** (Postgres, Redis) is `role="custom"`, `kind="service"`, with its `port`: it must not take the `dev` role, which means "the app" to the primary-service logic and to Track C's additive rule. `custom` without `kind="service"` is refused.
- [ ] The row is created with `enabled=False` and `proposed_by_agent_run_id` set. Registering the same `(role, name)` again in the same run updates the proposal rather than duplicating it. Cap proposals per run (20) and reuse the registry's own length/validation limits.
- [ ] Audit each registration (`command_proposed_by_agent`), with the run id and never the command's output.
- [ ] Tests: tool absent for a `developer` run; row disabled and attributed; duplicate → update; cap enforced; a disabled proposed command is not picked by `spec_link`'s "first enabled `role="test"` command" rule and gets no service row from the service manager's normal sync (B5 adds the one explicit path that may start it).

**Verify.** Invariants.

**Exit.** A setup run leaves a set of disabled, attributed command proposals; nothing in the loop uses them yet.

## B5 — Setup proof and the environment gate

**Context.** Setup is done when Enigma itself has executed the proposed contract, not when the agent says so. The proof is a verification run of a new kind; approving it is the one-time environment gate that enables the proposals.

- [ ] Migration `0048_verification_run_kind`: `verification_runs.kind` `String(16)`, `server_default="change"`, CHECK `kind in ('change','setup')`; and `verification_runs.setup_agent_run_id` (nullable FK `agent_runs`, `ON DELETE SET NULL`, indexed) — the devops run whose proposals this run proves. Do **not** reuse `source_agent_run_id` (Plan 9): it drives `is_fix_run` and the supersede rule for change runs. Evidence kind `"setup"` added to `VERIFICATION_EVIDENCE_KINDS` (a CHECK widening). Both need the `# contract:` marker (see Baseline).
- [ ] **No new run status.** A setup run reuses `testing`: `advance` branches on `kind` there, then goes straight to `review` (no `fixing`, no `observing`). That keeps `ck_verification_runs_status`, `ACTIVE_STATUSES`, the reaper, reconciliation and the frontend status types unchanged.
- [ ] Start path: when a `role="devops"` agent run completes, its finaliser calls a **dedicated** `start_setup_verification` (never for a failed/stopped run; a refusal is logged, not fatal). It must not go through `auto_verify_agent_run`, which needs the workspace flag and criteria; and the finaliser must branch on `role` **before** `_auto_verify` (`agents.py:1244`), or with B2's default a setup run is verified twice. Test both. Write that branch as one small dispatch function (`_after_completed(run)`): Track C step C3 adds one more branch to it (a run that ended with a pending request), and the order must stay role → pending request → auto-verify.
- [ ] Service start path: the manager cannot start a disabled command (see Baseline). Add one explicit method, e.g. `WorkspaceServiceManager.start_for_proposal(workspace, command)`, usable only by this step, that creates the service row for a disabled proposal and launches it. It must still apply `_check_limit` and `_check_port_conflict`; a conflict is setup evidence (failed), not an exception.
- [ ] Clean slate: the setup agent probably left its own processes running (Postgres on 5432, the app). Starting the proposal on top of them either conflicts or passes falsely. Before the proof, stop what listens on each proposal's port (through `SandboxRuntime.exec`, by the listen table, absolute paths), then start from the registered commands only.
- [ ] The setup step runs the run's proposals **by id**, in this order: `install` → **support services** (`role="custom"`, `kind="service"`: started, each waited for on its port — a database must be up before migrations) → `migrate` → `seed` → the app (`role="dev"`) started and waited for → the `test` proposal executed once. `install`/`migrate`/`seed` must exit 0 (evidence `setup` with status and the last 8 KiB of output, `OUTPUT_TAIL_CHARS`); each service start is one `setup` evidence. For the test (evidence `test`; a failing test is recorded but does **not** fail setup — setup proves the suite *can run*).
- [ ] Stop on the first failed `install`/`migrate`/`seed` or a service that never listens: the run goes to the gate with the failure as evidence (a human may reject, or re-run setup with the evidence in the prompt). No automatic fix loop in this step.
- [ ] Gate: `approve` on a `kind="setup"` run enables its proposals in the same transaction; `reject` leaves them disabled. A newer **human-started** setup run supersedes an older human-started one still in `review` (→ `rejected`). On approval a proposal replaces an earlier agent-proposed command with the **same role and name** (disable, do not delete); commands with another name, and every human-authored command, are never touched. Keep both rules keyed this way: Track C step C3 adds setup runs started by a request, which must neither supersede a human's pending gate nor disable the first setup's commands.
- [ ] Stop, reaper and queue-mode behaviour come from the existing orchestrator paths (`end`, `reap_lost_verification_jobs`); add tests that a `kind="setup"` run is covered by both.
- [ ] Queue mode: installs can be long. Check the proof against the worker's job timeout (`worker_job_timeout_seconds`, `app/worker.py` ~471). If one `advance` could exceed it, make each proposal its own step (persist progress in evidence, return `Step.CONTINUE`) so a redelivered job resumes instead of restarting.

**Verify.** Invariants; live E2E: empty-registry workspace → setup run → evidence shows install, Postgres listening, test executed → approve → a normal agent run auto-verifies using the enabled `test` command and B1 starts the `dev` service.

**Exit.** A fresh repository reaches a working, human-approved environment with no command typed by a human.

## B6 — API probes

**Context.** For a backend there is no page to look at. What can be observed is how the running service answers. A probe is one HTTP request plus its expectation; Enigma runs it, not the agent, so the result is evidence.

- [ ] `PlanCriteria.probes: tuple[Probe, ...] = ()`; `Probe(method, path, body=None, headers=None, expect_status, expect_json_contains=None)`. Persisted in `criteria` (`criteria_to_row` / `criteria_from_row`; rows without `probes` load as empty). API schemas for `POST /verification/runs`, the agent-run verify endpoint and Spec Kit's workflow `verification` accept the same list. Limits: 20 probes, path must start with `/`, no dot segments, no absolute URLs, methods `GET POST PUT PATCH DELETE`, body ≤ 16 KiB.
- [ ] Executor in `services/verification/probes.py`: uses the same route as page observation (`runtime.preview_link(workspace, port)` + its `headers`, `httpx`, bounded timeout per probe). Probes run in order and share nothing but the service's own state (so `POST` then `GET` works).
- [ ] **Security — the preview shares execd's origin and the request carries execd's token** (see Baseline). Three rules, each with a test:
  1. Build the URL by **appending** the probe path to the preview link's own path (`…/proxy/<port>` + `/users`). Never `url.join(path)`: joining an absolute path replaces `/proxy/<port>` and the request lands on execd's API with its token.
  2. Validate every probe path with `check_preview_path` (encoded and double-encoded dots, backslashes). Move it out of `app/preview_proxy.py` into a module both can import (e.g. `services/preview_capability.py`); do not copy it.
  3. **Never follow redirects** for probes. A tenant app answering `307` to a path outside `/proxy/<port>` would otherwise have Enigma replay a `POST` with the token against execd. A redirect response is recorded as the probe's result.
- [ ] Daytona: observation sends `link.headers` only, never `link.token`. Check whether a private Daytona preview answers a probe that way; if not, record `observation_failed` with a clear reason rather than guessing, and note the limitation in the docs.
- [ ] Port: probes need one. Use `criteria.observe_port`; when it is not set, the enabled `role="dev"` command's `port`. Fill `observe_port` the same way where criteria are **derived** (`spec_link.criteria_for_workflow`, `agent_link`'s criteria for an agent run), which today carry none. With no port at all, probes are recorded `observation_failed` ("no port to probe").
- [ ] Evidence: one `kind="observation"` row; `detail.probes` = per probe `{method, path, expect_status, status, passed, response_excerpt (≤ 2 KiB), error?}`; row status `passed` when all pass, `failed` when any expectation fails, `observation_failed` when the service could not be reached. No new evidence kind.
- [ ] Loop semantics: a `failed` probe evidence is treated like a failed test — with iterations left the run goes to `fixing`, and the fix prompt carries the failing probes (method, path, expected vs got; excerpt only). `observation_failed` never blocks (spec §6). With both a page and probes, both run.
- [ ] Redaction: request headers named `authorization`/`cookie` and any header value from the preview link are never stored.
- [ ] Tests: schema limits; executor with an `httpx` mock transport (pass, wrong status, JSON mismatch, unreachable); orchestrator transitions for each outcome; PR body builder still excludes probe output.

**Verify.** Invariants; live: a small API with Postgres (from B5) and three probes — create, read back, 404 — all pass; break the handler, see the fix loop run.

**Exit.** A backend-only project gets behavioural evidence from the running service.

## B7 — Frontend: probes, setup, proposed commands

**Context.** `components/VerificationPanel.tsx` (~680 lines) leads with a manual form (plan ref, test command, port, cwd, timeout, iterations) and shows evidence below. `CustomCommandsPanel.tsx` lists commands. Neither knows probes, setup runs or proposals. All HTTP goes through `src/lib/api.ts`; no component library; Tailwind utilities; icons from `lucide-react`.

- [ ] `lib/api.ts`: types for `probes` in criteria and evidence detail, `kind` on verification runs, `role` on agent runs, `proposed_by_agent_run_id` on commands; `startSetupRun(workspaceId)`.
- [ ] Verify panel: evidence first (the run list and the selected run's evidence), the manual form second and collapsed. A probes table in an observation evidence card: method, path, expected, got, pass/fail, expandable response excerpt. A probe editor (rows of method/path/expected status) in the form.
- [ ] Setup: a "Set up environment" action (Commands panel empty state and workspace overview) that starts a setup run and links to it; setup verification runs are labelled and their gate copy says "Approve environment".
- [ ] Commands panel: a "proposed by agent" badge and disabled state for proposals, with a link to the setup run that proposed them.
- [ ] Tests (Vitest + Testing Library) for each new piece; existing `VerificationPanel.test.tsx` stays green.

**Verify.** Invariants; walk the B5 and B6 live flows in the browser.

**Exit.** Everything B3–B6 added is usable without an API client.

## B8 — Review queue as the home page

**Context.** `routes/DashboardOverviewRoute.tsx` (the `/` dashboard) shows database/Redis/runtime health and usage: an operator's view. Under the spec the first question is "what is waiting for me?". Gates that wait: spec phases at `awaiting_review` and verification runs at `review`.

- [ ] `GET /dashboard/review-queue`: for the workspaces the caller can access (`services/access.py` rules, same scoping as `/dashboard/summary`), verification runs in `review` (with `kind`, workspace, source — agent run, Spec Kit workflow or setup) and spec phases in `awaiting_review`, newest first, capped (50). One query per kind, no N+1.
- [ ] Home page: the queue first, each item linking straight to its gate; empty state says nothing is waiting. Track A step A3 removes `UsagePanel` from this same file — whichever lands second rebases; if A3 has not landed, remove the panel here and tell A3. Subsystem health moves to `/settings` (owner view); the workspace status counts may stay below the queue.
- [ ] Tests: access scoping (a user never sees another team's item), ordering, cap; route test for the page.

**Verify.** Invariants.

**Exit.** Opening Enigma shows the gates that need a decision.

---

## What this track hands to Track C

Agent roles (developer asks, orchestrator routes, devops/QA/reviewer answer; the 2D floor view) are Plan 12, `2026-11-28-enigma-agent-roles.md`. It starts after B5 and B6 and reuses, without redefining them: `agent_runs.role` and `services/agent_roles.py` (B3), `register_command` and proposals (B4), the setup proof and `start_setup_verification` (B5), probes in `PlanCriteria` (B6), and the `_after_completed` dispatch (B5).

## Exit criteria for the track (B1–B8)

- A repository with an empty command registry and a Postgres dependency reaches an approved environment through one setup run, with no human-typed command.
- A backend-only change is verified by tests **and** probe evidence from the running service, on the agent's own branch.
- A UI change is verified with the app started by the loop.
- The home page lists every gate waiting for the user.
- Both execution modes pass the suite; every guard listed under **Invariants** is green.
