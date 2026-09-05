import { Loader2 } from "lucide-react";

/**
 * Inline busy indicator for buttons. Hidden from the accessibility tree — the button keeps
 * its label and carries `aria-busy`, so screen readers already announce the pending state
 * and tests can still match the button by name.
 */
export function Spinner({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return <Loader2 className={`${className} animate-spin`} aria-hidden="true" />;
}
