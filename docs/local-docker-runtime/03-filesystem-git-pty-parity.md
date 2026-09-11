# Epic 03 - Filesystem, Git & PTY Parity

## Goal

Bring the Docker provider to full feature parity for the three surfaces beyond plain
exec: the `RuntimeFileSystem` operations (agent file tools + Files panel), git flows
(clone/status/diff/commit/push, worktrees, Spec Kit), and a reconnectable terminal PTY.
At the end of this epic every agent tool and the terminal work identically on
`runtime="docker"` and `runtime="daytona"`.

## User Value

The AI can actually build software locally: read/write/patch files, clone the repo with
the user's GitHub credentials, run Spec Kit, and the human can open a terminal into the
local sandbox — the full OpenHands-style container experience without a cloud
subscription.

## Current State

- Epic 01 defined `RuntimeFileSystem` (`list_files`, `get_file_info`, `download_file`,
  `upload_file`, `create_folder`, `move_files`, `delete_file(recursive)`, `find_files`)
  and `PtyHandle` (`send_input`, `resize`, `disconnect`) with the Daytona implementation
  as a passthrough; the Docker implementation raises `NotImplementedError`.
- Consumers of `fs`: `services/files.py` (Files panel: list/read/write/mkdir/move/
  delete/search), `services/agent_tools.py` (list_files/read_file/write_file/
  apply_patch/search_code), `services/repositories.py:642-660` (repo scaffolding),
  `services/dev_server.py:549,559` (config detection).
- Git is CLI-over-exec after Epic 01 (askpass/env credentials, `sanitize_git_output`);
  `worktrees.py` was always CLI (`git worktree` via `exec`, lines 170–461);
  `speckit.py` uses exec only (`:213,591,880-938`). So git parity is "exec works +
  credentials work", not new surface.
- Terminal: `services/terminal.py` calls `create_or_connect_pty(workspace, session_id,
  on_data, env, cols, rows, cwd)` (`:193`) and `kill_pty` (`:128`);
  `terminal_sessions.pty_session_id` persists session names so a browser reconnect
  reattaches to the same shell. Daytona provides named, reconnectable PTY sessions
  natively — Docker exec does not, which is the hard part of this epic.
- Preview detection (`preview.py:243`) and dev server are exec-based — they come along
  for free but are validated in Epic 04.

## Scope

**In scope**

- `DockerFileSystem` implementing `RuntimeFileSystem` via `docker exec` + tar archives.
- Git credential plumbing verified inside containers (clone/pull/push private repos).
- tmux-backed PTY: named, reconnectable, resizable terminal sessions.
- Conformance-suite coverage for all three surfaces on the Docker fake + integration
  tests on a real daemon.

**Out of scope**

- Preview/editor/dev-server end-to-end (Epic 04).
- An in-container action-execution server (recorded as a Future Enhancement; exec+tar
  first).
- Any change to the agent tool semantics, denylist, or path-confinement logic.

## Backend Tasks

1. **`DockerFileSystem` — reads**:
   - `download_file` → `container.get_archive(path)` (tar stream), extract the single
     member in memory; enforce the same max-size guard `files.py` applies today.
   - `list_files` / `get_file_info` → one exec of `find <path> -maxdepth 1 -printf` /
     `stat --printf` with a stable record format, parsed into the DTO the Daytona SDK
     returns (`name`, `is_dir`, `size`, `mod_time`, `permissions`) so `files.py`
     serializers need zero changes.
   - `find_files` → `grep -rn --binary-files=without-match` (pattern passed via argv
     array, never shell-interpolated), mapped to the SDK's match structure used by
     `agent_tools.search_code`.
2. **`DockerFileSystem` — writes**:
   - `upload_file` → build a single-member tar in memory, `container.put_archive(parent,
     tar)`; preserve the non-root user's uid/gid in tar headers so files stay editable.
   - `create_folder(path, mode)` → `mkdir -p` + `chmod`; `move_files` → `mv`;
     `delete_file(recursive)` → `rm` / `rm -rf` with argv-array execution.
   - All paths normalized and confined exactly as today (`agent_tools` path confinement
     untouched; `files.py` keeps its own validation).
