# Epic 03 - Agent Editing Tools (patch / search / safe git)

## Goal

Give the agent cheaper, safer, more precise tools. Today edits are full-file overwrites, there
is no code search, and `git` is denied outright — which makes large edits token-expensive and
risky, and forces the agent to re-read whole files to change a line.

## User Value

Faster, lower-cost agent runs with smaller, reviewable diffs and the ability to find code
before editing it.

## Current State

`services/agent_tools.py` exposes exactly four tools:

- `list_files`, `read_file`, `write_file` (**full-file overwrite**), `run_command`.
- `run_command` blocks shell operators (`&&`, `|`, `;`, …) and a `DANGEROUS_COMMANDS`
  denylist that includes `git`, `rm`, `npm`, `uv`, `curl`, etc.
- There is no patch/range edit and no search/grep tool.

The denylist approach is inherently bypassable and also blocks legitimate, safe operations.

## Scope

- A diff/patch-based edit tool (apply a unified diff or a search/replace block).
- A code search tool (ripgrep-style, scoped to the workspace).
- A constrained, structured git tool (status/diff/add/commit on safe paths) so the agent
  doesn't need raw shell `git`.
- Move toward an **allowlist** policy for `run_command` (the original plan calls for a
  "command denylist and allowlist policy").

## Backend Tasks

- Add tools to `WorkspaceTools`:
  - `apply_patch(path, patch)` — apply a unified diff / search-replace block; reject if
    context doesn't match (fail loudly, don't silently overwrite).
  - `search_code(query, path_glob?, max_results)` — ripgrep within the sandbox, excluding
    `EXCLUDED_NAMES`, with capped output.
  - `git_status` / `git_diff` — read-only structured git, reusing `RepositoryService`.
- Register the new tools in both providers (`agent_providers.py`, `openhands_tools.py`) so
  tool schemas stay in sync.
- Introduce an explicit **allowlist** mode for `run_command` (config-driven), keeping the
  denylist as a fallback. Log/audit blocked commands.
- Preserve and extend path-traversal protection (`resolve_path`) for the new tools.

## Frontend Tasks

- Render `apply_patch` tool calls as diffs in the agent timeline (reuse `ChangedFiles`/
  `ToolCard` components).
- Show `search_code` results as a collapsible list with file/line links.
- Surface blocked-command messages clearly in the run output.

## Database Tasks

- None required (tool calls already persist as `agent_events` / `agent_file_changes`).
  Optionally tag `agent_file_changes` rows with the tool that produced them.

## Security Requirements

- `apply_patch` must respect the same path restrictions as `write_file`.
- The git tool must never accept a raw token or write outside the workspace path.
- Allowlist decisions and rejections must be auditable.

## Acceptance Criteria

- The agent can modify a single function via `apply_patch` without rewriting the file.
- The agent can locate symbols via `search_code` before editing.
- `apply_patch` fails cleanly when context doesn't match (no destructive overwrite).
- Tool diffs render in the timeline.

## Dependencies

- Improves the **Epic 01** `implement` phase and benefits from **Epic 04** (models that use
  tool calls well).

## Future Enhancements

- Confirmation mode before destructive tool calls (ties into Epic 01's HITL gates).
- Multi-file atomic patches.
- Language-server-assisted edits (rename/refactor).
