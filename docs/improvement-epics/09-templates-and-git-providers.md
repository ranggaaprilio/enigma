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
