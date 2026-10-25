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
  `upload_file`, `create_folder`, `move_files`, `delete_file(recursive)`, `find_files`,
  `set_file_permissions`) and `PtyHandle` (`send_input`, `resize`, `disconnect`) with
  the Daytona implementation as a passthrough; the Docker implementation raises
  `NotImplementedError`.
- Consumers of `fs`: `services/files.py` (Files panel: list/read/write/mkdir/move/
  delete/search), `services/agent_tools.py` (list_files/read_file/write_file/
  apply_patch/search_code), `services/repositories.py` (workspace dir scaffolding
  326–332 and 890–892, SSH deploy-key materialisation 824–826 with `chmod 600`,
  `.git` probes 874), `services/workspace_init.py:115-147` (template/blank seeding),
  `services/workspace_env.py:181-190` (`.env` materialisation, `chmod 600`),
  `services/dev_server.py:462,470` and `services/workspace_services.py:1120,1129`
  (mise config detection).
- Git is CLI-over-exec already: clone/pull/fetch/push run through `exec` with
  per-command auth — a token (`git_providers.py` basic-auth usernames) **or** a
  server-generated SSH deploy key written into the sandbox (`ssh_keys.py`,
  `repositories.py::_run_ssh_git`, needs `ssh` on PATH) — and `sanitize_git_output`;
  `worktrees.py` was always CLI (`git worktree` via `exec`, from line 171);
  `speckit.py` uses exec only (`specify init` at 237). So git parity is "exec works +
  both credential flows work", not new surface.
- Terminal: `services/terminal.py` calls `create_or_connect_pty(workspace, session_id,
  on_data, env, cols, rows, cwd)` (`:199`) and `kill_pty` (`:128`); session ids are
  `execute-<workspace.id.hex>` (`:75`) and `terminal_sessions.pty_session_id` persists
  them so a browser reconnect reattaches to the same shell. Worktree terminals get a
  per-worktree PTY (`worktree_id` query param). Daytona provides named, reconnectable
  PTY sessions natively — Docker exec does not, which is the hard part of this epic.
- Preview port detection (`preview.py:316`, `sandbox_probe.py`), run detection
  (`run_detection.py`) and dev server / services are exec-based — they come along for
  free but are validated in Epic 04.

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
     `delete_file(recursive)` → `rm` / `rm -rf`; `set_file_permissions(path, mode)` →
     `chmod` — all with argv-array execution. The `chmod 600` on SSH keys and `.env`
     files must be honoured, not best-effort: `ssh` refuses world-readable keys.
   - All paths normalized and confined exactly as today (`agent_tools` path confinement
     untouched; `files.py` keeps its own validation).
3. **Git parity validation** (no new code expected beyond Epic 01's CLI collapse):
   - Verify clone/pull/push of a private repo inside a container with **both**
     credential flows: token (`RepositoryService` per-command auth) and SSH deploy key
     (`POST /repo/ssh-key` → key written via `fs` → `_run_ssh_git`); token absent from
     `ps`, container logs, structlog; the key file lands `0600`.
   - Run the existing worktree suite (`worktrees.py`) and Spec Kit init
     (`speckit.py:237`) against Docker — both are exec-only and should pass as-is; fix
     any assumptions about `$HOME`/`PATH` that differ from the snapshot.
   - Template/blank workspaces (`workspace_init.py`: clone template → `git init` →
     initial commit) seed correctly on a fresh container.
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
     apply_patch → read_file → search_code on a real container; clone a fixture repo
     with a token and with an SSH deploy key; materialise a workspace `.env` and assert
     `0600`; tmux reconnect keeps scrollback (run `echo marker`, disconnect, reconnect,
     assert the marker via `tmux capture-pane`).

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
- Clone → edit → commit → push of a private repo works with the stored token **and**
  with a server-generated SSH deploy key; the token never appears in logs or `ps`
  output.
- Worktrees create/list/remove, per-worktree terminals, workspace `.env`
  materialisation and Spec Kit `specify init` succeed on Docker.
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

## Implementation Notes (shipped)

Built on the Epic 02 branch. Verified on Podman 5.8 with the real image: unit + conformance
suites and `tests/test_docker_integration.py` (13 real-container tests). The code and
security reviews' findings are fixed below; each has a regression test.

- **Files** — `services/sandbox/docker_fs.py`, `DockerRuntime.fs()` returns a
  `DockerFileSystem` over the (auto-started) container. **Every operation is a `docker exec`
  as the sandbox user** — never the daemon's `get_archive`/`put_archive`, which the
  security review showed run as root inside the container: through a committed symlink
  such as `docs -> /etc` they wrote root-owned files (even replaced `/etc/passwd`) and read
  `/etc/shadow`. With an exec the kernel checks every access; the integration suite pins
  that writes and reads through final *and* parent symlinks to `/etc` fail with
  `PermissionError` and leave `/etc` untouched.
  - `list_files` / `get_file_info`: `find [-H] <path> -printf` with NUL-separated records
    (any filename round-trips). Entries carry Daytona's `FileInfo` fields (`name`,
    `is_dir`, `size`, `mod_time` ISO-8601 UTC, `mode`, `permissions` `0644`, `owner`,
    `group`) plus `is_symlink`, which Daytona never sets — so the Files panel refuses to
    open a symlink on Docker (stricter, deliberately).
  - `download_file`: `[ -f "$1" ] && timeout 60 head -c <64 MiB + 1>` — a symlink is
    followed (like Daytona), a directory or device is refused before anything is read
    (the `timeout` covers a FIFO swapped in after the check), and backend memory is bounded
    (`MAX_DOWNLOAD_BYTES`). The output is streamed and joined once: docker-py's
    `exec_run(demux=True)` concatenates frame by frame, which the re-review measured at
    5–30 s of GIL-holding CPU for 64 MiB.
  - `upload_file`: the bytes stream over the exec's stdin into
    `mkdir -p -- <parent> && head -c <n> > <path>`. `head -c n` needs no end-of-file
    (portable across Docker and Podman), and truncating in place keeps an existing file's
    mode and owner, so an agent edit of an executable script keeps it executable. A stream
    cut off mid-way is an error, never a silently short file. Uploads are not atomic (a
    failed one leaves the file partial) — Daytona's are not either, and a temp-file + rename
    would replace symlinks and modes.
  - `create_folder` = `mkdir -p -m` (an existing folder keeps its mode, like Daytona's
    `MkdirAll`), `move_files` = `mv -T` (a rename, never "into"), `delete_file` = `rm -r` /
    `rm -d`, `set_file_permissions` = `chmod` with modes validated as 3–4 octal digits.
  - `find_files` is a **content** search, as Daytona's is (its SDK: "Search for text
    pattern within files"): fixed-string `grep -rnHIF --null`, under `timeout 60`, output
    capped at 8 MiB; a pattern with a newline matches nothing. The conformance test that
    asserted a filename glob encoded the interface fake's behaviour, not Daytona's; it and
    the fake now search contents.
  - Paths are argv elements after `--` (or `$1` of a constant `sh -c` script) and always
    absolute (`find` has no `--`, so `-delete` can never be built). Per-file failures raise
    `OSError` subclasses (`FileNotFoundError`, `PermissionError`, `IsADirectoryError`), not
    `SandboxRuntimeError`: consumers read the latter as "sandbox unavailable", and the
    Daytona SDK's fs errors are not `SandboxRuntimeError` either. Daemon failures still are.
  - File operations run on the provider's exec pool; the control pool stays free for kills.
