import { useState } from "react";
import { RotateCw } from "lucide-react";

import type { PreviewAdvisoryResponse } from "../../lib/api";
import { CopyButton } from "./CopyButton";

/**
 * Shown when a web + API pair has no dev-server proxy configured -- names the framework, the
 * exact snippet to paste, and why `localhost:8000` cannot work from the browser. Modelled on
 * `EnvVarEditor/AffectedServicesBanner.tsx`'s inline-banner shape (this codebase has no toasts).
 */
export function WiringAdvisor({
  advisory,
  onRecheck,
}: {
  advisory: PreviewAdvisoryResponse;
  onRecheck: () => Promise<void> | void;
}) {
  const [rechecking, setRechecking] = useState(false);

  async function handleRecheck() {
    setRechecking(true);
    try {
      await onRecheck();
    } finally {
      setRechecking(false);
    }
  }

  return (
    <div className="mb-4 rounded-md border border-warn/30 bg-[#fffbf0] p-4 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-semibold text-warn">{advisory.title}</p>
          <p className="mt-1 text-ink/72">{advisory.detail}</p>
          <p className="mt-1 text-xs text-ink/55">
            Your browser can only reach the signed preview origin, not `localhost` inside the
            sandbox — a same-origin dev proxy is what makes `/api/...` calls work without CORS.
          </p>
        </div>
        <button
          type="button"
          disabled={rechecking}
          onClick={() => void handleRecheck()}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-warn/40 bg-white px-2.5 py-1 text-xs font-semibold text-warn transition hover:bg-warn/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <RotateCw className={`h-3.5 w-3.5 ${rechecking ? "animate-spin" : ""}`} aria-hidden="true" />
          {rechecking ? "Checking…" : "Re-check"}
        </button>
      </div>
      {advisory.snippet && (
        <div className="mt-3">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-medium text-ink/55">
              {advisory.framework ? `${advisory.framework} config` : "Suggested config"}
              {advisory.target_file ? ` — ${advisory.target_file}` : ""}
            </p>
            <CopyButton text={advisory.snippet} />
          </div>
          <pre className="mt-1 overflow-x-auto rounded-md border border-line bg-white p-3 text-xs text-ink/85">
            <code>{advisory.snippet}</code>
          </pre>
        </div>
      )}
    </div>
  );
}
