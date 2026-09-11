import { useEffect, useState } from "react";

import {
  ApiError,
  getEditorStatus,
  listWorktrees,
  startEditorSession,
  stopEditorSession,
  type EditorSessionResponse,
  type WorkspaceResponse,
  type WorktreeResponse,
} from "../lib/api";

type EditorPanelState =
  | { status: "loading" }
  | { status: "inactive"; session: null }
  | { status: "ready"; session: EditorSessionResponse }
  | { status: "stopped"; session: EditorSessionResponse | null }
  | { status: "error"; message: string; session: EditorSessionResponse | null };

const BASE_WORKTREE_VALUE = "__base__";

export function EditorPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const [panelState, setPanelState] = useState<EditorPanelState>({ status: "loading" });
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [blockedEditorUrl, setBlockedEditorUrl] = useState<string | null>(null);
  const [worktrees, setWorktrees] = useState<WorktreeResponse[]>([]);
  // Used as the iframe's React `key`: bumping it remounts the frame, which reloads code-server
  // without touching the process in the sandbox. Same idiom as PreviewPanel.
  const [frameVersion, setFrameVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setPanelState({ status: "loading" });

    getEditorStatus(workspace.id, controller.signal)
      .then((response) => {
        if (response.status === "active" && response.session) {
          setPanelState({ status: "ready", session: response.session });
          return;
        }
        if (response.status === "stopped") {
          setPanelState({ status: "stopped", session: response.session });
          return;
        }
        if (response.status === "error") {
          setPanelState({
            status: "error",
            message: "Editor session is unavailable.",
            session: response.session,
          });
          return;
        }
        setPanelState({ status: "inactive", session: null });
      })
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setPanelState({
          status: "error",
          message: caughtError instanceof ApiError ? caughtError.message : "Unable to load editor.",
          session: null,
        });
      });

    // The folder picker is a convenience, so a failure here must never block the editor itself.
    listWorktrees(workspace.id, controller.signal)
      .then(setWorktrees)
      .catch(() => setWorktrees([]));

    return () => controller.abort();
  }, [workspace.id]);

  const session =
    panelState.status === "ready" ||
    panelState.status === "stopped" ||
    panelState.status === "error"
      ? panelState.session
      : null;

  const activeSession = session?.status === "active" ? session : null;

  function openEditorUrl(url: string) {
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    setBlockedEditorUrl(opened ? null : url);
  }

  async function launch(action: string, worktreeId: string | null) {
    setBusyAction(action);
    try {
      const nextSession = await startEditorSession(workspace.id, worktreeId);
      setPanelState({ status: "ready", session: nextSession });
      setFrameVersion((current) => current + 1);
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message: caughtError instanceof ApiError ? caughtError.message : "Unable to start editor.",
        session,
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function restartEditor() {
    setBusyAction("restart");
    try {
      await stopEditorSession(workspace.id).catch(() => null);
      const nextSession = await startEditorSession(workspace.id, session?.worktree_id ?? null);
      setPanelState({ status: "ready", session: nextSession });
      setFrameVersion((current) => current + 1);
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message: caughtError instanceof ApiError ? caughtError.message : "Unable to restart editor.",
        session,
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function stopEditor() {
    setBusyAction("stop");
    try {
      const stopped = await stopEditorSession(workspace.id);
      setPanelState({ status: "stopped", session: stopped });
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message: caughtError instanceof ApiError ? caughtError.message : "Unable to stop editor.",
        session,
      });
    } finally {
      setBusyAction(null);
    }
  }

  const statusLabel = {
    loading: "Loading",
    inactive: "Not started",
    ready: "Active",
    stopped: "Stopped",
    error: "Error",
  }[panelState.status];

  const selectedWorktreeValue = activeSession?.worktree_id ?? BASE_WORKTREE_VALUE;
  const featureWorktrees = worktrees.filter((worktree) => !worktree.is_base);

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
            Editor
          </p>
          <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-sm font-semibold text-ink/65">
            {statusLabel}
          </span>
          {featureWorktrees.length > 0 && (
            <label className="flex items-center gap-2 text-sm text-ink/65">
              <span className="sr-only">Folder</span>
              <select
                aria-label="Folder"
                className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
                disabled={busyAction !== null}
                value={selectedWorktreeValue}
                onChange={(event) => {
                  const value = event.target.value;
                  void launch("folder", value === BASE_WORKTREE_VALUE ? null : value);
                }}
              >
                <option value={BASE_WORKTREE_VALUE}>Base clone</option>
                {featureWorktrees.map((worktree) => (
                  <option key={worktree.id} value={worktree.id}>
                    {worktree.branch}
                  </option>
                ))}
              </select>
            </label>
          )}
          {activeSession ? (
            <>
              <button
                className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
                type="button"
                disabled={busyAction !== null}
                onClick={() => setFrameVersion((current) => current + 1)}
              >
                Reload frame
              </button>
              <button
                className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
                type="button"
                disabled={busyAction !== null}
                onClick={() => openEditorUrl(activeSession.prime_url)}
              >
                Open new tab
              </button>
            </>
          ) : (
            <button
              className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
              disabled={busyAction !== null}
              onClick={() => void launch("start", session?.worktree_id ?? null)}
            >
              {busyAction === "start" ? "Starting..." : "Start editor"}
            </button>
          )}
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null}
            onClick={() => void restartEditor()}
          >
            {busyAction === "restart" ? "Restarting..." : "Restart"}
          </button>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null || session?.status !== "active"}
            onClick={() => void stopEditor()}
          >
            {busyAction === "stop" ? "Stopping..." : "Stop"}
          </button>
        </div>
      </div>

      {panelState.status === "error" && (
        <div className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {panelState.message}
        </div>
      )}

      {blockedEditorUrl && (
        <div className="mb-4 rounded-md border border-line bg-panel p-3 text-sm text-ink/72">
          Your browser blocked the new tab.{" "}
          <a
            className="font-medium text-ink underline underline-offset-4"
            href={blockedEditorUrl}
            rel="noreferrer"
            target="_blank"
          >
            Continue to code-server
          </a>
        </div>
      )}

      {panelState.status === "loading" && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Loading editor...
        </div>
      )}

      {(panelState.status === "inactive" || panelState.status === "stopped") && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Editor is not running. Start it to open code-server here.
        </div>
      )}

      {activeSession && (
        <div className="overflow-hidden rounded-lg border border-line bg-panel p-2">
          <iframe
            key={frameVersion}
            className="h-[calc(100vh-16rem)] min-h-[520px] w-full rounded-sm border-0 bg-white"
            src={activeSession.prime_url}
            title={`${workspace.name} editor`}
            allow="clipboard-read; clipboard-write"
          />
        </div>
      )}
    </section>
  );
}
