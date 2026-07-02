import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Copy,
  Monitor,
  Play,
  RefreshCw,
  Search,
  Smartphone,
  Square,
  Tablet,
} from "lucide-react";

import {
  ApiError,
  detectPreviewSession,
  ensureDevServer,
  listPreviewSessions,
  refreshPreviewSession,
  runDevServer,
  stopPreviewSession,
  type DevServerStatusResponse,
  type PreviewSessionResponse,
  type WorkspaceResponse,
} from "../lib/api";
import { useActiveWorktree } from "../lib/activeWorktree";

const DEV_STATUS_POLL_MS = 4000;

function devStatusLabel(status: DevServerStatusResponse): string {
  switch (status.status) {
    case "installing":
      return status.message ?? "Setting up environment from mise.toml…";
    case "ready":
      return "Environment ready — click Run App to start.";
    case "starting":
      return status.message ?? "Starting dev server…";
    case "no-config":
      return "No mise.toml found — start your dev server manually, then click Detect/Open.";
    case "no-dev-task":
      return "mise.toml has no 'dev' task — start your dev server manually, then click Detect/Open.";
    case "error":
      return status.message ?? "Environment setup failed.";
    default:
      return "";
  }
}

type PreviewPanelState =
  | { status: "loading" }
  | { status: "inactive"; session: null }
  | { status: "ready"; session: PreviewSessionResponse }
  | { status: "stopped"; session: PreviewSessionResponse | null }
  | { status: "error"; message: string; session: PreviewSessionResponse | null };

type DeviceMode = "desktop" | "tablet" | "mobile";

type PreviewStatusSnapshot = {
  status: "inactive" | "active" | "stopped" | "error";
  port: number | null;
};

type PreviewPanelProps = {
  workspace: WorkspaceResponse;
  onStatusChange: (snapshot: PreviewStatusSnapshot) => void;
};

const deviceOptions: { id: DeviceMode; label: string; icon: typeof Monitor }[] = [
  { id: "desktop", label: "Desktop", icon: Monitor },
  { id: "tablet", label: "Tablet", icon: Tablet },
  { id: "mobile", label: "Mobile", icon: Smartphone },
];

const deviceWidthClass: Record<DeviceMode, string> = {
  desktop: "max-w-full",
  tablet: "max-w-[820px]",
  mobile: "max-w-[390px]",
};

