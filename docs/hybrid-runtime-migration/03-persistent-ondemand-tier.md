# Epic 03 — Tier 2: Persistent On-Demand Human Sandbox

## Goal

Keep the human-facing features — code-server IDE, interactive PTY terminal, dev-server
preview, file browsing — on the **existing persistent Daytona sandbox**, but change its
lifecycle to **on-demand**: it starts only when a human actually opens the IDE/terminal/
preview and auto-stops aggressively when idle. Full feature parity for occasional human use,
without an always-on sandbox for a mostly-idle, mostly-AI workspace.

## Why

These features genuinely need persistence (a human interacts over minutes/hours across
reloads), but they do **not** need to run 24/7. Daytona already auto-starts a stopped sandbox
on access (`_ensure_started`) and auto-stops on idle (`auto_stop_interval`). So this tier is
largely **configuration + making sure agent work no longer keeps it warm** — the code that
drives editor/terminal/preview is reused unchanged.

## Current State

- `editor.py` launches code-server in the sandbox and exposes it via a Daytona preview URL.
- `terminal.py` bridges xterm.js ↔ Daytona PTY (`create_or_connect_pty`, resize, kill).
- `dev_server.py` runs two-phase `setup` (mise install + deps → `ready`) then `run`
  (`mise run dev`), tracks `/tmp/enigma-dev.status`, detects the port, and sets
  `dev_autostart` to relaunch after sandbox sleep.
- `files.py` does `fs.*`; `preview_proxy.py` proxies authenticated preview traffic.
- `_ensure_started` (`daytona_runtime.py:314`) already starts a stopped sandbox on access and
  reconciles egress policy.
- Today `auto_stop_interval = daytona_auto_stop_minutes` (**30 min**) — and the same sandbox
  is kept warm by agent runs, so it rarely idles.

## Scope

- The persistent (session-role) sandbox serves **only** human intents (editor/terminal/
  preview/dev_server/files) via the router (Epic 01).
- **On-demand start**: the sandbox is provisioned/started when a human opens a human feature,
  not on every agent run.
- **Aggressive auto-stop**: lower `auto_stop_interval` for the session tier (e.g. 10 min),
  independent of the execution tier.
- Reuse editor/terminal/dev_server/preview/files code as-is (they already target the session
  sandbox through the router).

## Backend Tasks

- Ensure agent runs (Epic 02) **do not** resolve or start the session sandbox — only human
  intents do. This is what lets it idle and auto-stop.
- Session-tier provisioning uses `daytona_session_auto_stop_minutes` (new, e.g. 10) instead
  of the shared 30; keep `ephemeral=False`, `auto_delete_interval=-1` (persistent).
- On-demand start hooks: opening the editor, connecting a terminal, or requesting a preview
  triggers `get_sandbox(..., start=True)` for the session sandbox (already the behavior; just
  make sure it is the *only* thing that starts it).
- `dev_autostart` preserved: on session-sandbox resume, relaunch `mise run dev` per the
  existing two-phase logic.
- Optional: a workspace may have **no session sandbox provisioned** until first human use
  (lazy create), further cutting idle cost.

## Frontend Tasks

- IDE / terminal / preview panels show a **"starting environment…"** state while the session
  sandbox starts from stopped (Daytona start latency), then connect.
- A visible workspace state indicator: "environment stopped / starting / ready", ideally with
  the idle-stop countdown.
- Terminal/editor WebSocket reconnect after an idle-stop → next-open resume.

## Database Tasks

- Workspace: keep `daytona_sandbox_id` as the **session** sandbox id; add `last_active_at`
  (drives idle expectations + observability). `dev_autostart` reused.
- Config: `daytona_session_auto_stop_minutes` (session tier) separate from any execution-tier
  knob (Epic 01/05).

## Security & Operational Requirements

- Egress policy reconciled on every session-sandbox start (`_apply_network_settings`) —
  unchanged.
- Preview/editor remain behind the authenticated `preview_proxy.py` — never public.
- Idle-stop is safe: Daytona persists sandbox state across stop/start, so no data loss.

## Acceptance Criteria

- Opening the IDE (hybrid on) starts the session sandbox on demand, serves code-server via
  the existing preview path, and shows workspace files.
- Terminal gives an interactive PTY (resize works) identical to today.
- Dev server runs, preview serves it, and after idle-stop → reopen, the sandbox resumes with
  state intact and (if `dev_autostart`) relaunches the dev server.
- After the session idle threshold with no human activity **and no agent run**, the sandbox
  auto-stops; agent runs never keep it warm.
- With hybrid off, the persistent sandbox behaves exactly as today.

## Dependencies

- Depends on **01** (router). Independent of **02** but only saves cost once **02** stops
  routing agent work here. Uses **05** for auto-stop tuning.

## Future Enhancements

- Lazy-provision the session sandbox on first human use (no sandbox at all for
  never-opened workspaces).
- Per-worktree preview (multiple branch previews at once).
- A "warm on schedule" option for teams that open workspaces at predictable times.
