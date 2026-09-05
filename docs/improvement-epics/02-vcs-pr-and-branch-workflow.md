# Epic 02 - VCS: Pull Requests & Branch Workflow (Git Worktrees)

## Goal

Close the "AI writes code → review → ship" loop, with **git worktrees** as the branching
primitive. Today the agent edits files, commits, and pushes against a single working tree on
one branch, with a hardcoded commit author and no PR. Adopt a worktree-per-branch model so
multiple branches/features live side-by-side in one sandbox — each in its own isolated
directory — then add per-user commit identity and GitHub Pull Request creation/status.

## Why Git Worktrees

A workspace is a single Daytona sandbox with one clone at `workspace_path`. Switching
branches with `git checkout` mutates that shared tree in place, which:

- thrashes the dev server / preview / code-server pointed at it,
- clobbers in-progress edits, and
- makes **concurrent** work on two branches impossible.

`git worktree` attaches multiple working directories to the **same** repository (shared
`.git` object store, one checkout per worktree). That gives Enigma:

- **Isolation** — the SDD `implement` phase (Epic 01) runs on a feature branch in its own
  worktree; the base tree stays clean.
- **Parallelism** — multiple agent runs / spec workflows execute on different branches at
  the same time with no checkout contention.
- **Cheap** — worktrees share objects, so they cost working-tree disk only, not a full clone.

This mirrors the isolation pattern the platform already wants for parallel agent execution.

## User Value

Developers (and the SDD workflow in Epic 01) can run several features in parallel inside one
workspace and take any branch all the way to a reviewable Pull Request — without leaving
Enigma and without branches stepping on each other.

## User Stories

- As a developer, I can create a feature branch as a new **worktree** and work in it without
  disturbing the base branch or other in-flight branches.
- As a developer, I can see all active worktrees/branches in a workspace and pick which one
  the terminal / editor / preview / agent targets.
- As a developer, I can commit with **my** identity (name/email), not a hardcoded author.
- As a developer, I can open a GitHub Pull Request from a worktree's branch, with title/body.
- As a developer, I can see PR status (open/merged/checks) inside the workspace.
- As a reviewer, the SDD `implement` phase runs in its own worktree and produces a branch +
  PR I can review on GitHub; the base tree is never touched until merge.

## Current State

- `services/repositories.py` supports `clone/status/diff/pull/commit/push` — **GitHub only**
  (`sanitize_repository_url` rejects non-`github.com`) — all against the single
  `workspace.workspace_path`.
- Commit author is **hardcoded** `"Execute Dashboard" <execute@localhost>`
  (`repositories.py:241-243`).
- There is **no** branch create/switch, **no** worktree support, and **no** PR endpoint.
- `runtime.exec(workspace, cmd, cwd=...)` (`daytona_runtime.py:178`) already runs arbitrary
  commands with a chosen `cwd` — the mechanism for `git worktree` — but **terminal, editor,
  preview, and agent tools all hardcode `workspace.workspace_path`** as the working dir, so
  they currently cannot target anything but the base tree.
- Repo credentials are GitHub PATs, encrypted with `WorkspaceSecretCipher` (Fernet).

## Branching Model: Layout & Lifecycle

Base clone stays at `workspace_path` (relative to the sandbox home `/home/daytona`, default
`workspace`) tracking the default branch. Feature worktrees live in a sibling managed dir so
they are never nested inside the base tree:

```
/home/daytona/
├── workspace/                 # base worktree (default branch), holds the shared .git
└── worktrees/
    ├── 001-add-auth/          # worktree on branch feat/001-add-auth
    └── fix-login-bug/         # worktree on branch fix/login-bug
```

Lifecycle (all via `runtime.exec` with raw `git worktree`, since the Daytona git SDK wraps
only high-level ops):

| Action | Command (cwd = base `workspace_path`) |
|---|---|
| create | `git worktree add ../worktrees/<slug> -b <branch> <base-ref>` |
| list | `git worktree list --porcelain` |
| remove | `git worktree remove ../worktrees/<slug>` (guard `--force`) |
| prune | `git worktree prune` |

Constraints to enforce: the same branch cannot be checked out in two worktrees; the **base**
worktree cannot be removed; removal of a worktree with uncommitted changes requires explicit
confirmation.

Flow (feature → PR):

```
create worktree (branch off base)
   → agent run / SDD implement targets the worktree dir
   → commit (user identity) in the worktree
   → push branch
   → open PR (head = worktree branch, base = default)
   → on merge: remove worktree + prune
```

## Scope

- Git-worktree management (create / list / select-active / remove / prune) via sandbox exec.
- A per-context **working directory** so terminal/editor/preview/agent/files can target a
  chosen worktree (default = base), instead of the hardcoded `workspace_path`.
- Per-user commit identity (from the authenticated user).
- GitHub PR creation + status via the GitHub REST API using the stored PAT.
- (Defer GitLab/Bitbucket to Epic 09.)

## Backend Tasks

