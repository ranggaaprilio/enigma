import { ChevronDown, ChevronUp, Copy, ExternalLink, Play, RotateCw, Square } from "lucide-react";

import type { WorkspaceServiceResponse } from "../../lib/api";
import { StatusPill } from "../ui/StatusPill";
import { ServiceLogPane } from "./ServiceLogPane";

const ROLE_LABEL: Record<string, string> = {
  install: "Install",
  migrate: "Migrate",
  dev: "Dev",
  test: "Test",
  lint: "Lint",
  seed: "Seed",
  custom: "Custom",
};

function statusMessage(service: WorkspaceServiceResponse): string | null {
  if (service.status === "error") {
    return service.last_error ?? "Environment setup failed.";
  }
  if (service.status === "exited") {
    return service.exit_code === null
      ? "Process exited."
      : `Process exited (code ${service.exit_code}).`;
  }
  return null;
}

export function ServiceRow({
  workspaceId,
  service,
  activeWorktreeId,
  activeBranch,
  busy,
  isSelected,
  logsOpen,
  logResetToken,
  copyLabel,
  onRun,
  onStop,
  onRestart,
  onOpen,
  onCopy,
  onToggleLogs,
}: {
  workspaceId: string;
  service: WorkspaceServiceResponse;
  activeWorktreeId: string | null;
  activeBranch: string;
  busy: "start" | "stop" | "restart" | null;
  isSelected: boolean;
  logsOpen: boolean;
  /** Bumped on restart so the log pane remounts and drops stale pre-restart output. */
  logResetToken: number;
  copyLabel: string;
  onRun: () => void;
  onStop: () => void;
  onRestart: () => void;
  onOpen: () => void;
  onCopy: () => void;
  onToggleLogs: () => void;
}) {
  const isActive = service.status === "installing" || service.status === "starting" || service.status === "running";
  const canOpen = service.status === "running" && Boolean(service.public_url);
  // The service is serving a different worktree than the one now selected in the UI.
  const branchMismatch = isActive && service.worktree_id !== activeWorktreeId;
  const message = statusMessage(service);

  return (
    <div
      className={`rounded-md border p-3 ${isSelected ? "border-primary/50 bg-panel" : "border-line bg-white"}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-semibold text-ink">{service.name}</span>
            <span className="shrink-0 rounded-full bg-line px-2 py-0.5 text-xs font-semibold text-ink/60">
              {ROLE_LABEL[service.role] ?? service.role}
            </span>
            {service.port !== null && (
              <span className="shrink-0 text-xs text-ink/55">:{service.port}</span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-ink/55">
            Branch: <span className="font-medium text-ink/75">{service.branch ?? "base"}</span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill status={service.status} />
          <button
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2.5 py-1 text-xs font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="button"
            disabled={busy !== null || (isActive && !branchMismatch)}
            onClick={onRun}
          >
            <Play className="h-3.5 w-3.5" aria-hidden="true" />
            {busy === "start" ? "Starting…" : branchMismatch ? `Run ${activeBranch}` : "Run"}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busy !== null || !isActive}
            onClick={onRestart}
          >
            <RotateCw className="h-3.5 w-3.5" aria-hidden="true" />
            {busy === "restart" ? "Restarting…" : "Restart"}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={busy !== null || !isActive}
            onClick={onStop}
          >
            <Square className="h-3.5 w-3.5" aria-hidden="true" />
            {busy === "stop" ? "Stopping…" : "Stop"}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!canOpen}
            onClick={onOpen}
          >
            <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
            {isSelected ? "Showing" : "Open"}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!service.public_url}
            onClick={onCopy}
          >
            <Copy className="h-3.5 w-3.5" aria-hidden="true" />
            {copyLabel}
          </button>
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel"
            type="button"
            aria-expanded={logsOpen}
            onClick={onToggleLogs}
          >
            {logsOpen ? (
              <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            Logs
          </button>
        </div>
      </div>

      {branchMismatch && (
        <p className="mt-2 rounded-md border border-line bg-panel p-2 text-xs text-ink/72">
          Serving <span className="font-medium text-ink/85">{service.branch}</span>. Click{" "}
          <span className="font-medium text-ink/85">Run {activeBranch}</span> to serve the active
          branch instead.
        </p>
      )}

      {message && (
        <p
          className={`mt-2 rounded-md border p-2 text-xs ${
            service.status === "error"
              ? "border-danger/30 bg-[#fff5f2] text-danger"
              : "border-line bg-panel text-ink/72"
          }`}
        >
          {message}
        </p>
      )}

      {logsOpen && (
        <ServiceLogPane key={logResetToken} workspaceId={workspaceId} serviceId={service.id} />
      )}
    </div>
  );
}