export function PreviewPanel({ workspace, onStatusChange }: PreviewPanelProps) {
  // Preview follows the shared active-worktree selection (as Terminal/Agents do);
  // null = the base clone / default branch.
  const [activeWorktree] = useActiveWorktree(workspace.id);
  const worktreeId = activeWorktree?.id ?? null;
  const activeBranch = activeWorktree?.branch ?? "base";

  const [panelState, setPanelState] = useState<PreviewPanelState>({ status: "loading" });
  const [deviceMode, setDeviceMode] = useState<DeviceMode>("desktop");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [frameVersion, setFrameVersion] = useState(0);
  const [copyLabel, setCopyLabel] = useState("Copy URL");
  const [devStatus, setDevStatus] = useState<DevServerStatusResponse | null>(null);
  // Bumped by Run App to re-trigger the dev-status poll loop below.
  const [pollNonce, setPollNonce] = useState(0);

  const applySessionState = useCallback(
    (nextSession: PreviewSessionResponse | null) => {
      if (!nextSession) {
        setPanelState({ status: "inactive", session: null });
        onStatusChange({ status: "inactive", port: null });
        return;
      }
      onStatusChange({ status: nextSession.status, port: nextSession.port });
      if (nextSession.status === "active") {
        setPanelState({ status: "ready", session: nextSession });
        return;
      }
      if (nextSession.status === "stopped") {
        setPanelState({ status: "stopped", session: nextSession });
        return;
      }
      setPanelState({
        status: "error",
        message: "Preview session is unavailable.",
        session: nextSession,
      });
    },
    [onStatusChange],
  );

  useEffect(() => {
    const controller = new AbortController();
    setPanelState({ status: "loading" });

    listPreviewSessions(workspace.id, controller.signal)
      .then((sessions) => {
        const session = sessions.find((candidate) => candidate.status === "active") ?? sessions[0];
        if (!session) {
          setPanelState({ status: "inactive", session: null });
          onStatusChange({ status: "inactive", port: null });
          return;
        }
        applySessionState(session);
      })
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setPanelState({
          status: "error",
          message:
            caughtError instanceof ApiError ? caughtError.message : "Unable to load preview.",
          session: null,
        });
        onStatusChange({ status: "error", port: null });
      });

    return () => controller.abort();
  }, [applySessionState, onStatusChange, workspace.id]);

  // Surface dev-server progress. After clone this reflects the environment-setup
  // phase (ending at "ready" — the app is NOT auto-run). It also auto-opens the
  // preview when the app is actually "running", which covers an app that was run
  // earlier and auto-resumed after a sandbox sleep/wake. Run App bumps pollNonce to
  // re-enter this loop and watch the freshly started app come up.
  useEffect(() => {
    let cancelled = false;
    let autoDetected = false;
    let timer: number | undefined;
    const controller = new AbortController();

    async function poll() {
      let next: DevServerStatusResponse;
      try {
        // ensureDevServer also auto-restarts the dev server if it was running
        // before the sandbox slept (waking the sandbox). Same response shape.
        next = await ensureDevServer(workspace.id, controller.signal);
      } catch (caughtError) {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        return;
      }
      if (cancelled) {
        return;
      }
      setDevStatus(next);

      // Auto-open the preview only when the app is genuinely running — never for the
      // "ready" (set-up-but-not-running) state, so cloning no longer auto-launches.
      if (next.status === "running" && !autoDetected) {
        try {
          const response = await detectPreviewSession(workspace.id);
          if (!cancelled && response.session?.status === "active") {
            autoDetected = true;
            applySessionState(response.session);
            setFrameVersion((current) => current + 1);
          }
        } catch {
          // Manual Detect/Open remains available if auto-detect races the server boot.
        }
      }

      const keepPolling =
        next.status === "installing" ||
        next.status === "starting" ||
        (next.status === "running" && !autoDetected);
      if (!cancelled && keepPolling) {
        timer = window.setTimeout(() => void poll(), DEV_STATUS_POLL_MS);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) {
        window.clearTimeout(timer);
      }
    };
    // `worktreeId` re-enters the loop when the active worktree changes, so the panel
    // re-reads which branch the dev server is actually serving.
  }, [applySessionState, workspace.id, pollNonce, worktreeId]);

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
    return session.public_url;
  }, [session]);

  async function runApp() {
    setBusyAction("run");
    try {
      const response = await runDevServer(workspace.id, worktreeId);
      setDevStatus({
        status: response.status,
        port: null,
        message: response.message,
        worktree_id: worktreeId,
        branch: activeBranch,
      });
      // Re-enter the poll loop to surface startup progress and auto-open the preview
      // once the dev server binds its port.
      setPollNonce((current) => current + 1);
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message:
          caughtError instanceof ApiError ? caughtError.message : "Unable to run the app.",
        session,
      });
      onStatusChange({ status: "error", port: session?.port ?? null });
    } finally {
      setBusyAction(null);
    }
  }

  async function detectPreview() {
    setBusyAction("detect");
    try {
      const response = await detectPreviewSession(workspace.id);
      applySessionState(response.session);
      if (response.session?.status === "active") {
        setFrameVersion((current) => current + 1);
      }
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message:
          caughtError instanceof ApiError ? caughtError.message : "Unable to detect preview.",
        session,
      });
      onStatusChange({ status: "error", port: session?.port ?? null });
    } finally {
      setBusyAction(null);
    }
  }

  async function refreshPreview() {
    if (!session) {
      return;
    }
    setBusyAction("refresh");
    try {
      const refreshed = await refreshPreviewSession(workspace.id, session.id);
      applySessionState(refreshed);
      setFrameVersion((current) => current + 1);
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message:
          caughtError instanceof ApiError ? caughtError.message : "Unable to refresh preview.",
        session,
      });
      onStatusChange({ status: "error", port: session.port });
    } finally {
      setBusyAction(null);
    }
  }

  async function stopPreview() {
    if (!session) {
      return;
    }
    setBusyAction("stop");
    try {
      const stopped = await stopPreviewSession(workspace.id, session.id);
      applySessionState(stopped);
    } catch (caughtError) {
      setPanelState({
        status: "error",
        message: caughtError instanceof ApiError ? caughtError.message : "Unable to stop preview.",
        session,
      });
      onStatusChange({ status: "error", port: session.port });
    } finally {
      setBusyAction(null);
    }
  }

  async function copyPreviewUrl() {
    if (!iframeUrl) {
      return;
    }
    await navigator.clipboard?.writeText(iframeUrl);
    setCopyLabel("Copied");
    window.setTimeout(() => setCopyLabel("Copy URL"), 1200);
  }

  const statusLabel = {
    loading: "Loading",
    inactive: "Not detected",
    ready: "Active",
    stopped: "Stopped",
    error: "Error",
  }[panelState.status];

  // Compare by worktree id (null = base clone), not branch name — the base clone's
  // branch (e.g. "main") differs from the "base" label we show for a null selection.
  const runningWorktreeId = devStatus?.worktree_id ?? null;
  const runningBranchLabel = runningWorktreeId ? devStatus?.branch ?? "" : "base";
  const isServing =
    devStatus?.status === "running" ||
    devStatus?.status === "starting" ||
    panelState.status === "ready";
  // The dev server is serving a different worktree than the one now selected.
  const branchMismatch = isServing && runningWorktreeId !== worktreeId;

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
            Preview
          </p>
          <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
          <p className="mt-0.5 text-xs text-ink/55">
            Branch: <span className="font-medium text-ink/75">{activeBranch}</span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-sm font-semibold text-ink/65">
            {statusLabel}
            {session ? ` :${session.port}` : ""}
          </span>
          <button
            className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="button"
            disabled={
              busyAction !== null ||
              devStatus?.status === "installing" ||
              devStatus?.status === "no-config" ||
              // Allow Run App while a preview is active only to switch to another branch.
              (panelState.status === "ready" && !branchMismatch)
            }
            onClick={() => void runApp()}
          >
            <Play className="h-4 w-4" aria-hidden="true" />
            {busyAction === "run"
              ? "Starting..."
              : branchMismatch
                ? `Run ${activeBranch}`
                : "Run App"}
          </button>
          <button
            className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null}
            onClick={() => void detectPreview()}
          >
            <Search className="h-4 w-4" aria-hidden="true" />
            {busyAction === "detect" ? "Detecting..." : "Detect/Open"}
          </button>
          <button
            className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null || !session}
            onClick={() => void refreshPreview()}
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            {busyAction === "refresh" ? "Refreshing..." : "Refresh"}
          </button>
          <button
            className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!iframeUrl}
            onClick={() => void copyPreviewUrl()}
          >
            <Copy className="h-4 w-4" aria-hidden="true" />
            {copyLabel}
          </button>
          <button
            aria-label="Stop preview"
            className="inline-flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busyAction !== null || session?.status !== "active"}
            onClick={() => void stopPreview()}
          >
            <Square className="h-4 w-4" aria-hidden="true" />
            {busyAction === "stop" ? "Stopping..." : "Stop"}
          </button>
        </div>
      </div>

      <div className="mb-4 inline-flex rounded-md border border-line bg-white p-1">
        {deviceOptions.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            aria-pressed={deviceMode === id}
            className={`flex h-9 min-w-10 items-center justify-center rounded-md px-3 text-sm font-semibold transition ${
              deviceMode === id
                ? "bg-primary text-white"
                : "text-ink/65 hover:bg-panel hover:text-ink"
            }`}
            title={label}
            type="button"
            onClick={() => setDeviceMode(id)}
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
            <span className="sr-only">{label}</span>
          </button>
        ))}
      </div>

      {branchMismatch && (
        <div className="mb-4 rounded-md border border-line bg-panel p-3 text-sm text-ink/72">
          Previewing <span className="font-medium text-ink/85">{runningBranchLabel}</span>. Click{" "}
          <span className="font-medium text-ink/85">Run {activeBranch}</span> to preview the active
          branch.
        </div>
      )}

      {devStatus && devStatusLabel(devStatus) && (
        <div
          className={`mb-4 rounded-md border p-3 text-sm ${
            devStatus.status === "error"
              ? "border-danger/30 bg-[#fff5f2] text-danger"
              : "border-line bg-panel text-ink/72"
          }`}
        >
          {devStatusLabel(devStatus)}
        </div>
      )}

      {panelState.status === "error" && (
        <div className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {panelState.message}
        </div>
      )}

      {panelState.status === "loading" && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Loading preview...
        </div>
      )}

      {(panelState.status === "inactive" || panelState.status === "stopped") && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Preview is not running.
        </div>
      )}

      {iframeUrl && (
        <div className="flex justify-center overflow-auto rounded-lg border border-line bg-panel p-2">
          <iframe
            key={frameVersion}
            className={`h-[680px] min-h-[420px] w-full ${deviceWidthClass[deviceMode]} rounded-sm border-0 bg-white`}
            src={iframeUrl}
            title={`${workspace.name} preview`}
          />
        </div>
      )}
    </section>
  );
}
