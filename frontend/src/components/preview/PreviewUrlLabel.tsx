import type { PreviewUrlKind } from "../../lib/api";

/**
 * Labels a preview URL by what kind it is, so nobody pastes a rotating capability URL into a
 * config file. A capability URL rotates roughly hourly (`PreviewService._proxy_url`'s renewal
 * window); an alias URL is stable by design.
 */
export function PreviewUrlLabel({ kind }: { kind: PreviewUrlKind }) {
  if (kind === "capability") {
    return (
      <span className="inline-flex items-center rounded-full border border-warn/30 bg-[#fffbf0] px-2 py-0.5 text-xs font-medium text-warn">
        Temporary link — rotates hourly
      </span>
    );
  }

  return (
    <span className="inline-flex items-center rounded-full border border-line bg-panel px-2 py-0.5 text-xs font-medium text-ink/72">
      Stable link — safe to paste into a config
    </span>
  );
}
