import { HeartCrack, HeartPulse, Loader2 } from "lucide-react";

import type { WorkspaceServiceHealth } from "../../lib/api";

/**
 * A smaller, outline-style chip *next to* `StatusPill`, never replacing it -- `status` answers
 * "is the port bound", this answers "does it actually respond correctly", and a service can be
 * `running` + `unhealthy` at the same time (a 500 on every request). Renders nothing for
 * "unknown" (no `health_path` configured), so the badge's mere presence already tells you this
 * service is health-checked at all.
 */
export function HealthBadge({ health }: { health: WorkspaceServiceHealth }) {
  if (health === "unknown") {
    return null;
  }

  if (health === "starting") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium text-warn ring-1 ring-warn/30"
        title="Health check hasn't reported healthy yet"
      >
        <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
        Checking…
      </span>
    );
  }

  if (health === "healthy") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium text-signal ring-1 ring-signal/30"
        title="Health check is passing"
      >
        <HeartPulse className="h-3 w-3" aria-hidden="true" />
        Healthy
      </span>
    );
  }

  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium text-danger ring-1 ring-danger/30"
      title="The service is listening but its health check is failing"
    >
      <HeartCrack className="h-3 w-3" aria-hidden="true" />
      Unhealthy
    </span>
  );
}
