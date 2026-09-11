import { ExternalLink } from "lucide-react";

import type { ComposedServiceResponse } from "../../lib/api";
import { StatusPill } from "../ui/StatusPill";
import { HealthBadge } from "./HealthBadge";
import { PreviewUrlLabel } from "./PreviewUrlLabel";
import { CopyButton } from "./CopyButton";

/**
 * The primary "Open app" block for the service marked (explicitly or inferred) as the
 * workspace's entry point. Sits above the per-service list, which stays the place for
 * secondary services and their own URLs.
 */
export function EntryPointCard({
  service,
  onOpen,
  isSelected,
}: {
  service: ComposedServiceResponse;
  onOpen: () => void;
  isSelected: boolean;
}) {
  const url = service.alias_url ?? service.capability_url;
  const canOpen = service.status === "running" && Boolean(url);
  const urlKind = service.alias_url ? "alias" : "capability";

  return (
    <div className="mb-4 rounded-md border border-primary/30 bg-[#fff8f4] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-[0.16em] text-primary/70">
            Entry point
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="truncate text-base font-semibold text-ink">{service.name}</span>
            <StatusPill status={service.status} />
            <HealthBadge health={service.health_status} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={!canOpen}
            onClick={onOpen}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
          >
            <ExternalLink className="h-4 w-4" aria-hidden="true" />
            {isSelected ? "Showing" : "Open app"}
          </button>
          {url && <CopyButton text={url} label="Copy app URL" />}
        </div>
      </div>
      {url && (
        <div className="mt-2">
          <PreviewUrlLabel kind={urlKind} />
        </div>
      )}
    </div>
  );
}
