# Epic 09 - Workspace Templates & Multi-Provider Git

## Goal

Lower the barrier to creating a workspace. Today a workspace requires an existing **GitHub**
repository. Add starter templates / blank workspaces and support for GitLab, Bitbucket, and
generic git over HTTPS/SSH.

## User Value

Users can spin up a workspace from a template (or empty) and connect repos hosted anywhere —
not just GitHub.

## Current State

- `services/repositories.py` `sanitize_repository_url` hard-rejects anything that isn't
  `https://github.com/...`; credentials are GitHub PATs.
- Workspace creation requires a `repo_url` (`models/workspace.py`).
- The original plan explicitly deferred GitLab/Bitbucket to post-MVP.

## Scope

- Workspace templates / scaffolds (and a blank workspace option).
- A provider abstraction for git hosts (GitHub, GitLab, Bitbucket, generic).
- SSH key auth in addition to tokens.

## Backend Tasks

- Generalize repository handling:
  - A `GitProvider` abstraction; refactor GitHub-specific logic out of
    `sanitize_repository_url` and the clone/auth path.
  - Support GitLab and Bitbucket URL validation + token auth; add a "generic git" mode.
  - Add SSH-key credential type alongside PAT (encrypted via `WorkspaceSecretCipher`).
- Templates:
  - A `workspace_templates` registry (name, source repo or scaffold script, default branch,
    optional `mise`/preview config).
  - "Create from template" and "blank workspace" creation paths (blank = `git init` in the
    sandbox, no clone).
- Keep PR/branch features (Epic 02) provider-aware (GitHub first, others as available).

## Frontend Tasks

- Workspace creation wizard: choose template / blank / connect existing repo.
- Provider + auth selector (token vs. SSH key) with provider-specific help text.
- Template gallery.

## Database Tasks

- Migration: `workspace_templates`; extend `repository_credentials` for provider + auth type
  (SSH key material encrypted).
- Make `workspaces.repo_url` nullable for blank workspaces.

## Security Requirements

- Encrypt all credential material (tokens and SSH keys) at rest.
- Validate/normalize URLs per provider; prevent SSRF/host spoofing in validation.
- Redact credentials from all output.

## Acceptance Criteria

- A workspace can be created from a template, blank, or an existing repo.
- A GitLab (or Bitbucket) repo can be cloned and pushed.
- SSH-key auth works for a generic git host.

## Dependencies

- Builds on **Epic 02** (PR/branch) for provider-aware PR/MR creation.

## Future Enhancements

- Org-level template catalogs.
- Provider-native MR/PR creation for GitLab/Bitbucket.
- Import from a zip/local upload.

## Implementation Notes (shipped)

### Design decisions

- **Provider abstraction is pure.** `backend/app/services/git_providers.py` owns URL parsing,
  canonicalisation, per-provider HTTP basic-auth usernames (`x-access-token` / `oauth2` /
  `x-token-auth` / `git`), PR capability, and host-agnostic redaction. `repositories.py` keeps
  `sanitize_repository_url` / `sanitize_git_output` as thin wrappers.
- **Strict URL/auth pairing.** `ssh_key` requires an SSH URL (`git@host:owner/repo.git` or
  `ssh://git@host/...`); `personal_access_token` requires HTTPS. No silent rewriting.
- **SSRF guards are literal-only.** IP literals, `localhost`, `*.local/.internal/.lan/...`,
  dotless hosts, ports on the SaaS providers, `http://`/`git://`/`file://` are rejected. Egress
  from the sandbox is governed separately by `DAYTONA_NETWORK_ALLOW_LIST`.
- **All clones run through the git CLI** (`git clone --branch <b> -- <url> .`) so token and SSH
  auth share `_run_authenticated_git`; the Daytona SDK's go-git clone is no longer used.
- **SSH keys are generated server-side** (ed25519, `services/ssh_keys.py`), stored encrypted with
  `WorkspaceSecretCipher`, and only materialised in `/tmp/enigma-ssh/<uuid>/id_ed25519` (0600)
  for the duration of one git command, with `GIT_SSH_COMMAND` pointing at it
  (`IdentitiesOnly`, `BatchMode`, `StrictHostKeyChecking=accept-new`).
- **One credential per workspace** (UNIQUE on `repository_credentials.workspace_id`), with
  `auth_type` selecting `encrypted_token`(+`username`) or `encrypted_private_key`+`public_key`.
- **`origin` is reconciled lazily** in push/pull/fetch (`_ensure_origin`), so `connect` stays
  DB-only and template/blank workspaces gain a remote the first time they push.
- **Templates and blank workspaces are initialised inside `SqlAlchemyWorkspaceService.create`**
  (`services/workspace_init.py`): shallow clone → drop `.git` → write `mise.toml` if missing →
  `git init -b <default_branch>` → initial commit. Blank workspaces get a `README.md` commit so
  worktrees/discard/commit-detail work before a remote exists.
- **Provider-native pull requests stay GitHub + token only.** Other providers (or SSH-key
  credentials) get `409` with a clear message; the UI hides the PR form and shows a
  "push and open it on <host>" note. GitLab MR / Bitbucket PR creation remains future work.
- **Seed templates** are GitHub's public Codespaces starters (`react-vite`, `nextjs`,
  `node-express`, `python-flask`, `python-django`), each with a `mise_config` providing `install`
  and `dev` tasks. Swap the URLs in migration `0022` to point at organisation-owned templates.

### API surface

| Method / path | Purpose |
|---|---|
| `GET /workspace-templates` | Active template catalog (sorted by `sort_order`). |
| `POST /workspaces`, `POST /projects/{p}/teams/{t}/workspaces` | Body gains `source` (`repository` \| `template` \| `blank`) and `template_id`; `repo_url` is required only for `repository`. |
| `POST /workspaces/{id}/repo/connect` | Body gains `provider` (`auto` default), `auth_type`, optional `username`; `token` only for token auth. |
| `GET /workspaces/{id}/repo/credential` | Provider, auth type, public key, `supports_pull_requests`. Never secrets. |
| `POST /workspaces/{id}/repo/ssh-key` | Ensure/rotate the deploy key pair; returns the public key. |

### Operational notes

- Migration `0022_templates_and_git_providers` (reversible): `workspace_templates`,
  `workspaces.repo_url` nullable + `source` + `template_id`, credential columns + CHECKs.
- When `DAYTONA_NETWORK_BLOCK_ALL=true`, allow the git hosts (443/22) and github.com (template
  clones) in `DAYTONA_NETWORK_ALLOW_LIST`.
- The sandbox image must ship an OpenSSH client for deploy-key auth; the backend surfaces
  "SSH client is not available in this sandbox image" otherwise (see `daytona/Dockerfile`).
- GitHub Enterprise Server and self-hosted Bitbucket are not supported (the REST base URL and
  `/scm/` path layout differ); self-hosted GitLab works by choosing GitLab explicitly.

