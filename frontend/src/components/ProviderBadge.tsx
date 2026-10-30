import { Cloud, Container } from "lucide-react";

import { runtimeLabel } from "../lib/runtime";

/** Which sandbox provider a workspace runs on (local-docker-runtime Epic 06). */
export function ProviderBadge({ runtime }: { runtime: string | null | undefined }) {
  if (!runtime) return null;
  const Icon = runtime === "docker" ? Container : Cloud;
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-white px-2 py-0.5 text-xs font-medium text-body ring-1 ring-line">
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {runtimeLabel(runtime)}
    </span>
  );
}
