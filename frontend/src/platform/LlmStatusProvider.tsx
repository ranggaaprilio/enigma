import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "../auth/context";
import { getLlmSettingsStatus, type LlmSettingsSource } from "../lib/api";
import {
  clearLlmSetupSkipped,
  LlmStatusContext,
  type LlmConfigState,
  type LlmStatusValue,
} from "./llmStatus";

export function LlmStatusProvider({ children }: { children: ReactNode }) {
  const { status: authStatus } = useAuth();
  const [state, setState] = useState<LlmConfigState>("unknown");
  const [source, setSource] = useState<LlmSettingsSource | null>(null);
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const status = await getLlmSettingsStatus(signal);
      setState(status.configured ? "configured" : "unconfigured");
      setSource(status.source);
      setBaseUrl(status.base_url);
      setModel(status.model);
      if (status.configured) {
        // Setup is done, so a stale skip flag must not suppress a future prompt.
        clearLlmSetupSkipped();
      }
    } catch (caughtError) {
      if (caughtError instanceof DOMException && caughtError.name === "AbortError") return;
      // Fail open -- see the `LlmConfigState` doc comment.
      setState("unknown");
    }
  }, []);

  // Keyed off auth rather than run once: the status endpoint needs a session, and logging in
  // flips `authStatus` without remounting this provider.
  useEffect(() => {
    if (authStatus !== "authenticated") {
      setState("unknown");
      setSource(null);
      setBaseUrl(null);
      setModel(null);
      return;
    }
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [authStatus, load]);

  const value = useMemo<LlmStatusValue>(
    () => ({
      state,
      source,
      baseUrl,
      model,
      refresh: () => load(),
      markConfigured: (nextSource) => {
        setState("configured");
        setSource(nextSource);
      },
    }),
    [state, source, baseUrl, model, load],
  );

  return <LlmStatusContext.Provider value={value}>{children}</LlmStatusContext.Provider>;
}
