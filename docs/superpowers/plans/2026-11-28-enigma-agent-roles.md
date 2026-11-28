# Enigma — Agent Roles: Ask, Route, Answer (Plan 12, Track C)

> **For agentic workers:** Every step below is one PR and can be executed cold: read its context brief, do its tasks with TDD (red → green → refactor), run its verification, meet its exit criteria. Tick the `- [x]` boxes as they land. This track **starts after Track B steps B5 and B6 are merged** (`2026-11-28-enigma-loop-additions.md`); it reuses what they built and must not redefine it. The cross-track dependency map and the owner of every shared file, column and constant is spec §14.

> **If Track D steps D1 and D2 are merged** (`2026-11-28-enigma-single-team.md`, Plan 13 — recommended before this track): Enigma has one execution mode. Ignore every task here that mentions queue mode, a job deferring with `Retry`, awaiting inside a job, the reaper, `worker_job_timeout_seconds` or a redelivered job; keep its inline half (a `run_registry` task, conditional status writes, and the startup sweep in `services/reconciliation.py` covering the new state).

**Goal:** Each bot has one clear job. A bot that needs something outside its job asks; the orchestrator routes the request to the role that owns it; the answer comes back into the asker's conversation. A human can watch all of it.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §13.

**The example that defines the track:** the developer bot is writing a feature and finds it needs Redis. It does not install Redis (it cannot). It calls `request_help(role="devops", need="Redis on 6379 for the session cache")` and its turn ends. The orchestrator starts a devops run. The devops bot installs Redis and registers its command; Enigma proves Redis listens. The orchestrator then continues the developer's conversation with "Redis is running on 127.0.0.1:6379".

## Decisions (agreed with the user, 2026-11-28)

| Decision | Choice | Why |
|---|---|---|
| Orchestrator | **Code** (a state machine), shown in the UI as the orchestrator bot | A request names its target role, so routing is deterministic, cheap and cannot skip a gate |
| Concurrency | Roles run **in turn**; one active run per workspace stays | One workspace is one sandbox and one checkout; a hand-off does not need parallel runs |
| Communication | Stored **requests and answers**, never free chat | Everything a bot said to another is visible at the human gate |
| Environment changes asked for mid-task | **Additive** ones are enabled automatically once proven and shown as evidence at the final gate; a change that replaces an enabled command waits for a human | A human gate on every dependency would stall the loop |
| Install permission | Only the devops role | That is the point of asking |
| Architect's context | The **stored artifacts** (constitution, spec, plan, tasks, the decision log, the code), never a remembered conversation | Spec Kit phase runs have no session to resume, and an answer that lives only in a chat cannot be checked by a human |
| Changing an approved plan | Never decided between bots; it goes to the human through the existing Spec Kit *Request changes* | The plan was approved at a human gate |
| View | **2D** first, drawn from data that already streams | No new library; 3D can replace the drawing later with the same data |

## Baseline

Verified in the code on 2026-11-28:

