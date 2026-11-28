# Enigma — Lean: Cut and Refocus (Plan 10, Track A)

> **For agentic workers:** Every step below is one PR and can be executed cold: read its context brief, do its tasks, run its verification, meet its exit criteria. Tick the `- [x]` boxes as they land. Steps only remove or reposition; nothing here adds a capability (that is Track B, `2026-11-28-enigma-loop-additions.md`, and Track C, `2026-11-28-enigma-agent-roles.md`). The cross-track dependency map and the owner of every shared file is spec §14 — read it before touching a file another step also names.

**Goal:** Remove every feature the Verifiable Agent Loop does not use, and make the UI say what the product is.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §9.2 (cut), §9.3 (change). The rule for every decision: *does the loop (§3) or its governance use this?*

## Baseline (verified 2026-11-28, on `feature/verify-agent-runs` = `main` + Plan 9, PR #37)

- Migration head is `0045_verify_agent_runs`. New migrations start at `0046`.
- Migrations after `0034` must be **expand-only**: `tests/test_migration_safety.py` fails a drop/rename/retype in `upgrade()` unless it carries `# contract: <why>`, and contract steps ship **one release after** the code stopped using the object (AGENTS.md gotcha 25). So every cut step below removes *code* and leaves its table; step A10 drops the tables later.
- Importers of each cut module (`grep` of `backend/app`, 2026-11-28):

| Module | Imported by |
|---|---|
| `services/presence.py` | `api/presence.py` |
| `services/comments.py` | `api/comments.py` |
| `services/editor.py` | `api/editor.py` |
| `services/editor_alias.py` | `preview_proxy.py`, `services/editor.py`, `services/preview_alias.py` |
| `services/dev_server.py` | `api/dev_server.py` |
| `services/preview_composition.py` | `api/preview.py` |
| `services/service_diagnostics.py` | `preview_proxy.py`, `services/preview_composition.py` |
| `services/proxy_detection.py` | `services/run_detection.py`, `services/preview_composition.py` |
| `services/preview_alias.py` | `preview_proxy.py`, `api/preview.py`, `services/workspace_env.py`, `services/editor.py`, `services/preview_composition.py` |
| `services/run_detection.py` | `api/commands.py`, `services/workspace_services.py`, `services/workspace_env.py`, `services/preview_composition.py` |
| `services/run_presets.py` | `api/commands.py` |

- Frontend users: `EditorPanel` ← `routes/DashboardRoute.tsx`; `CommentThread` ← `WorktreesPanel.tsx`, `SpecPanel/index.tsx`, `AgentTab/TurnView.tsx`; `PresenceAvatars` ← `DashboardShell.tsx`; `UsagePanel` ← `routes/TeamRoute.tsx`, `routes/DashboardRoute.tsx`, `routes/DashboardOverviewRoute.tsx`; `WiringAdvisor` ← `PreviewPanel.tsx`; `CorsDiagnosis` ← `preview/ServiceRow.tsx`; `RunConfigurationTab` ← `CustomCommandsPanel.tsx`; `setupDevServer` ← `workspace/CreateWorkspaceWizard.tsx`, `routes/DashboardRoute.tsx`.
- Tables that become orphans: `comments`, `editor_sessions`, `workspace_preview_aliases`, `service_diagnostics`, plus two columns on `workspaces`: the editor-alias column (`models/workspace.py` near line 89) and `preview_alias_enabled` (line 85).
- The guard test files also contain tests *of the features being cut* (found by review, 2026-11-28): `tests/test_data_layer.py` imports `PresenceService` (~119–141) and lists `presence.py:stream_presence` (~190); `tests/test_preview_proxy_scaling.py` has diagnostics assertions (~141, 177, 477–517), alias-based cases (~26–31, 284–304, 763–790) and an editor case (~834–841); `tests/test_rate_limits.py` lists `editor/start` and `dev/setup|run|ensure` (~26–29); `tests/test_migration_names.py` reads `models/service_diagnostic.py` and `models/workspace_preview_alias.py` (~31–51).