- Add `WorktreeService` (or extend `RepositoryService`) using `runtime.exec`:
  - `create_worktree(branch, base_ref)`, `list_worktrees`, `remove_worktree(branch, force)`,
    `prune_worktrees`, `current_branch(path)`.
  - Validate/sanitize branch names and slugs (reject shell/path metacharacters; confine the
    worktree path under the managed `worktrees/` dir).
- Introduce a **resolved working directory** abstraction:
  - A helper that maps a `worktree_id`/branch → its absolute path, defaulting to the base
    `workspace_path`.
  - Thread an optional `worktree` selector through `exec`, the agent tools
    (`agent_tools.resolve_path` must confine to the *selected* worktree root, preserving
    traversal protection), terminal PTY (`create_or_connect_pty`), `dev_server`, `editor`,
    and `files` so each can operate on a worktree.
- Extend git ops to accept a target worktree path:
  - `status/diff/add/commit/push/pull` parameterized by worktree.
  - `commit(...)` takes `author_name`/`author_email` from the request user instead of the
    hardcoded identity.
- Add `GitHubClient` service (REST API, token from `RepositoryCredential`):
  - `create_pull_request(repo, head, base, title, body, draft)`.
  - `get_pull_request` / `list_pull_requests` (status, mergeable, checks summary).
  - Parse `owner/repo` from the sanitized `repo_url`.
- Endpoints under `/workspaces/{id}/repository`:
  - `GET /worktrees`, `POST /worktrees` (`{branch, base?}`), `DELETE /worktrees/{id}`.
  - `POST /worktrees/{id}/commit`, `POST /worktrees/{id}/push`.
  - `POST /pull-requests` (`{worktree_id|head, base, title, body, draft}`),
    `GET /pull-requests`, `GET /pull-requests/{number}`.
- Redact tokens from all git/API output (extend `sanitize_git_output`).
- Audit + notify on worktree creation/removal, push, and PR creation.

## Frontend Tasks

- Repository view: a **worktrees/branches panel** listing each worktree (branch, path,
  dirty/clean, ahead/behind), with "New worktree" (branch + base) and "Remove".
- An **active worktree selector** that sets which worktree the terminal / editor / preview /
  agent panels operate on; show the active branch prominently.
- "Open Pull Request" panel: choose source worktree, prefilled title/body (from last commit
  or SDD spec summary), base selector, draft toggle, link to the created PR.
- PR status card (open/merged/closed, checks summary) with a link to GitHub.
- Commit dialog uses the signed-in user's identity and the selected worktree.

## Database Tasks

- `workspace_worktrees` table: `id`, `workspace_id`, `branch`, `path`, `base_ref`,
  `is_base` (bool), `status` (`active`/`removing`), timestamps. Unique `(workspace_id, branch)`
  and `(workspace_id, path)`. Index on `workspace_id`.
- Link execution context to a worktree (nullable FK, default = base): `agent_runs.worktree_id`
  (and `agent_sessions.worktree_id` from Epic 05, `spec_workflows.worktree_id` from Epic 01),
  plus terminal sessions.
- Optional `pull_requests` cache: `workspace_id`, `worktree_id`, `number`, `url`, `state`,
  `head`, `base`, `title`, timestamps; index on `workspace_id`.

## Security & Operational Requirements

- Confine every worktree path under the managed `worktrees/` directory; reuse and extend the
  existing path-traversal protection so agent tools cannot escape the selected worktree root.
- Validate `branch`/`base` names; reject refs with shell/path metacharacters.
- PRs are created with the workspace's stored PAT only; never expose the token client-side;
  continue redacting credentials from surfaced git output.
- **Disk**: each worktree consumes working-tree disk (the snapshot is provisioned with
  `--disk 10`, see `daytona/build-snapshot.sh`). Cap worktrees per workspace; auto-prune
  removed/merged worktrees; surface disk pressure.
- On workspace deletion, remove all worktrees (sandbox delete already covers this, but prune
  DB rows).

## Acceptance Criteria

- A user can create a feature worktree, and the base branch's files/preview are unaffected.
- Two worktrees on different branches can have independent agent runs at the same time.
- The terminal / editor / preview / agent can be pointed at a chosen worktree.
- A user can commit with their own identity in a worktree, push, and open a PR whose head is
  that worktree's branch and base is the default branch.
- PR status is visible in the workspace and refreshes.
- The SDD `implement` phase (Epic 01) runs in a dedicated worktree and opens a PR on
  completion; removing the worktree afterward prunes cleanly.

## Dependencies

- Pairs with **Epic 01** (SDD `implement` runs in a worktree and ends in a PR). The
  worktree selector benefits **Epic 05** (per-session worktree) and **Epic 08** (parallel
  collaborative branches). The working-directory refactor touches the same services as
  **Epic 03**.

## Future Enhancements

- A live preview per worktree (multiple branches previewed simultaneously).
- GitLab/Bitbucket MR/PR support (Epic 09).
- Inline PR review comments surfaced in Enigma.
- Auto-create a worktree from a PR branch for review/checkout.
- Branch protection / required-approval awareness, and auto-link PR ↔ `spec_workflow`.
