import { useState } from "react";
import { Copy } from "lucide-react";

/**
 * The copy-with-"Copied"-feedback pattern, extracted from `PreviewPanel.copyServiceUrl` and
 * `EnvVarEditor.exportTemplate` (both open-coded it identically) so new copy sites (the entry
 * point card, the wiring advisor's snippet) share one implementation.
 */
export function CopyButton({
  text,
  label = "Copy",
  copiedLabel = "Copied",
  className,
}: {
  text: string;
  label?: string;
  copiedLabel?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    await navigator.clipboard?.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }

  return (
    <button
      type="button"
      onClick={() => void handleCopy()}
      className={
        className ??
        "inline-flex items-center gap-1.5 rounded-md border border-line bg-white px-2.5 py-1 text-xs font-medium transition hover:bg-panel"
      }
    >
      <Copy className="h-3.5 w-3.5" aria-hidden="true" />
      {copied ? copiedLabel : label}
    </button>
  );
}
