# Enigma — Verifiable Agent Loop: Pull Request from an Approved Workflow (Plan 4)

> **For agentic workers:** Execute task-by-task with TDD (red → green → refactor). Tick the `- [x]` steps as they land.

**Goal:** Close spec §3's loop with its last step, **PR → Merge**. A completed Spec Kit workflow, whose `implement` evidence a human has approved, is handed over as a GitHub pull request:
- The feature branch is pushed with exactly the verified code.
- The PR body carries the spec, the plan and a summary of the verification evidence.
- The PR's link is shown in the SpecPanel.

The merge stays a human decision on GitHub.

**Spec:** `docs/superpowers/specs/2026-11-26-enigma-verifiable-agent-loop-design.md` §3 (step 5), §5 (step 5).
**Previous plans:** Plan 3 `2026-11-27-enigma-verification-spec-kit.md` (PR #24), plus PR #25 (spec kit init retry).

## Current state (verified in code)

- **Workflow fields:** a workflow has `branch`, `spec_dir` and `worktree_path` (`models/speckit.py`). The gate sets it to `completed` (`spec_link.approve_verification_run`), or `approve_phase("implement")` does when there are no criteria.
- **Verification fixes are never committed.** `_commit_phase_artifact` (`services/speckit.py`) commits only after a phase's agent run. The `AgentFixer` runs that make the tests pass write to the worktree afterwards, so the verified tree is the worktree's **working tree**, not its HEAD. Pushing HEAD today would ship the code that *failed* the tests.
- **Push:** `RepositoryService.push(workspace_id, owner_id, worktree_path=...)` runs `push --set-upstream origin HEAD` with per-command auth and redacts output.
- **PRs:** `PullRequestService.create(...)` (`services/github_client.py`) requires a GitHub token credential. Otherwise it raises `PullRequestsUnsupportedError` or `RepositoryCredentialNotFoundError`, both mapped to 409 by `api/workspaces.py` `repository_error`. It caches the PR in `pull_requests` (`models/worktree.py`; `worktree_id` is nullable, and spec worktrees have no `WorkspaceWorktree` row).
- **Existing endpoint:** `POST /workspaces/{id}/repo/pull-requests` creates a PR inline (with audit + notification). Re-creating a PR for a head that already has one gets a GitHub 422 ("A pull request already exists").

## Design

**Trigger:** an explicit **Open pull request** action on a `completed` workflow, `POST /workspaces/{id}/spec/workflows/{wid}/pull-request` (201).
- The UI shows the button as soon as the evidence is approved.
- It is not automatic, because a push is an outward-facing action on the user's remote, and the token's owner should take it knowingly. *(Gate 1 decision.)*

**Steps** (`SpecKitService.open_pull_request`, reusing its workspace and file helpers; the body lives in `services/spec_pull_request.py`):
1. **Guard** (then `PullRequestService.ensure_supported` before anything is pushed):
   - The workflow is in the workspace and the user is the workspace owner (as for all git/PR operations).
   - `status == "completed"`.
   - Its `worktree_path` is set.
   - If `pull_request_id` is already set, the existing PR is returned (idempotent; 200 instead of 201).
2. **Commit the verified tree:** commit any uncommitted changes in the worktree as `spec(<slug>): verification fixes`, attributed to the workflow's user (the same author rules as `_commit_phase_artifact`). Nothing to commit is fine.
3. **Push:** `RepositoryService.push(worktree_path=workflow.worktree_path)`.
4. **Create the PR:**
   - `PullRequestService.create(head=workflow.branch, base=None → default_branch, title="<feature_name>", body=...)`.
   - If GitHub says a PR already exists for that head, adopt it: list open PRs by `head`, upsert, and link. This makes a retry after a lost response safe.
5. **Link:** set `spec_workflows.pull_request_id` (migration `0043`, nullable FK to `pull_requests` `ON DELETE SET NULL`). Record audit `spec_pull_request_opened` and a `pull_request_opened` notification, best effort.

**PR body** (pure function `build_pr_body`, capped at 60,000 chars, under GitHub's 65,536):
- **Feature:** the workflow description.
- **Spec Kit artifacts:** links to `spec.md`, `plan.md` and `tasks.md` on the branch (`https://github.com/<owner>/<repo>/blob/<branch>/<spec_dir>/…`), plus a short excerpt of `spec.md` (first ~3,000 chars) read from the worktree.
- **Verification:** from the latest **approved** linked run:
  - test command
  - pass/fail
  - number of fix attempts
  - observation summary (if any)
  - when it was approved (the date only: no approver identity in a possibly public PR)

  Or "Not verified (no criteria)" when there is none.
- **Never includes test or command output.** It can hold secrets, and the PR may be public.

**Failures** (nothing is half-linked; the action can simply be retried):
- No credential or not GitHub+token → 409, as `repository_error` already says.
- A push or GitHub error → mapped through the same `repository_error` (redacted messages). The workflow stays unlinked.

**Frontend:** on a `completed` workflow, the SpecPanel shows **Open pull request**. After success, or when `pull_request` is set, it shows `PR #12 · open` linking to GitHub. A 409 shows its message inline (e.g. "Connect a GitHub token…").

## Global Constraints

- Migration `0043` is expand-only: one nullable column with an inline FK, plus an index.
- Async-only, no module-level state. Git runs through `RepositoryService` / `SandboxRuntime.exec`, never the agent tool sandbox.
- Tokens never leave the backend. Every surfaced git or GitHub message goes through `sanitize_git_output` (existing).
- The rate limit matches the other sandbox actions (`enforce_sandbox_rate_limit`).

## Review Focus

- **The PR ships what was verified:** the fix commit happens before the push, and the push targets the feature worktree's branch, never the base checkout.
- **Idempotent and retry-safe:** a retry, a lost response or a PR opened by hand all end with one linked PR (the button is disabled while a request is in flight).
- **Authorization:** owner-only, workspace-scoped lookup (404 for another workspace's workflow), same as the gate.
- **No secret leakage:** no command output in the PR body; error messages are redacted.

---

### Task 1: Link a workflow to its pull request (migration `0043`)

**Files:** `alembic/versions/0043_spec_workflow_pull_request.py`, `models/speckit.py`, `schemas/specs.py`, model/schema tests.

- [x] Write failing tests:
  - `spec_workflows.pull_request_id` (nullable, FK `pull_requests` `ON DELETE SET NULL`, indexed)
  - `SpecWorkflowResponse.pull_request` (`number`, `url`, `state`, or `null`)
- [x] Implement, `alembic upgrade`/`downgrade`/`upgrade`, migration-safety test green.

### Task 2: Build the PR body

**Files:** `services/spec_pull_request.py` (new), `tests/test_spec_pull_request.py`.

- [x] Write failing tests for `build_pr_body(workflow, repo_url, spec_excerpt, evidence)`:
  - artifact links built from the repo URL + branch + `spec_dir`
  - the evidence summary for an approved run (command, passed, fix attempts, approver)
  - "Not verified" when there is no run
  - the excerpt is truncated, and the body is capped at 60,000 chars
  - no evidence output text ever appears in it
- [x] Implement, green.

### Task 3: Open the PR from a completed workflow

**Files:** `services/spec_pull_request.py`, tests.

- [x] Write failing tests (fake runtime + fake `GitHubClient`):
  - A completed workflow → commit `spec(<slug>): verification fixes` in the worktree, push from the worktree, PR head=`branch` and base=default, workflow linked, audit + notification.
  - Nothing to commit → still pushes and opens.
  - Not `completed` → `SpecWorkflowNotReadyForPullRequestError` (409). Already linked → returns the linked PR, no push.
  - GitHub "already exists" → adopts the open PR for that head and links it.
  - Push fails → error raised, workflow not linked.
  - No credential / not GitHub token → the existing errors propagate.
- [x] Implement, green.

### Task 4: API

**Files:** `api/specs.py`, `tests/test_specs.py`.

- [x] Write failing tests for `POST /workspaces/{id}/spec/workflows/{wid}/pull-request`:
  - 201 with the PR; 200 when it was already linked
  - 404 for a non-owner (spec routes are owner-only; another user's workspace is not found)
  - 409 when not completed, no credential, or the provider is unsupported
  - the rate-limit dependency is applied
  - `GET` workflow includes `pull_request`
- [x] Implement (reuse `repository_error` for git/PR errors), green.

### Task 5: Frontend

**Files:** `lib/api.ts` (`openSpecPullRequest`, `SpecWorkflow.pull_request`), `components/SpecPanel/index.tsx`, `SpecPanel.test.tsx`.

- [x] Write failing tests:
  - a completed workflow shows **Open pull request**, and clicking it calls the API and then shows the PR link
  - a linked workflow shows the link and no button
  - a 409 shows the server message
  - an incomplete workflow shows no button
- [x] Implement, green (`tsc -b`, `eslint`, `vitest`, `vite build`).

### Task 6: Docs + live check

- [x] AGENTS.md note 29 (completed workflow → PR; fixes committed before push; `0043`), and tick this plan.
- [ ] Live E2E (optional, needs a throwaway GitHub repo + token; confirm with the user first): workflow → evidence approved → Open pull request → the PR exists on GitHub with the fix commit, and the SpecPanel shows its link.

## Review fixes (applied)

- **Confined spec read:** the excerpt is read with `confined_read_command`. It refuses a `spec.md` that resolves outside the worktree, either itself a symlink or under a symlinked directory, and caps the bytes read. An agent-planted link to `~/.git-credentials` can therefore never reach the PR body. This is tested against a real shell.
- **Inert excerpt:** the agent-written excerpt sits in a fenced code block longer than any backtick run inside it, so its `@mentions` and `#refs` notify no one.
- **Re-run unlinks:** re-running `implement` clears `pull_request_id` (`run_phase`), so the next completion commits and pushes again. A still-open PR is re-adopted through GitHub's "already exists".
- **Known, unchanged:** `RepositoryService.commit` stages with `git add -A`, the same as the per-phase commits. Anything in the worktree that `.gitignore` does not cover is pushed.

## Out of scope

- Auto-merge, PR status sync (merged/closed) back into the workflow, and providers other than GitHub (existing limitation).
- Re-opening a PR for a workflow whose PR was closed. The link stays; the user can open one by hand from the Worktrees panel.
- The Plan 2 known gaps (Stop button, lost-queue-job reaper, screenshot rendering).