- **Terminal** — `services/sandbox/docker_pty.py`: each terminal is a tmux session in the
  workspace container, `docker exec -it` of `tmux new-session -A -s <name>` (attach or
  create), `bash -l` (the sandbox user's login shell is `/bin/sh`), `TERM=xterm-256color`,
  cwd and env (`-e`) from the caller. Session ids map to tmux-safe names (`:` and `.` →
  `_`; worktree terminals are `execute-<ws>:<wt>`). Podman does not end an exec when its
  socket closes, so the attach runs under a constant wrapper that records the tmux client's
  pid: `disconnect` SIGHUPs that client (detach) and the session, its shell and scrollback
  live on; reconnecting reattaches and tmux redraws the screen. The wrapper starts the tmux
  server separately (status bar off, no Escape delay, `exit-empty off`), so the long-lived
  server's argv never carries a session's `-e` env. `resize` = exec resize (tmux reflows).
  `kill_pty` = `tmux kill-session -t =<name>`, idempotent, and a stopped container is left
  stopped. **Two tabs on one session mirror each other** — tmux's normal behaviour,
  accepted. Each terminal's reader holds a thread of a dedicated `pty` pool for its whole
  life, so open terminals can starve neither commands/file access nor their own input
  (exec pool); the control pool stays reserved for kills. The hijacked exec socket and its
  HTTP response are closed explicitly, in order (left to GC, docker-py's response finalizer
  raises on the already-closed socket).
- **Git** — no new code was needed: clone/pull/fetch/push, worktrees, blank and template
  init, and `specify init` all pass on a real container. One fix, shared with Daytona and
  approved at review: **token auth moved off git's command line**. The Basic header used to
  be `git -c http.extraHeader=…` — readable from `ps` by any process in the sandbox,
  including agent commands, while git ran. It is now `GIT_CONFIG_COUNT/KEY_0/VALUE_0`
  environment (git ≥ 2.31; the image has 2.53). The integration test samples `ps` and every
  `/proc/*/cmdline` during a running authenticated git and finds neither the token nor its
  base64. The SSH deploy key lands `0600` in a `0700` dir and is removed afterwards; a
  materialised `.env` is `0600`.
- **Tests** — `tests/sandbox/fakes.py` gains `FakeContainerFs` (understands exactly the
  argv shapes above and fails loudly on anything else), `FakeStdinSocket` and
  `FakePtySocket`; the conformance suite's Docker case no longer xfails files or the PTY
  and adds an unusual-filenames round trip for every provider; `tests/test_docker_fs_pty.py`
  covers argv safety with hostile names, permission errors, bounds, pools and the tmux
  lifecycle.

### Known limitations

- **Private-repo credentials were not exercised end to end** (no test repository): token and
  SSH paths are covered up to the remote. Manual check: connect a private GitHub repo with a
  PAT on a Docker workspace, clone, edit, commit, push; repeat with a generated deploy key.
- **Files-panel search** passes a filename glob (`*query*`) to `find_files`, which is a
  content search on both providers, so it finds next to nothing on either — a pre-existing
  consumer bug, left for a follow-up.
- **Capacity**: open terminals are bounded by the `pty` pool (`DOCKER_MAX_CONCURRENT_EXECS`,
  default 64, platform-wide); past that a new terminal stays silent until one closes. There
  is no per-workspace terminal cap yet (Epic 05). Directory listing has no timeout of its
  own.
- **A shell that exits** (`exit`) leaves the browser terminal silent until the tab is closed
  or reconnected (which starts a fresh session); the bridge is not told.
- `files.read` refuses only a final symlink; reads through a parent symlink succeed when the
  sandbox user may read the target, the same as on Daytona.