- `agent_runs` has `session_id` and `worktree_id`; a run ends `completed | error | stopped` (`TERMINAL_RUN_STATUSES`, `services/agents.py:60`); active is `thinking | executing` (`services/capacity.py:18`). **There is no paused or waiting run status**, so "the developer waits" means its run *ends* and a later run continues the same session.
- A new run continues a conversation by passing `session_id` to `AgentService.create_run` (Plan 9's fix agent does exactly this).
- The finaliser calls `_auto_verify` for every `completed` run (`services/agents.py:1244`).
- The pattern "start an agent run, wait for it without blocking, act on its result" exists in `services/verification/fixer.py` (`start` / `status` / `stop`) and `orchestrator.py` (`Step.WAIT`); in queue mode the job defers with `Retry` and never awaits the agent run (AGENTS.md gotcha 27).
- Spec Kit phases are fixed by `SPEC_PHASES` (`models/speckit.py:25`) and `PHASE_ORDER` (`services/speckit.py:74`); `implement` is hard-gated on `plan` and `tasks`.
- **A Spec Kit phase run has no session:** `services/speckit.py:425` calls `create_run(workspace_id, user_id, prompt, working_dir=working_dir)` — no `session_id`, no `worktree_id`. So there is no architect conversation to resume, and an `implement` run cannot be "continued" the way C3 continues a developer session until C8 changes that.
- The plan phase writes its design artifacts beside the spec: `research.md`, `data-model.md`, `contracts/`, `quickstart.md` (`services/speckit.py:231`).
- `verification_runs` has **no approver column**: approval is a status change plus an audit entry (`orchestrator.py` ~505–518). `_step_fix` treats `agent_run_id IS NOT NULL` as "the fix was already started" (~290), and `end` stops the agent only when the previous status was `fixing` (~463).
- `_after_completed` will run only for a `completed` run (`agents.py:1243`); startup reconciliation and the reaper write `error` straight to the row and never call the finaliser (`services/reconciliation.py` ~93–108).
- The agent stream is per run and closes when the run ends (`api/agents.py` ~326–421).
- Only the OpenHands provider gives an agent tools; `mock` emits one `list_files` and `openai` is text only (`services/agent_providers.py`).
- `auto_verify_agent_run` excludes only fix runs and Spec Kit phase runs (`services/verification/agent_link.py` ~71–85, 171–177).
- Spec Kit: `criteria_for_workflow` uses `workflow.verification` only when it has a `test_command` (`spec_link.py:51–52`); the implement gate is `PREREQUISITES` / `_assert_prerequisites` over phase rows (`speckit.py:88`, `1302`); phase finalisers are keyed by phase id.
- Verification run statuses are fixed by a CHECK: `planning|testing|fixing|observing|review|approved|rejected|error`; evidence kinds by another. A new value in either is a CHECK widening and needs a `# contract:` comment line in its migration (`tests/test_migration_safety.py`).

Provided by Track B — **confirm each exists before starting; if a name differs, use the real one and fix this plan in the same PR**:

- `agent_runs.role` with `developer | devops`, and `services/agent_roles.py` holding one profile per role (B3).
- `register_command`, disabled attributed proposals, `custom_commands.proposed_by_agent_run_id` (B4).
- `start_setup_verification`, `verification_runs.kind`, `verification_runs.setup_agent_run_id`, the setup proof, and the `_after_completed(run)` dispatch in the finaliser (B5).
- `role` on `AgentRunResponse` (B3); support services registered as `role="custom"`, `kind="service"` (B4); proposals replaced by role **and name**, and supersede limited to human-started setup runs (B5).
- `PlanCriteria.probes` and the probe executor (B6).

## Invariants (run after every step)

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && pnpm lint && pnpm test && pnpm build
```

- All invariants of Plan 11 apply (sandbox boundary, both execution modes, conditional status writes, persist before publish, no module-level in-flight state, pinned settings in tests, no output in PR bodies).
- One active run per workspace is never relaxed in this track.
- Every agent run goes through `AgentService.create_run`, so budget, caps, the draining refusal and the run guard apply to every role.
- A role's permissions come only from its profile in `services/agent_roles.py`. No `if role == …` anywhere else. The one per-run switch is `agent_runs.allow_requests` (C2), which can only *withhold* a tool the profile grants.
- Tests cannot drive tool calls through the `mock` provider (it has no tools). Every orchestration seam takes an injectable factory, as `AgentFixer` does, and is tested against a fake; tool behaviour is tested at the executor.
- Each step updates `AGENTS.md`.

## Step map

| Step | Adds | Depends on | Model tier |
|---|---|---|---|
| C1 | Role profiles for `qa` and `reviewer` | B3, B4 | default |
| C2 | Requests: the `request_help` tool and its record | C1 | strongest |
| C3 | Orchestrator routing and resume (developer → devops → developer) | C2, B4, B5 | strongest |
| C4a | QA role, by request | C3, B6 | strongest |
| C4b | QA gate before Spec Kit `implement` | C4a | strongest |
| C5 | Reviewer role: findings as evidence before the human gate | C1, B5, B6, B7 | strongest |
| C6 | 2D floor view | C3, B3 (shows C4a, C5, C7 when present) | default |
| C7 | Architect role: consultation and the decision log | C3 | strongest |
| C8 | Requests from the Spec Kit `implement` run | C3 (makes C7 useful where it matters most) | strongest |

C1 → C2 → C3 is serial. C4a, C5, C7 and C8 do not depend on each other; C5 does not need C2–C3 (the reviewer is started by the verification loop, not by a request). C6 can start once C3 is merged. **Shared files, so rebase and do not merge blindly:** C4a and C7 each add a line to `request_help`'s allowed targets (C2) and the routing table (C3); C3 and C5 both extend `services/reconciliation.py` and `services/reaper.py`; C2, C4a, C5 and C7 each add a tool to `services/openhands_tools.py`; C4b and C8 both change `services/speckit.py` (`_assert_prerequisites` vs the phase finaliser).

Rollback: revert the PR. Migrations are additive.

---

## C1 — Role profiles for `qa` and `reviewer`

**Context.** B3 created `agent_runs.role` (`developer`, `devops`) and `services/agent_roles.py`. This step adds the `qa` and `reviewer` profiles. Nothing starts them yet.

- [ ] Migration: widen the `agent_runs.role` CHECK (`ck_agent_runs_role`, named by B3) **once, for the whole track**, to `developer|devops|qa|reviewer|architect` (`# contract:` marker; widening only). `architect` gets its value here and its profile in C7, so C7 needs no migration of its own; until C7, `create_run` refuses it.
- [ ] Profiles in `services/agent_roles.py`: each is a prompt preamble, a command policy and a tool set.
  - `qa`: read-only file tools, `run_command` under the **standard** policy, plus `propose_probes` (C4a). No write, no patch.
  - `reviewer`: read-only file tools, git status and git diff, plus `report_finding` (C5). No write, no patch, no `run_command`.
- [ ] Enforce the tool set where the tool list is built per run (B4's plumbing in `services/openhands_provider.py`): a tool not in the profile is not offered. Test each role's exact tool list; test that `qa` and `reviewer` cannot write a file.
- [ ] Profile flag `auto_verify` (true for `developer` only). `verifiable_agent_run` in `services/verification/agent_link.py` refuses a run whose profile has it off. Without this, B2's default would start a verification for every QA, reviewer or architect run, and that verification would make the developer's continuation skip as `workspace_busy`.
- [ ] These roles need tools, so `create_run` refuses them on a provider that has none (`mock`, `openai`), as B4 does for `devops`. A reviewer that "completes" with no tools would read as a clean review.
- [ ] `create_run` refuses `qa`/`reviewer`/`architect` from the public agent-run endpoint (they are started by the system only), and refuses any role that has no profile.

**Verify.** Invariants.

**Exit.** Four roles exist with distinct, tested permissions (the fifth, `architect`, has its column value reserved); behaviour of `developer` and `devops` runs is unchanged.

## C2 — Requests: `request_help` and its record

**Context.** A bot asks for something outside its job by leaving a request. This step stores requests and lets the developer create one; nothing acts on them yet (C3).

- [ ] Migration: table `agent_requests` — `id`, `workspace_id`, `session_id` (the asker's conversation), `from_run_id`, `from_role`, `to_role`, `need` (text, ≤ 2 000 chars), `status` (`pending|running|fulfilled|refused|failed`), `result` (text, ≤ 4 000 chars), `handler_run_id` (nullable), `answer_run_id` (nullable: the run that delivered the answer to the asker; C3 needs it to deliver exactly once), `created_at`, `resolved_at`, `answered_at`. Same migration: `agent_runs.allow_requests` (bool, `server_default` false). Indexed on `(workspace_id, status)` and `session_id`. Partial unique index: at most one `pending|running` request per workspace (roles run in turn).
- [ ] Tool `request_help(role, need)` in the `developer` profile only. The allowed targets are a list in the developer's profile, starting with `devops`; C4a adds `qa`, C7 adds `architect`. A target with no router (C3) is refused. It records the request through a narrow callback (the same shape B4 used for `register_command`: a fresh session per call, never one held for the run).
- [ ] **Ending the turn.** The run must end `completed`, because only a completed run reaches `_after_completed`; setting `stop_event` yields `stopped` and would skip it. Find how the OpenHands SDK lets a tool finish the conversation normally and use that; write which mechanism in the PR. Whatever it is, also make every later tool call in that run answer "a request is open; end your turn", so an agent that keeps going cannot act while it is supposed to wait. Test that the run ends `completed`.
- [ ] Limits: 5 requests per session (`AGENT_REQUESTS_PER_SESSION`), refused beyond that with a message the agent can read. A second request while one is open is refused.
- [ ] Who may ask is decided **when the run is created**, not when tools are built: a Spec Kit phase run and a verification fix run are linked to their phase or verification only *after* `create_run` returns, so nothing at tool-build time can tell them apart. `create_run(..., allow_requests=False)` stamps `agent_runs.allow_requests`; the public agent-run endpoint passes `True` for a `developer` run; Spec Kit (until C8) and the fixer leave it false. The tool is offered only when the profile has it **and** the row allows it. `devops`/`qa`/`reviewer`/`architect` profiles do not have it (no chains in this track). Test each.
- [ ] `GET /workspaces/{id}/agent/requests?session_id=` (workspace access rules). Audit `agent_request_created`.
- [ ] Live updates: a request changes state *after* the asker's run has ended, and the agent stream is per run and closes with it, so it cannot carry these. After each committed change publish a hint on the `notifications:` topic (persist before publish); the list endpoint is the source of truth and clients refetch from it.

**Verify.** Invariants.

**Exit.** A developer run can leave one well-formed, capped request; it is stored, listed and audited.

## C3 — Orchestrator routing and resume

**Context.** The orchestrator is code. When a developer run ends with a pending request, it starts the role that owns the need, waits without blocking, and continues the developer's conversation with the answer. This step delivers the Redis example end to end.

- [ ] `services/agent_orchestration.py`: `advance(request_id)` does one step from the row, every transition a conditional UPDATE (same discipline as `VerificationOrchestrator.advance`):
  - `pending` → start the handler run (`create_run` with `role=to_role`, a prompt built from `need`, the asker's `worktree_id`, **no `session_id`** — the handler is found through the request row, not the asker's conversation — and the asker run's `user_id`, since `agent_runs.user_id` is not null and `create_run` checks drive access) → `running`, `handler_run_id` set.
  - `running`, handler still active → wait.
  - `running`, handler `completed` → for `devops`: find its setup proof by `verification_runs.setup_agent_run_id` (B5) and wait for it, then apply the **approval rule** below → `fulfilled` or `refused`, with `result` written. **No proof exists** (B5's start was refused, or the run registered nothing) → `failed` ("the devops run proposed nothing to prove"); never wait for a run that will not come.
  - handler `error`/`stopped` → `failed`. `create_run` refused for a reason that will not change — budget, caps, the command allowlist being on (B3's 409), a provider without tools (B4), lost drive access — → `failed`, with the reason as `result`. Refused because the app is **draining** or the workspace is briefly busy → defer and try again, as the fixer does with `FixDeferredError`.
  - request terminal and `answer_run_id` is null → start a new `developer` run in `session_id` (same worktree, the asker's `user_id`, `allow_requests=True`) whose prompt is the answer, and record it as `answer_run_id` with a conditional UPDATE. This is what makes delivery exactly-once: a replayed `advance` sees the id and does nothing. If *this* `create_run` is deferred, retry; if it is refused for good (budget), the answer cannot be delivered by a run: leave `answer_run_id` null, set `answered_at`, and send a notification to the user with the answer. "The developer always hears back" holds whenever a run can still be started, and the human hears back otherwise.
  - `pending` whose asker run is terminal but not `completed` (stopped by a human, errored, reaped) → `failed`, no answer run. This sweep matters: the finaliser never ran for that asker, and C2's one-open-request-per-workspace index would otherwise block the workspace for good.
- [ ] Driving it: inline, a `run_registry` task; in queue mode, a job that defers with `Retry` between steps. Never await an agent run inside a job. Startup reconciliation (`services/reconciliation.py`) and the reaper (`services/reaper.py`) get a requests pass: `running` with a dead handler → `failed` then answered; and the `pending` sweep above. C5 extends the same two files; keep each pass a separate function.
- [ ] Finaliser: add one branch to `_after_completed(run)` (B5), keeping the order **role → pending request → auto-verify**. A developer run that ended with a pending request is *not finished work*: it must not be auto-verified. The run that continues the session afterwards is verified normally.
- [ ] **Approval rule** for a devops run started by a request (B5's human gate still applies to a setup run a human started):
  - A setup run started by a request never supersedes, and is never superseded by, a human-started one (B5 keys its supersede rule on human-started runs). If a human-started setup run is still waiting at `review` — the first environment is not approved yet — the request's run waits at `review` too: the first environment gate stays a human's.
  - Proof passed and every proposal is **additive** — its role is `install`, `migrate`, `seed` or `custom` (a support service), and no enabled command has the same role and name. A proposal with role `dev` or `test` is never additive: those mean "the app" and "the suite". → proposals are enabled automatically and the setup run becomes `approved`. `verification_runs` has no approver column (see Baseline), so add `verification_runs.auto_approved_reason` (nullable text, set to `additive` here) in this step's migration and audit it as `verification_run_auto_approved`; the UI shows "auto-approved: additive" from that column. The evidence is linked from the requesting session so the final gate shows it.
  - Proof passed but a proposal replaces an enabled command → the setup run waits at `review` for a human; the request stays `running` until they decide.
  - Proof failed → proposals stay disabled, request `refused`, the failing evidence summarised in `result` (never raw output beyond the 8 KiB tail already stored as evidence).
- [ ] Stop: stopping the handler run ends the request `failed` and the developer is answered with that. There is no "stop a session" operation; deleting the session (`delete_session`; the request's FK is `SET NULL`) ends its open request `failed` with no answer run.
- [ ] Tests: each transition; replayed `advance` is harmless; the three approval outcomes; no auto-verify on a run with a pending request; queue-mode deferral; reconciliation of an orphaned `running` request.

**Verify.** Invariants; live E2E on a local OpenSandbox workspace: a developer prompt that needs Redis → request → devops run → proof shows Redis listening → auto-enabled → developer continues and its code connects.

**Exit.** The Redis example works with no human action between the ask and the answer, and every hand-off is a stored, visible row.

## C4a — QA role, by request

**Context.** Track B lets a run carry API probes, but whoever writes the code could also write an easy exam. The QA role writes the probes separately. This step covers ordinary developer sessions; the Spec Kit gate is C4b.

- [ ] Tool `propose_probes(probes)` in the `qa` profile: validates with B6's probe schema and limits and stores them on the asker's session — new nullable JSON column `agent_sessions.proposed_criteria` (`{"probes": [...]}`), written through a narrow callback. One call per QA run; a QA run that ends without it ends the request `failed`.
- [ ] Add `qa` to `request_help`'s allowed targets (C2) and to the routing table (C3). The handler prompt points at the code and, when the session's worktree belongs to a Spec Kit workflow, at its spec artifacts. The answer the developer receives lists the probes.
- [ ] Using them: where criteria for an agent run are derived (`agent_link`, the function B6 already taught to fill `observe_port`), merge the session's `proposed_criteria.probes` into the criteria. A verify request that names its own probes in the body wins over the session's.
- [ ] Frozen: no tool lets a `developer` or fix run write `proposed_criteria`; assert it in a test. A later QA request in the same session replaces the list (and is visible as a new request row).
- [ ] The human sees who wrote the probes: the evidence card (B7) labels probes that came from a QA run, with a link to the request.
- [ ] Tests: schema reuse; routing; merge and precedence; the frozen rule; a QA run without `propose_probes`.

**Verify.** Invariants; live: a backend task where QA's probes catch a handler the developer's own tests missed.

**Exit.** In an ordinary session, behavioural criteria can come from a role that did not write the code, and the human sees that at the gate.

## C4b — QA gate before Spec Kit `implement`

**Context.** For a Spec Kit workflow the right moment for QA is *before* code exists: after `tasks` is approved, QA proposes probes from the spec, plan and tasks, and a human approves them. This cannot reuse C4a's storage or routing: phase runs have no session, `criteria_for_workflow` ignores `workflow.verification` unless it carries a `test_command`, and the implement gate is a check over phase rows (see Baseline). **Do not add a phase to `SPEC_PHASES`**: a new phase touches a CHECK, `PHASE_ORDER`, the UI and every workflow in flight. This is a gate on the workflow.

- [ ] Migration: `workspaces.qa_before_implement` (bool, default false); `spec_workflows.qa_probes` (JSON, nullable), `spec_workflows.qa_status` (`none|running|proposed|approved`, default `none`; a new CHECK → `# contract:` marker), `spec_workflows.qa_agent_run_id` (nullable FK).
- [ ] Driver: with the option on, approving `tasks` starts one `qa` run in the feature worktree (through `create_run`, `allow_requests=False`), `qa_status = running`. Register its completion the way phase finalisers are registered in `run_registry`, keyed so it cannot collide with a phase id. `propose_probes` writes `spec_workflows.qa_probes` for this run (the callback is built from what the run is attached to: a session in C4a, a workflow here) → `proposed`. A failed or empty QA run returns to `none` with the reason shown; the human may retry or switch the option off.
- [ ] Endpoints under `/workspaces/{id}/spec/workflows/{wid}/qa`: `POST …/approve` (owner; → `approved`), `POST …/retry`. The human may edit the list before approving (same schema validation).
- [ ] Gate: `_assert_prerequisites("implement")` also requires `qa_status == "approved"` **when the workspace option is on**; with it off nothing changes. A workflow already past `tasks` when the option is switched on is not blocked retroactively (treat `none` as satisfied if `implement` has ever started).
- [ ] Criteria: `criteria_for_workflow` merges approved `qa_probes` into whichever criteria it resolves (the workflow's own `verification`, or the derived test command), and returns none only when there is neither a test command nor probes.
- [ ] Re-planning: *Request changes* on `plan` or `tasks` resets `qa_status` to `none` and clears `qa_probes`, so stale probes never gate a changed plan.
- [ ] Frontend (`components/SpecPanel/`): a QA card between `tasks` and `implement` showing status, the probes, edit, approve, retry. `SpecPanel/index.tsx` is also edited by Track A step A2 (removing comments) — rebase.
- [ ] Tests: option on/off; the gate; retroactive rule; merge into both criteria sources; reset on re-plan; failed QA run; queue mode.

**Verify.** Invariants; live: a Spec Kit workflow with the option on stops before `implement` until the probes are approved, and its verification evidence shows those probes.

**Exit.** With the option on, a Spec Kit change is tested against criteria a human approved before any code was written; with it off, Spec Kit is exactly as before.

## C5 — Reviewer role: findings before the human gate

**Context.** After self-test passes and before a human looks, a read-only reviewer reads the diff. Its findings are evidence, so the human reviews code that has already been read once. Opt-in per workspace, because it costs a run on every verification.

- [ ] Migration: `workspaces.review_before_gate` (bool, default false); `verification_runs.reviewer_run_id` (nullable FK `agent_runs`); evidence kind `review` and verification run status `reviewing` added (two CHECK widenings, `# contract:` marker). The reviewer run gets its **own column**: `_step_fix` reads `agent_run_id IS NOT NULL` as "the fix was already started", so storing the reviewer there would make a blocking finding skip its fix. A new status is needed because the loop must *wait for an agent run* here, and waiting needs a state it can resume from. Update everything that enumerates statuses or evidence kinds — grep for each existing value rather than trusting this list: `VERIFICATION_RUN_STATUSES` and `VERIFICATION_EVIDENCE_KINDS` (`models/verification.py`); the status and evidence-kind `Literal`s in `schemas/verification.py` (~7–8; a response with an unknown value fails validation); `ACTIVE_STATUSES` and the `advance` dispatch (`orchestrator.py` ~50, ~188–207); `VerificationOrchestrator.end`, which today stops the agent only when the previous status was `fixing` (~463) — generalise it to stop `reviewer_run_id` when the previous status was `reviewing`; the reaper and startup reconciliation; and the frontend copies in `lib/api.ts` (~2793), `VerificationPanel.tsx` (~24, ~564), `AgentTab/useRunVerifications.ts` (~10) and `AgentTab/TurnVerification.tsx` (~13).
- [ ] Orchestrator: with the option on, `observing` → `reviewing` (starts one `reviewer` run on the verified worktree, through a `Reviewer` seam shaped like `Fixer`) → on completion `review`. A refusal to start (budget, caps) is recorded as `review` evidence with status `error` and the run goes to the gate; it never stays active.
- [ ] Tool `report_finding(severity, file, line, summary)` in the `reviewer` profile; `severity` ∈ `blocking | advisory`; capped (30). Each is stored in one `review` evidence row's `detail.findings`.
- [ ] A `blocking` finding with iterations left sends the run to `fixing` (clear `agent_run_id` on that transition so `_step_fix` starts a new fix), with the findings in the fix prompt (the prompt builder in `fixer.py` that B6 extended for failing probes); otherwise the run reaches the gate with the findings shown. The reviewer runs again after a fix, within the same `max_iterations` budget — never an unbounded loop.
- [ ] Findings appear in the PR body as a count by severity only (`services/spec_pull_request.py` rule: no raw text from evidence).
- [ ] Tests: transitions with the option on and off; stop while `reviewing`; reaper/reconciliation; blocking → fix → re-review within budget; frontend evidence card.

**Verify.** Invariants; live: a change with an obvious defect the tests do not cover is sent back by the reviewer and fixed before the gate.

**Exit.** With the option on, no change reaches a human unreviewed; with it off, the loop is exactly as before.

## C6 — 2D floor view

**Context.** The user wants to *see* the bots work. Everything to draw already exists as data: runs with their role and status, agent events (tool calls, file changes), requests and their answers (C2–C3), evidence. This step is a new drawing of that data and adds no backend state.

- [ ] One read endpoint, `GET /workspaces/{id}/agent/floor?session_id=`: the session's runs (role, status, started/ended), its requests with their handler and answer runs (handler runs have no session; they are reached through `agent_requests.handler_run_id`), and the latest verification run's status with its reviewer run if any. One query per kind. `role` comes from `AgentRunResponse` (B3); add the TypeScript field in `lib/api.ts` if B7 has not.
- [ ] View `Floor` in the workspace's **Loop** group (`components/AppSidebar.tsx`, the grouping Track A step A9 introduces; if A9 has not landed, add the entry to the flat list and let A9 group it). Route in `src/App.tsx`, panel in `src/components/FloorPanel/`.
- [ ] Drawing: inline SVG and Tailwind only — no canvas or 3D library, no component kit, icons from `lucide-react`. One station per role (orchestrator, developer, devops, QA, reviewer, architect — a station appears only if its role's step has landed); a station shows idle / working / waiting and what it is doing now (last tool call, from the event stream). A request is a line from asker to orchestrator to handler, labelled with `need`; an answer travels back. Artifacts (plan, probes, evidence, findings) are small markers that move along the loop. Clicking a station opens that run; clicking a request opens its text.
- [ ] Live updates, two sources: the **active run's** tool calls from its own agent stream (`getAgentStreamUrl`), opened for whichever run is active and reopened when the active run changes; and **everything between runs** (request transitions, a handler starting, an answer delivered) from refetching the floor endpoint when the notifications stream (`getNotificationStreamUrl`, C2's hint) says something changed, with a slow poll as the fallback. The per-run stream closes when its run ends, so it alone would freeze the floor exactly when a hand-off happens. Backfill-then-tail: refetch on every reconnect.
- [ ] `frontend/design.md`: warm canvas, hairlines, one `primary`, the five pastel timeline colours for the five agent roles (developer, devops, QA, reviewer, architect); the orchestrator station is neutral, because it is code. Respect `prefers-reduced-motion`: no travelling animation, state changes only. Every station and request is reachable by keyboard and has a text label; the view must make sense with motion off.
- [ ] Tests: renders each station state from fixture data; a request draws its three hops; reduced motion; reconnect refetch.

**Verify.** Invariants; run the C3 Redis example and watch it on the floor at desktop and mobile widths.

**Exit.** A person watching the floor can tell which bot is working, who asked whom for what, and where the work is in the loop — without reading a log.

## C7 — Architect role: consultation and the decision log

**Context.** The developer hits a question the plan does not settle ("the plan says an in-memory cache, but there are two instances — Redis or memory?"). It should ask the architect, not guess. The architect has no memory of writing the plan: Spec Kit phase runs have no session (see Baseline). Its context is what is stored — the spec artifacts, the decision log, the code — which is also what a human can read. Every answer is written down so the next consultation, and every other role, sees the same decisions.

- [ ] Profile `architect` in `services/agent_roles.py` (the column value exists since C1): read-only file tools, search, git status and git diff, plus `record_decision` (below). No write, no patch, no `run_command`.
- [ ] Add `architect` to `request_help`'s allowed targets (C2) and to the routing table (C3). The handler prompt is built from `need` plus **pointers, not copies**: the paths of the feature's spec directory (`spec.md`, `plan.md`, `tasks.md`, `research.md`, `data-model.md`, `contracts/`), `.specify/memory/constitution.md` if present, and the decision log. Resolve the spec directory from the session's worktree → its `SpecWorkflow` (the workflow already knows its feature directory); confirm the exact field in `models/speckit.py` and use it, do not rebuild the path by hand.
- [ ] Tool `record_decision(kind, title, decision, rationale)` — the architect's only way to answer:
  - `kind="clarification"`: the plan does not settle this; here is the decision. The backend appends an entry (date, title, decision, rationale, the asking run's id) to `<spec dir>/decisions.md` in the worktree through the sandbox file API — a fixed path, so the architect still cannot write anywhere else. The request ends `fulfilled` with the decision as `result`.
  - `kind="plan_change"`: answering would contradict the approved plan. Nothing is written to the plan or the log. The request ends `refused` with the proposal as `result`; the developer is told not to work around it; a notification goes to the workspace's users (existing `NotificationService`) saying the plan needs a human decision. The human acts through the existing Spec Kit *Request changes* on the `plan` phase. **Two bots never change an approved plan.**
  - Exactly one `record_decision` per architect run; a run that ends without one ends the request `failed`.
- [ ] `decisions.md` is an ordinary file in the feature worktree: it is committed with the rest when the PR is opened (`open_pull_request` commits the worktree's uncommitted changes), and linked from the PR body with the other artifacts (`services/spec_pull_request.py`; link only, no content).
- [ ] Outside Spec Kit (an ordinary session with no workflow) there is no spec directory: the architect gets the code and the question only, `clarification` is stored on the request row and no file is written, `plan_change` cannot occur (there is no approved plan). State this limit in the tool's description so the developer knows what it is asking.
- [ ] Other roles read the log: add the `decisions.md` pointer to the developer's, QA's and reviewer's prompts when the file exists (one helper, used by all three).
- [ ] Tests: the architect's exact tool list; it cannot write a file or run a command; `clarification` appends one well-formed entry and fulfils the request; `plan_change` writes nothing, refuses, notifies; no-workflow session; a run without `record_decision` fails the request; the PR body links the log without its content.

**Verify.** Invariants; live: in a Spec Kit feature worktree, a developer question the plan does not settle → architect answers → entry in `decisions.md` → developer continues accordingly; then a question that contradicts the plan → refused, human notified.

**Exit.** The developer can consult the architect; every answer is a written decision other roles read; an approved plan can only be changed by a human.

## C8 — Requests from the Spec Kit `implement` run

**Context.** Until now only ordinary developer sessions can ask for help. The Spec Kit `implement` run — where a missing dependency or an unsettled design question is most likely — cannot, for two reasons found in the code: it has no session to continue (`speckit.py:425`), and its phase finaliser (`_finalize_phase_inner`) treats the run's completion as the phase's completion and starts verification. A run that stopped to wait for an answer is not a finished phase.

- [ ] `implement` runs in a session: `run_phase` passes a `session_id` (created for the phase), the feature worktree's `worktree_id` and `allow_requests=True` to `create_run`. Check what passing `worktree_id` changes compared with today's `working_dir`-only call (branch targeting, file-change tracking) and keep the phase's behaviour identical; say what you found in the PR. Other phases stay as they are.
- [ ] That flag is the whole switch: C2 offers `request_help` to a `developer` run whose row allows it. Only `implement` passes it; the other phases write artifacts and have nothing to ask for.
- [ ] Phase finaliser: when the phase's run completes **with a pending request**, the phase stays `running` and is not finalised; no verification starts. When C3 continues the session, the phase's `agent_run_id` moves to the continuation run (conditional UPDATE, under the same phase-row lock the approval takes). Finalisers are keyed by phase id, so the registration survives; it must now wait on the phase's *current* run. Only a completion with no pending request finalises the phase.
- [ ] Token and cost totals for the phase include every run of the session, not just the last.
- [ ] Failure paths: a request that ends `failed`/`refused` still continues the developer (C3), so the phase always reaches a real completion or error. Stopping the phase stops the open request and its handler run. Startup reconciliation: a phase left `running` whose session has an orphaned request is ended `error` like any interrupted phase.
- [ ] The two gates of gotcha 28 are untouched: evidence still gates the workflow, `approve_phase("implement")` is still refused when criteria exist, and stale evidence is still superseded when implement re-runs.
- [ ] Tests: implement asks devops (Redis) and finishes after the answer, verification starts once and only at the end; implement asks architect (C7, if landed); stop mid-request; restart mid-request; totals across the session's runs; queue mode.

**Verify.** Invariants; live: a Spec Kit workflow whose implement needs Redis completes with one verification run and the request visible in the workflow.

**Exit.** The Redis and architect examples work inside a Spec Kit workflow, and a workflow never completes or verifies on a run that was only waiting for an answer.

---

## Not in this track (named so nobody adds them by accident)

- **Parallel runs.** Relaxing "one active run per workspace" to "one per worktree" needs per-worktree dev services and ports, a test database per bot, and bigger sandboxes. It is a separate plan, after this track.
- **Free-form bot-to-bot chat.** Requests and answers are the whole protocol here.
- **An LLM orchestrator.** Only worth it when the orchestrator must split a large goal into its own plan.
- **Chains** (a handler asking another role). Capped at depth 1 in C2.
- **3D view.** Same data as C6; a later replacement of the drawing.
- **Refactor and design roles.** No loop stage needs them yet.

## Exit criteria for the track

- The Redis example runs end to end with no human action between ask and answer.
- Each role (developer, devops, QA, reviewer, architect) has a tested, distinct permission set defined in one module.
- A developer can consult the architect; the answer is a written decision, and an approved plan changes only through a human.
- Requests work from ordinary sessions and from the Spec Kit `implement` run.
- Every hand-off between roles is a stored row a human can read at the gate.
- QA-authored probes and reviewer findings appear as evidence; both are opt-in and the loop is unchanged when they are off.
- The floor view shows all of it live.
