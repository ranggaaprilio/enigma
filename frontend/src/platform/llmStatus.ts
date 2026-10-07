import { createContext, useContext, useEffect, useState } from "react";

import type { LlmSettingsSource } from "../lib/api";

/**
 * Whether the platform has a usable LLM endpoint.
 *
 * `"unknown"` is load-bearing: it covers "still loading" *and* "the status call failed". The
 * first-run gate only redirects on an affirmative `"unconfigured"`, so an unreachable status
 * endpoint can never lock anyone out of the product. The backend is the authoritative gate for
 * agent runs, so failing open here costs nothing -- same reasoning as `auth/permissions.ts`.
 */
export type LlmConfigState = "unknown" | "configured" | "unconfigured";

export type LlmStatusValue = {
  state: LlmConfigState;
  source: LlmSettingsSource | null;
  /** Reported by the status endpoint; neither is a secret. Null when unknown/unconfigured. */
  baseUrl: string | null;
  model: string | null;
  /** Re-read the status from the server. Awaited by the save handlers before they navigate. */
  refresh: () => Promise<void>;
  /** Optimistic flip after a successful save, so the gate stops redirecting immediately. */
  markConfigured: (source: LlmSettingsSource) => void;
};

const FALLBACK: LlmStatusValue = {
  state: "unknown",
  source: null,
  baseUrl: null,
  model: null,
  refresh: async () => undefined,
  markConfigured: () => undefined,
};

export const LlmStatusContext = createContext<LlmStatusValue | null>(null);

/**
 * Unlike {@link useAuth}, this does *not* throw without a provider -- it degrades to
 * `"unknown"`. `AppSidebar` reads it and is rendered standalone in component tests; a throwing
 * hook would force a new wrapper into unrelated test files for a purely advisory signal.
 */
export function useLlmStatus(): LlmStatusValue {
  return useContext(LlmStatusContext) ?? FALLBACK;
}

// --- "Skip for now" ------------------------------------------------------------------------
// Persisted per browser, mirroring lib/activeWorktree.ts: same try/catch-guarded accessors and
// the same CustomEvent broadcast, so the gate re-renders the moment Skip is pressed.

const SKIP_KEY = "enigma.llmSetupSkipped";
const SKIP_EVENT = "enigma:llm-setup-skipped";

export function isLlmSetupSkipped(): boolean {
  try {
    return window.localStorage.getItem(SKIP_KEY) === "1";
  } catch {
    return false;
  }
}

export function markLlmSetupSkipped(): void {
  try {
    window.localStorage.setItem(SKIP_KEY, "1");
  } catch {
    // Private mode / blocked storage: the event below still unblocks the current tab.
  }
  window.dispatchEvent(new CustomEvent(SKIP_EVENT));
}

export function clearLlmSetupSkipped(): void {
  try {
    window.localStorage.removeItem(SKIP_KEY);
  } catch {
    // Nothing to do -- the flag was never readable in the first place.
  }
}

/** Live view of the skip flag so the gate reacts without a reload. */
export function useLlmSetupSkipped(): boolean {
  const [skipped, setSkipped] = useState(isLlmSetupSkipped);
  useEffect(() => {
    function handler() {
      setSkipped(isLlmSetupSkipped());
    }
    window.addEventListener(SKIP_EVENT, handler);
    return () => window.removeEventListener(SKIP_EVENT, handler);
  }, []);
  return skipped;
}
