import { useState } from "react";
import { RotateCw, X } from "lucide-react";

import { ApiError, restartWorkspaceService, type AffectedService } from "../../lib/api";

/**
 * Inline banner (never a toast — this codebase has none) prompting a restart for every
 * service whose running environment no longer matches the env vars it was started with.
 */
export function AffectedServicesBanner({
  workspaceId,
  services,
  onDismiss,
}: {
  workspaceId: string;
  services: AffectedService[];
  onDismiss: (serviceId: string) => void;
}) {
  const [restartingId, setRestartingId] = useState<string | null>(null);
  const [restartedId, setRestartedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (services.length === 0) {
    return null;
  }

  async function handleRestart(service: AffectedService) {
    setRestartingId(service.service_id);
    setError(null);
    try {
      await restartWorkspaceService(workspaceId, service.service_id);
      setRestartedId(service.service_id);
      window.setTimeout(() => setRestartedId(null), 1200);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to restart service.",
      );
    } finally {
      setRestartingId(null);
    }
  }

  return (
    <div className="mt-4 space-y-2">
      {services.map((service) => (
        <div
          key={service.service_id}
          className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-warn/30 bg-[#fffbf0] px-3 py-2 text-sm text-warn"
        >
          <span className="min-w-0 break-words">
            <span className="font-mono">{service.keys.join(", ")}</span> changed — restart{" "}
            <span className="font-semibold">{service.name}</span>?
          </span>
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              disabled={restartingId === service.service_id}
              aria-busy={restartingId === service.service_id}
              onClick={() => void handleRestart(service)}
              className="inline-flex items-center gap-1.5 rounded-md border border-warn/40 bg-white px-2.5 py-1 text-xs font-semibold text-warn transition hover:bg-warn/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RotateCw className="h-3.5 w-3.5" aria-hidden="true" />
              {restartingId === service.service_id
                ? "Restarting…"
                : restartedId === service.service_id
                  ? "Restarted"
                  : "Restart"}
            </button>
            <button
              type="button"
              aria-label={`Dismiss ${service.name} restart prompt`}
              onClick={() => onDismiss(service.service_id)}
              className="rounded-md p-1 text-warn/70 transition hover:text-warn"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </div>
        </div>
      ))}
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}