3. **Git parity validation** (no new code expected beyond Epic 01's CLI collapse):
   - Verify clone/pull/push of a private GitHub repo inside a container using the
     askpass/env credential flow; token absent from `ps`, container logs, structlog.
   - Run the existing worktree suite (`worktrees.py`) and Spec Kit init
     (`speckit.py:213`) against Docker — both are exec-only and should pass as-is; fix
     any assumptions about `$HOME`/`PATH` that differ from the snapshot.
4. **PTY via tmux** (the image ships `tmux` — Epic 02 task 9):
   - `create_or_connect_pty(workspace, session_id, ...)` runs
     `exec_create(["tmux", "new-session", "-A", "-s", <session_id>, "-x", cols, "-y",
     rows], tty=True, stdin=True)` + `exec_start(socket=True)`; a reader task pumps the
     raw socket to `on_data` (bytes, exactly as `terminal.py` expects for xterm).
   - `PtyHandle.send_input` writes to the socket; `resize` calls the exec-resize API
     **and** `tmux resize-window -t <session_id> -x -y` so the inner app reflows;
     `disconnect` detaches (closes the socket/exec) **without** killing the tmux session
     — reconnect with the same `session_id` reattaches (`new-session -A`), matching
     Daytona's named-session reconnect that `terminal_sessions.pty_session_id` relies on.
   - `kill_pty` → `tmux kill-session -t <session_id>` (idempotent; a missing session is
     not an error, matching current behaviour at `terminal.py:128`).
   - Handle daemon/container restart: a dead container yields `SandboxNotFoundError`;
     the terminal service already recreates sessions on that path.
5. **Interactive shell environment**: tmux default-shell = the user's login shell with
   the snapshot `PATH` (`~/.local/bin`), `TERM=xterm-256color`, and the per-session
   `env`/`cwd` arguments applied via `tmux new-session -c <cwd>` + `set-environment`.
6. **Tests**:
   - Conformance suite: fs round-trip (upload → list → info → download → move → delete),
     unicode filenames, large-file guard, recursive delete, find_files match shape.
   - PTY contract test against the Docker fake (send/echo/resize/disconnect/reconnect).
   - Integration (`DOCKER_INTEGRATION_TESTS=1`): agent tool sequence write_file →
     apply_patch → read_file → search_code on a real container; clone a fixture repo;
     tmux reconnect keeps scrollback (run `echo marker`, disconnect, reconnect, assert
     the marker via `tmux capture-pane`).

## Frontend Tasks

None. Terminal (`@xterm/xterm`), Files panel, and agent run UI are runtime-agnostic
already; this epic's acceptance is that they work unchanged against a Docker workspace.

## Database Tasks

None. `terminal_sessions.pty_session_id` stores the tmux session name — same column,
same semantics.

## Security Requirements

- Every fs/git helper executes with **argv arrays** (`["rm", "-rf", path]`), never
  `sh -c` string interpolation of user-controlled paths; add tests with hostile
  filenames (`; rm`, backticks, newlines, leading `-`).
- Path confinement to the workspace/worktree root is enforced before any fs op —
  unchanged code in `agent_tools.py`/`files.py`, but re-run its tests against the Docker
  fs to prove symlink escapes inside the container don't bypass it (resolve with
  `realpath` in-container where the Daytona SDK resolved server-side).
- Git tokens only via askpass/env; `sanitize_git_output` on every surfaced git message.
- tmux sessions are per-workspace-container; `session_id` remains server-generated
  (never client-chosen) as today.

## Acceptance Criteria

- The full agent tool suite (`list_files`, `read_file`, `write_file`, `apply_patch`,
  `search_code`, `run_command`, `git_status`, `git_diff`) passes on a Docker workspace
  with byte-identical tool-result shapes to Daytona (assert via the conformance suite).
- Files panel: browse, open, edit/save, create folder, rename/move, delete, search — all
  functional on Docker.
- Clone → edit → commit → push of a private repo works with the stored GitHub PAT; the
  token never appears in logs or `ps` output.
- Worktrees create/list/remove and Spec Kit `specify init` succeed on Docker.
- Terminal: open, run `top`, resize the pane (reflows), close the tab, reopen — same
  shell, history intact. Two browser tabs on the same session mirror each other (tmux
  gives this for free — accept or explicitly serialize, but document the choice).
- Daytona path untouched: full existing test suite green with no expectation changes.

## Dependencies

- **Epic 02** (Docker core exec/lifecycle) — hard dependency.
- None on other series. (`copilot-workspace-parity` validation steps will inherit this
  parity for free since they only use terminal/exec surfaces.)

## Future Enhancements

- In-container action-execution server (OpenHands-style HTTP agent) if per-op exec
  latency becomes a bottleneck for file-heavy agent loops; the `RuntimeFileSystem`
  interface hides the swap.
- `inotify`-based file-change events for the editor/Files panel (neither provider
  streams changes today).
