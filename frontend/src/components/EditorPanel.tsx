import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  getApiBaseUrl,
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

  const iframeUrl = useMemo(() => {
    if (!session || session.status !== "active") {
      return null;
    }
    return new URL(session.url, getApiBaseUrl()).toString();
  }, [session]);

  async function startEditor() {
    setBusyAction("start");
    try {
      const nextSession = await startEditorSession(workspace.id);
      setPanelState({ status: "ready", session: nextSession });
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
    <section className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
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
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-[#f5f1e8] disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null}
            onClick={() => void startEditor()}
          >
            {busyAction === "start" ? "Starting..." : "Open"}
          </button>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-[#f5f1e8] disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null}
            onClick={() => void restartEditor()}
          >
            {busyAction === "restart" ? "Restarting..." : "Restart"}
          </button>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-[#f5f1e8] disabled:cursor-not-allowed disabled:opacity-50"
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

      {iframeUrl && (
        <iframe
          className="h-[680px] min-h-[420px] w-full rounded-md border border-[#0b1118] bg-[#111820]"
          src={iframeUrl}
          title={`${workspace.name} editor`}
        />
      )}
    </section>
  );
}
