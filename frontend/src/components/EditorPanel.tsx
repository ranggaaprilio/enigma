import { useEffect, useState } from "react";

import {
  ApiError,
  getEditorStatus,
  startEditorSession,
  stopEditorSession,
  type EditorSessionResponse,
  type WorkspaceResponse,
} from "../lib/api";

type EditorPanelState =
  | { status: "loading" }
  | { status: "inactive"; session: null }
  | { status: "ready"; session: EditorSessionResponse }
  | { status: "stopped"; session: EditorSessionResponse | null }
  | { status: "error"; message: string; session: EditorSessionResponse | null };

export function EditorPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const [panelState, setPanelState] = useState<EditorPanelState>({ status: "loading" });
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [blockedEditorUrl, setBlockedEditorUrl] = useState<string | null>(null);

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

    return () => controller.abort();
  }, [workspace.id]);

  const session =
    panelState.status === "ready" ||
    panelState.status === "stopped" ||
    panelState.status === "error"
      ? panelState.session
      : null;

  const activeEditorUrl = session?.status === "active" ? session.url : null;

  function openEditorUrl(url: string) {
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    setBlockedEditorUrl(opened ? null : url);
  }

  async function startEditor() {
    setBusyAction("start");
    try {
      const nextSession = await startEditorSession(workspace.id);
      setPanelState({ status: "ready", session: nextSession });
      openEditorUrl(nextSession.url);
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
      const nextSession = await startEditorSession(workspace.id);
      setPanelState({ status: "ready", session: nextSession });
      openEditorUrl(nextSession.url);
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
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null}
            onClick={() => void startEditor()}
          >
            {busyAction === "start" ? "Opening..." : "Open new tab"}
          </button>
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

      {panelState.status === "loading" && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Loading editor...
        </div>
      )}

      {(panelState.status === "inactive" || panelState.status === "stopped") && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Editor is not running.
        </div>
      )}

      {activeEditorUrl && (
        <div className="rounded-md border border-line bg-white p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-ink">code-server is ready</p>
              <p className="mt-1 break-all font-mono text-xs text-ink/60">{activeEditorUrl}</p>
            </div>
            <button
              className="rounded-md border border-line bg-[#26251e] px-3 py-2 text-sm font-medium text-white transition hover:bg-[#3a382f] disabled:cursor-not-allowed disabled:opacity-50"
              type="button"
              disabled={busyAction !== null}
              onClick={() => openEditorUrl(activeEditorUrl)}
            >
              Open new tab
            </button>
          </div>
          {blockedEditorUrl && (
            <a
              className="mt-4 inline-flex rounded-md border border-line bg-white px-3 py-2 text-sm font-medium text-ink underline-offset-4 hover:underline"
              href={blockedEditorUrl}
              rel="noreferrer"
              target="_blank"
            >
              Continue to code-server
            </a>
          )}
        </div>
      )}
    </section>
  );
}