## Invariants (run after every step)

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && pnpm lint && pnpm test && pnpm build
```

- If `pnpm` fails with a workspace error locally, run the binaries directly: `./node_modules/.bin/eslint .`, `./node_modules/.bin/vitest run`, `./node_modules/.bin/tsc -b && ./node_modules/.bin/vite build`.
- `test_settings.py::test_command_deny_dangerous_defaults_to_true` may fail in a full local run and pass alone (AGENTS.md gotcha 14). Any other failure is yours.
- The structural guards must stay green and their **rules** must not be weakened to make a cut pass. Removing a test case that exercises a cut feature is expected (each step names them); loosening a rule that applies to surviving code is not: `tests/sandbox/test_boundary.py`, `tests/test_migration_safety.py`, `tests/test_run_runtime.py`, `tests/test_data_layer.py`, `tests/test_preview_proxy_scaling.py`, `tests/test_deployment.py`.
- Each step updates `AGENTS.md` for what it removed (router count, domain list, gotchas that name the removed code).
- No step touches the database schema except A10.

## Step map

| Step | Cut | Depends on | Shares files with | Model tier |
|---|---|---|---|---|
| A1 | Presence | — | all (`router.py`, `api.ts`) | default |
| A2 | Comments | — | all (`router.py`, `api.ts`) | default |
| A3 | Usage panel placement | — | A9 (`AppSidebar.tsx`, `App.tsx`); **Track B B8** (`DashboardOverviewRoute.tsx`) | default |
| A4 | Editor (code-server) | — | A6, A7 (`preview_proxy.py`, proxy tests); A5 (`test_rate_limits.py`, `DashboardRoute.tsx`) | strongest |
| A5 | `/dev/*` facade (two PRs) | — | A4 (`test_rate_limits.py`, `DashboardRoute.tsx`) | strongest |
| A6 | Preview advisory layer | **A4** | A7 (`preview_proxy.py`, `PreviewPanel.tsx`, `preview_composition.py`) | strongest |
| A7 | Preview aliases | A4, A6 | — | strongest |
| A8 | Static run detection | **Track B B3–B5 and B7 merged** | — | default |
| A9 | Sidebar and landing | A3, A4 | — | default |
| A10 | Contract migration (also Track D's queue columns) | all of A1–A8 and D2 that have landed, **one release later** | — | strongest |

A1, A2, A3 and A5 have no dependency on each other; they can run in parallel if the workers rebase over the shared files named above (every backend cut touches `api/router.py`, every frontend cut touches `lib/api.ts`). **A4 → A6 → A7 is serial**: all three edit `app/preview_proxy.py`, and A4 and A6 touch the same statement (near line 597, the editor check that guards the CORS-diagnosis call).

Rollback for every step except A10: revert the PR. Nothing is dropped from the database before A10, so a revert restores the feature with its data.

---

## A1 — Remove presence

**Context.** Presence shows who else is looking at a workspace (avatars in the dashboard shell) over a WebSocket and Redis pub/sub. The loop never reads it. It has no test file of its own, but `tests/test_data_layer.py` uses it as the example for two rules. It owns no table.

- [ ] Delete `backend/app/api/presence.py` and `backend/app/services/presence.py`; remove the router from `backend/app/api/router.py`.
- [ ] `grep -rn "presence\|Presence" backend/app backend/tests` and remove what is left (schemas).
- [ ] `tests/test_data_layer.py`: the test near lines 119–141 builds a `PresenceService` to prove that commands stay off the subscriber client, and the list near line 190 names `presence.py:stream_presence`. Remove the presence entry from the list. Presence is the only app caller of `get_redis_client(subscriber=True)`: if `grep` confirms that, remove the `subscriber` option and its test (near line 115) too, since no code is left to protect; if another caller exists, re-point the test at it.
- [ ] Frontend: delete `components/PresenceAvatars.tsx`, remove it from `components/DashboardShell.tsx`, remove `getPresenceStreamUrl` and its types from `lib/api.ts`.
- [ ] `AGENTS.md`: router count, and gotcha 22's "see `PresenceService`" pointer (name another example or drop the pointer).

**Verify.** Invariants; `grep -rni presence backend/app frontend/src` returns nothing but unrelated words.

**Exit.** No presence route, socket, component or import remains; both suites green.

## A2 — Remove comments

**Context.** Comment threads attach to agent turns, spec phases and pull requests. The human gates already carry approve / *Request changes*, which is the feedback the loop acts on. Table `comments` stays until A10.

- [ ] Delete `backend/app/api/comments.py`, `backend/app/services/comments.py`, their schemas, and `backend/tests/test_comments.py`; remove the router.
- [ ] Take `backend/app/models/comment.py` out of the code path but do not drop the table: delete the model module and its export from `app/models/__init__.py`, and confirm no relationship on another model points at it (`grep -rn "Comment" backend/app/models`).
- [ ] If a notification type exists only for comments, remove its producer; leave stored notification rows alone.
- [ ] Frontend: delete `components/comments/`, remove `CommentThread` from `WorktreesPanel.tsx`, `SpecPanel/index.tsx` (near line 865) and `AgentTab/TurnView.tsx`; remove `listComments`, `createComment`, `resolveComment` and types from `lib/api.ts`. Fix the tests of those three panels.
- [ ] `AGENTS.md`: router count.

**Verify.** Invariants; `uv run alembic upgrade head` still succeeds on a fresh database (the table is still created by its old migration).

**Exit.** No comment endpoint or UI; `comments` table untouched; suites green.

## A3 — One home for usage

**Context.** Budget *enforcement* is governance and stays (`services/budgets.py`, `api/budgets.py`, `assert_within_budget` in `create_run`). The usage chart is shown in three places; the loop needs none of them in the workspace.

- [ ] Remove the `usage` workspace view: the route in `src/App.tsx`, the `"usage"` branch and type member in `routes/DashboardRoute.tsx`, the sidebar entry in `components/AppSidebar.tsx`.
- [ ] Remove `UsagePanel` from `routes/DashboardOverviewRoute.tsx`. Keep it in `routes/TeamRoute.tsx` (budgets are configured there).
- [ ] Backend is unchanged. Do not remove `/dashboard/usage` or `/budgets`.
- [ ] Update the affected route tests (`App.test.tsx` and any sidebar test).

**Verify.** Invariants; `grep -rn "UsagePanel" frontend/src --include='*.tsx' | grep -v test` lists only `usage/UsagePanel.tsx` and `routes/TeamRoute.tsx`.

**Exit.** Usage is reachable from the team page only; budgets still block an over-limit run (existing budget tests green).

## A4 — Remove the editor (code-server)

**Context.** The spec calls the editor "a vehicle, not a product promise" (§9.2, §10). It is the reason the sandbox image must carry code-server, and its host family (`editor-<12 hex>`) threads through the preview proxy. Files and Terminal remain for a human who needs to step in. **Security-sensitive:** the preview proxy's host classification changes; the capability path must behave exactly as before.

- [ ] Backend: delete `api/editor.py`, `services/editor.py`, `services/editor_alias.py`, `models/editor.py` (and its export), editor schemas, and the `editor_*` settings in `core/config.py` (e.g. `editor_code_server_binary`). Remove the router.
- [ ] `app/preview_proxy.py`: remove the editor-alias branch end to end (imports near line 36, the lookup near line 245, the framing/CSP and WebSocket special cases near lines 514, 615, 648). `services/preview_alias.py`: remove the `is_editor_alias_label` checks (near lines 42, 119, 151). A host that used to classify as an editor alias must now be refused the same way an unknown host is.
- [ ] Leave the `workspaces` editor-alias column and the `editor_sessions` table for A10; remove their use from the ORM model (the column may stay mapped if dropping the mapping breaks a query — say which in the PR).
- [ ] Tests: delete `tests/test_editor.py`, `tests/test_editor_alias.py`, `tests/test_preview_proxy_editor.py`; remove the editor case in `tests/test_preview_proxy_scaling.py` (~834–841) and the `editor/start` entry in `tests/test_rate_limits.py` (~26). Add one test that an `editor-…` host on the proxy is refused. Note: until A7, such a label is still *shaped* like a preview alias, so the refusal it gets is the proxy's "unknown alias" response — assert that one, and A7 tightens it.
- [ ] Frontend: delete `components/EditorPanel.tsx` and its test, the `editor` route, view branch and sidebar entry, and `startEditorSession`/`getEditorStatus`/`stopEditorSession` from `lib/api.ts`.
- [ ] Image: remove the code-server install from `sandbox/Dockerfile`; update `daytona/build-snapshot.sh` notes, `.env.example`, compose files and `deploy/` wherever they mention code-server (12 mentions outside `backend/app` on 2026-11-28). If `tests/test_deployment.py` asserts anything about it, update the assertion, not the guard.
- [ ] `AGENTS.md`: the "Custom snapshot required" paragraph (mise only now), the panel list, domain list, the `pgrep` pointer to `editor.py` under Exec hygiene (point at another example of the listen-table check or inline the rule), router count.

**Verify.** Invariants; `grep -rni "code-server\|code_server\|editor_alias" backend/app frontend/src sandbox deploy` is empty; `./sandbox/build-local-image.sh` builds.

**Exit.** No editor endpoint, proxy branch, UI or image layer; capability-host preview works exactly as before (the capability and alias cases of the proxy tests are green and unedited).

## A5 — Remove the `/dev/*` facade (two PRs)

**Context.** `services/dev_server.py` (`DevServerService`) is the pre-Epic-11 facade over `WorkspaceServiceManager`'s *primary* service (`primary_service`, `run_primary`, `stop_primary`, `status_primary`, `ensure_running_primary`). The frontend still calls it: `setupDevServer` (`CreateWorkspaceWizard.tsx`, `DashboardRoute.tsx`), plus `runDevServer`, `getDevServerStatus`, `ensureDevServer`, `stopDevServer` in `lib/api.ts`. Two ways to start the same process is the duplication to remove. **Do not remove the primary-service logic in `WorkspaceServiceManager`** — Track B step B1 calls it.

`DevServerService.setup` is not a thin wrapper: it is the install pipeline (`dev_server.py` ~107–350: `kill_primary`, `build_mise_setup_script`, `dev_worktree_id` handling, the `no-config` outcome). That is why this step is two PRs.

**PR A5a — move, change nothing.**
- [ ] Inventory in the PR description: for each `/dev/*` endpoint, the `/workspaces/{id}/services/*` call that replaces it.
- [ ] Move the setup pipeline into `WorkspaceServiceManager` (or a module beside it) behind a services endpoint (e.g. `POST /workspaces/{id}/services/setup`), with the same rate-limit dependency the `/dev/*` routes use. `/dev/*` keeps working by delegating.
- [ ] Port every `tests/test_dev_server.py` case whose behaviour survives to `tests/test_workspace_services*.py`. A dropped test whose behaviour survives is a regression.
- [ ] Move both frontend callers (`CreateWorkspaceWizard.tsx`, `DashboardRoute.tsx`) to the services API.

**PR A5b — delete the facade.**
- [ ] Delete `api/dev_server.py`, `services/dev_server.py`, the router, `tests/test_dev_server.py`, the five `*DevServer` functions and types in `lib/api.ts`, and the `dev/setup|run|ensure` entries in `tests/test_rate_limits.py` (~27–29), replacing them with the new services routes so the rate-limit coverage does not shrink.
- [ ] `AGENTS.md`: domain list (`dev_server`), router count.

**Verify.** Invariants; manual: create a workspace through the wizard, start the dev service, open its preview.

**Exit.** One API for running services; workspace creation and preview start still work; ported tests green.

## A6 — Remove the preview advisory layer

**Context.** Epic 13 added advice for humans debugging a dev server: "your frontend calls `/api` but nothing proxies it" (`services/proxy_detection.py` via `preview_composition.py`), recorded CORS findings (`services/service_diagnostics.py`, written by `preview_proxy.py`), and the UI for both (`preview/WiringAdvisor.tsx`, `preview/CorsDiagnosis.tsx`). Observe does not use any of it. The **composition descriptor itself stays**: `PreviewPanel.tsx` reads `getPreviewComposition` as its one data source; it just stops carrying advisories and diagnostics.

- [ ] `services/preview_composition.py`: remove advisories and diagnostics from the descriptor and its schema; drop the `proxy_detection` and `service_diagnostics` imports.
- [ ] `app/preview_proxy.py`: remove `DiagnosticEvent`/`ServiceDiagnosticsRecorder` (import near line 58), every call, and the `diagnostics=` parameter of `create_preview_proxy_app` (many proxy tests pass it — update their call sites). The AST rule in `tests/test_preview_proxy_scaling.py` (no module-level state) stays as it is.
- [ ] Delete `services/service_diagnostics.py`, `models/service_diagnostic.py` (table stays for A10), the `service_diagnostic_*` settings (`core/config.py` ~289–291), `tests/test_service_diagnostics.py` and `tests/test_preview_proxy_cors.py` (entirely diagnostics). Remove the diagnostics assertions in `tests/test_preview_proxy_scaling.py` (~141, 177, 477–517) and the `service_diagnostic` case in `tests/test_migration_names.py` (~44–51, it reads the deleted model file). Trim `tests/test_preview_composition*.py` to the surviving descriptor.
- [ ] Leave `services/proxy_detection.py` in place: `run_detection.py` still imports it. It goes in A8. `preview_composition.py` also imports `framework_signature` from `run_detection` (line ~32); that import stays until A8.
- [ ] Frontend: delete `preview/WiringAdvisor.tsx`, `preview/CorsDiagnosis.tsx` and their tests; remove them from `PreviewPanel.tsx` and `preview/ServiceRow.tsx`; drop the advisory/diagnostic types from `lib/api.ts`.

**Verify.** Invariants; `grep -rn "service_diagnostics\|WiringAdvisor\|CorsDiagnosis" backend/app frontend/src` is empty.

**Exit.** Preview panel lists services, health and URLs with no advice; proxy has no diagnostics writer.

## A7 — Remove preview aliases

**Context.** A preview alias is an opt-in stable hostname with a weaker security posture than the rotating capability host (`services/preview_alias.py`: host classification, a signed grant cookie, minting keyed on `custom_command_id`). The loop reaches previews through `SandboxRuntime.preview_link` and never through an alias. After A4 the editor no longer needs the alias host family. **Security-sensitive:** only the capability path may remain, unchanged.

- [ ] Read `services/preview_alias.py` fully and list, in the PR, which of its functions the capability path depends on (e.g. `classify_proxy_host`). Move those into `services/preview_capability.py`; delete the rest.
- [ ] `app/preview_proxy.py`: remove alias classification, the grant cookie and alias lookups. An alias-shaped host is refused like any unknown host.
- [ ] Remove alias use from `api/preview.py`, `schemas/preview.py`, `services/workspace_env.py` and `services/preview_composition.py` (the descriptor keeps only the capability URL form). Stop reading and writing `workspaces.preview_alias_enabled` (`models/workspace.py:85`; the column stays for A10) and delete the `preview_alias_*` settings (`core/config.py` ~279–287). Check `services/env_templates.py` for placeholders that expand to an alias URL and make them expand to the capability URL or be removed, with a test.
- [ ] Delete `models/workspace_preview_alias.py` (table stays for A10), `tests/test_preview_alias.py`, `tests/test_preview_alias_api.py`, `tests/test_preview_proxy_alias.py`. In `tests/test_preview_proxy_scaling.py` the alias-based cases (~26–31, 284–304, 763–790) exercise statelessness **through** an alias: rewrite each onto a capability host so the rule keeps its coverage, do not just delete them. Update `tests/test_migration_names.py` (~31–41), `tests/test_settings.py` (~91), `tests/test_env_templates.py` and `tests/test_preview_composition*.py`. Add a test that an alias-shaped host (including `editor-…`) is refused as an unknown host.
- [ ] Frontend: remove `enablePreviewAliases`, `disablePreviewAliases`, `regeneratePreviewAlias` and the alias controls/labels (`preview/PreviewUrlLabel.tsx`, `PreviewPanel.tsx`).
- [ ] `AGENTS.md`: gotcha 24 if it names aliases.

**Verify.** Invariants; `grep -rni "preview_alias\|PreviewAlias" backend/app frontend/src` is empty; the screenshot/observation tests (`tests/test_verification_screenshot.py`, observation tests) are green and unedited.

**Exit.** One preview URL form; proxy host handling is capability-only; verification observation unaffected.

## A8 — Remove static run detection (blocked by Track B B3–B5)

**Context.** `services/run_detection.py` guesses run commands from manifests, `services/run_presets.py` offers canned ones, `services/proxy_detection.py` guesses dev-proxy config; `RunConfigurationTab.tsx` is their UI. Track B's Prepare stage replaces guessing with an agent that actually installs, runs and registers commands. **Do not start this step until B3, B4, B5 and B7 are merged** (B7 ships the "Set up environment" action this step points users to), or a new workspace has no way to get commands at all.

- [ ] Inventory the non-UI callers first: `services/workspace_services.py`, `services/workspace_env.py` and `services/preview_composition.py` (`framework_signature`) import `run_detection` (2026-11-28). For each use, either keep a small local helper or replace it with the registered commands; write the choice in the PR.
- [ ] Delete `run_detection.py`, `run_presets.py`, `proxy_detection.py`, the detect/import/presets endpoints in `api/commands.py` (`detect_run_commands`, `import_run_commands`, `list_run_presets` and the preset apply route), and `tests/test_run_detection.py`, `tests/test_proxy_detection.py`.
- [ ] Frontend: delete `RunConfigurationTab.tsx` and its test; remove it from `CustomCommandsPanel.tsx`; remove `detectRunCommands`, `importRunCommands`, `listRunPresets`, `applyRunPreset` from `lib/api.ts`. The empty state of the Commands panel points at "Set up environment" (B7).

**Verify.** Invariants; a fresh workspace reaches a passing verification using only a setup run (Track B exit criteria still hold).

**Exit.** Commands come from the setup agent or from a human typing them; no manifest guessing remains.

## A9 — Sidebar and landing say what the product is

**Context.** `components/AppSidebar.tsx` `workspaceNav` lists twelve flat items with the loop buried in the middle. `routes/LandingRoute.tsx` still sells an "AI development control room" ("Give every coding agent a room you can see"); its workflow ends with a human checking the result and committing, and it never mentions verification or evidence. After A3 and A4, Usage and Editor are gone from the nav.

- [ ] Sidebar: three labelled groups using the existing `SectionLabel`. **Loop:** Overview, Spec, Agent, Verify, Repository. **Tools:** Files, Terminal, Preview, Commands. **Record:** Audit. No new routes, no new components, no new icons library.
- [ ] Landing: rewrite `productFeatures`, `workflowSteps`, the eyebrow, headline and lede to the Verifiable Agent Loop (spec §2 one-liner; steps Plan → Generate → Self-test → Human gate → PR). Remove the "Editor + files" card's code-server claim. Keep the layout, type scale and `frontend/design.md` rules (display weight 400, single `primary`, hairlines).
- [ ] Update `App.test.tsx` / sidebar and landing tests for the new labels and order.

**Verify.** Invariants; open `/` logged out and a workspace logged in at desktop and mobile widths.

**Exit.** The first screen and the nav both read as plan → build → prove → review.

## A10 — Contract migration (one release after the last cut)

**Context.** A1–A8 left tables no code reads. Dropping them is a contract step: it ships only after a release containing the code removal is live everywhere (AGENTS.md gotcha 25).

- [ ] Confirm the release with A2, A4, A6, A7 is deployed; if any is not merged, leave its table out of this migration.
- [ ] One migration `NNNN_drop_cut_feature_tables.py` (`revision` = filename stem) with a `# contract: <why>` comment line (the guard looks for the marker anywhere in the file, `tests/test_migration_safety.py` `CONTRACT_MARKER`; write one line per dropped object anyway, for the reader): drop `comments`, `editor_sessions`, `workspace_preview_aliases`, `service_diagnostics`, and the two `workspaces` columns (editor alias, `preview_alias_enabled`). If Track D step D2 has been live for a release, the same migration drops the queue columns it orphaned: `enqueued_at`, `claimed_at`, `worker_id`, `lease_expires_at` on `agent_runs` and `command_runs` (and any partial index on them). This step is the only contract migration across all tracks. `downgrade()` recreates the empty tables/columns.
- [ ] `tests/test_migration_safety.py` passes **because of the markers**, not because the guard was edited.
- [ ] `AGENTS.md`: migration count and head.

**Verify.** `uv run alembic upgrade head` on a copy of real data (`./scripts/backup_db.sh`, then the `./scripts/restore_db.sh <dump> --drop-after` pattern), `uv run alembic downgrade -1`, upgrade again; invariants.

**Rollback.** `downgrade` restores structure, not data. Take a `./scripts/backup_db.sh` dump before deploying.

**Exit.** No orphan tables; schema matches the models.

---

## Exit criteria for the track

- Backend routers: 23 → 19 (presence, comments, editor, dev_server removed).
- No file from §9.2 of the spec remains; the guards in **Invariants** are green and unedited in intent.
- A workspace can still: connect a repository, run an agent, verify the run (test + page observation + screenshot), pass the human gate, and open a pull request.
