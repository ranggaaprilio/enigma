import { useCallback, useEffect, useMemo, useState } from "react";
import { Monitor, Smartphone, Tablet } from "lucide-react";

import {
  ApiError,
  ensureWorkspaceServices,
  restartWorkspaceService,
  startWorkspaceService,
  stopWorkspaceService,
  type WorkspaceEnvironmentState,
  type WorkspaceServiceResponse,
  type WorkspaceServicesResponse,
  type WorkspaceResponse,
} from "../lib/api";
import { useActiveWorktree } from "../lib/activeWorktree";
import { ServiceRow } from "./preview/ServiceRow";

// Fast cadence while something is starting up or has no confirmed preview yet; slow
// cadence once everything has settled, so the panel still notices a crash while mounted.
const FAST_POLL_MS = 4000;
const SLOW_POLL_MS = 15000;
const POLL_BACKOFF_MS = [4000, 8000, 16000, 30000];

type DeviceMode = "desktop" | "tablet" | "mobile";

type PreviewStatusSnapshot = {
  running: number;
  total: number;
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

function environmentMessage(environment: WorkspaceEnvironmentState | null): string | null {
  if (!environment) return null;
  switch (environment.status) {
    case "installing":
      return environment.message ?? "Setting up environment from mise.toml…";
    case "no-config":
      return "No mise.toml found — add a service in Commands, or start one manually.";
    case "error":
      return environment.message ?? "Environment setup failed.";
    default:
      return null;
  }
}

export function PreviewPanel({ workspace, onStatusChange }: PreviewPanelProps) {
  // Preview follows the shared active-worktree selection (as Terminal/Agents do);
  // null = the base clone / default branch.
  const [activeWorktree] = useActiveWorktree(workspace.id);
  const worktreeId = activeWorktree?.id ?? null;
  const activeBranch = activeWorktree?.branch ?? "base";

  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [services, setServices] = useState<WorkspaceServiceResponse[]>([]);
  const [environment, setEnvironment] = useState<WorkspaceEnvironmentState | null>(null);
  const [limit, setLimit] = useState<number | null>(null);
  const [busy, setBusy] = useState<{ serviceId: string; action: "start" | "stop" | "restart" } | null>(
    null,
  );
  const [selectedServiceId, setSelectedServiceId] = useState<string | null>(null);
  const [logOpenServiceId, setLogOpenServiceId] = useState<string | null>(null);
  const [logResetTokens, setLogResetTokens] = useState<Record<string, number>>({});
  const [copyFeedback, setCopyFeedback] = useState<{ serviceId: string; label: string } | null>(
    null,
  );
  const [deviceMode, setDeviceMode] = useState<DeviceMode>("desktop");
  const [frameVersion, setFrameVersion] = useState(0);
  // Bumped after a manual action to re-enter the poll loop immediately.
  const [pollNonce, setPollNonce] = useState(0);

  const mergeService = useCallback((updated: WorkspaceServiceResponse) => {
    setServices((current) => current.map((row) => (row.id === updated.id ? updated : row)));
  }, []);

  const applyResponse = useCallback((response: WorkspaceServicesResponse) => {
    setServices(response.services);
    setEnvironment(response.environment);
    setLimit(response.limit);
    setLoadState("ready");
  }, []);

  // Reports the aggregate to the dashboard shell's footer whenever the service list
  // changes, in its own effect so it never fires from inside another component's render.
  useEffect(() => {
    const running = services.filter((row) => row.status === "running").length;
    onStatusChange({ running, total: services.length });
  }, [services, onStatusChange]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let backoffIndex = 0;
    let autoSelected = false;
    const controller = new AbortController();

    async function poll() {
      let response: WorkspaceServicesResponse;
      try {
        response = await ensureWorkspaceServices(workspace.id, controller.signal);
      } catch (caughtError) {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        if (!cancelled) {
          setLoadState((current) => (current === "loading" ? "error" : current));
          setErrorMessage(
            caughtError instanceof ApiError ? caughtError.message : "Unable to reach services.",
          );
          const delay = POLL_BACKOFF_MS[Math.min(backoffIndex, POLL_BACKOFF_MS.length - 1)];
          backoffIndex += 1;
          timer = window.setTimeout(() => void poll(), delay);
        }
        return;
      }
      if (cancelled) return;
      backoffIndex = 0;
      setErrorMessage(null);
      applyResponse(response);

      if (!autoSelected) {
        const openable = response.services.find(
          (row) => row.status === "running" && row.public_url,
        );
        if (openable) {
          autoSelected = true;
          setSelectedServiceId(openable.id);
          setFrameVersion((current) => current + 1);
        }
      }

      const stillSettling = response.services.some(
        (row) =>
          row.status === "installing" ||
          row.status === "starting" ||
          (row.status === "running" && !row.public_url),
      );
      if (!cancelled) {
        timer = window.setTimeout(() => void poll(), stillSettling ? FAST_POLL_MS : SLOW_POLL_MS);
      }
    }

    setLoadState((current) => (current === "ready" ? current : "loading"));
    void poll();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer) {
        window.clearTimeout(timer);
      }
    };
    // `worktreeId` re-enters the loop so the panel re-reads which branch each service is
    // actually serving; `pollNonce` re-enters after a manual action.
  }, [applyResponse, workspace.id, pollNonce, worktreeId]);

  const selectedService = useMemo(
    () => services.find((row) => row.id === selectedServiceId) ?? null,
    [services, selectedServiceId],
  );
  const iframeUrl =
    selectedService?.status === "running" ? selectedService.public_url : null;

  async function runService(service: WorkspaceServiceResponse) {
    setBusy({ serviceId: service.id, action: "start" });
    setErrorMessage(null);
    try {
      const result = await startWorkspaceService(workspace.id, service.id, worktreeId);
      mergeService(result.service);
      if (result.warning) {
        setErrorMessage(result.warning);
      }
      setPollNonce((current) => current + 1);
    } catch (caughtError) {
      setErrorMessage(
        caughtError instanceof ApiError ? caughtError.message : "Unable to start service.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function stopService(service: WorkspaceServiceResponse) {
    setBusy({ serviceId: service.id, action: "stop" });
    setErrorMessage(null);
    try {
      const result = await stopWorkspaceService(workspace.id, service.id);
      mergeService(result.service);
      if (selectedServiceId === service.id) {
        setSelectedServiceId(null);
      }
    } catch (caughtError) {
      setErrorMessage(
        caughtError instanceof ApiError ? caughtError.message : "Unable to stop service.",
      );
    } finally {
      setBusy(null);
    }
  }

  async function restartService(service: WorkspaceServiceResponse) {
    setBusy({ serviceId: service.id, action: "restart" });
    setErrorMessage(null);
    try {
      const result = await restartWorkspaceService(workspace.id, service.id, worktreeId);
      mergeService(result.service);
      setLogResetTokens((current) => ({
        ...current,
        [service.id]: (current[service.id] ?? 0) + 1,
      }));
      setPollNonce((current) => current + 1);
    } catch (caughtError) {
      setErrorMessage(
        caughtError instanceof ApiError ? caughtError.message : "Unable to restart service.",
      );
    } finally {
      setBusy(null);
    }
  }

  function openService(service: WorkspaceServiceResponse) {
    setSelectedServiceId(service.id);
    setFrameVersion((current) => current + 1);
  }

  async function copyServiceUrl(service: WorkspaceServiceResponse) {
    if (!service.public_url) return;
    await navigator.clipboard?.writeText(service.public_url);
    setCopyFeedback({ serviceId: service.id, label: "Copied" });
    window.setTimeout(() => setCopyFeedback(null), 1200);
  }

  function toggleLogs(service: WorkspaceServiceResponse) {
    setLogOpenServiceId((current) => (current === service.id ? null : service.id));
  }

  const environmentText = environmentMessage(environment);
  const runningCount = services.filter((row) => row.status === "running").length;

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">Preview</p>
          <h3 className="mt-1 text-base font-semibold">{workspace.name}</h3>
          <p className="mt-0.5 text-xs text-ink/55">
            Branch: <span className="font-medium text-ink/75">{activeBranch}</span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-full bg-white px-3 py-1 text-sm font-semibold text-ink/65">
            {services.length === 0 ? "No services" : `${runningCount}/${services.length} running`}
            {limit !== null && ` · limit ${limit}`}
          </span>
          <div className="inline-flex rounded-md border border-line bg-white p-1">
            {deviceOptions.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                aria-pressed={deviceMode === id}
                className={`flex h-8 min-w-9 items-center justify-center rounded-md px-2.5 text-sm font-semibold transition ${
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
        </div>
      </div>

      {environmentText && (
        <div
          className={`mb-4 rounded-md border p-3 text-sm ${
            environment?.status === "error"
              ? "border-danger/30 bg-[#fff5f2] text-danger"
              : "border-line bg-panel text-ink/72"
          }`}
        >
          {environmentText}
        </div>
      )}

      {errorMessage && (
        <div className="mb-4 rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {errorMessage}
        </div>
      )}

      {loadState === "loading" && services.length === 0 && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          Loading services...
        </div>
      )}

      {loadState !== "loading" && services.length === 0 && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          No services configured yet. Add a long-running service in the Commands tab to preview
          it here.
        </div>
      )}

      {services.length > 0 && (
        <div className="space-y-2">
          {services.map((service) => (
            <ServiceRow
              key={service.id}
              workspaceId={workspace.id}
              service={service}
              activeWorktreeId={worktreeId}
              activeBranch={activeBranch}
              busy={busy?.serviceId === service.id ? busy.action : null}
              isSelected={selectedServiceId === service.id}
              logsOpen={logOpenServiceId === service.id}
              logResetToken={logResetTokens[service.id] ?? 0}
              copyLabel={copyFeedback?.serviceId === service.id ? copyFeedback.label : "Copy URL"}
              onRun={() => void runService(service)}
              onStop={() => void stopService(service)}
              onRestart={() => void restartService(service)}
              onOpen={() => openService(service)}
              onCopy={() => void copyServiceUrl(service)}
              onToggleLogs={() => toggleLogs(service)}
            />
          ))}
        </div>
      )}

      {iframeUrl && (
        <div className="mt-4 flex justify-center overflow-auto rounded-lg border border-line bg-panel p-2">
          <iframe
            key={frameVersion}
            className={`h-[680px] min-h-[420px] w-full ${deviceWidthClass[deviceMode]} rounded-sm border-0 bg-white`}
            src={iframeUrl}
            title={`${selectedService?.name ?? workspace.name} preview`}
          />
        </div>
      )}
    </section>
  );
}
