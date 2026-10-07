import { CheckCircle2, XCircle } from "lucide-react";

import { Spinner } from "../ui/Spinner";
import type { LlmProbeResponse } from "../../lib/api";

/**
 * Renders the connection test's three states in place, above the action row so the buttons
 * never move. There is no toast library in this codebase, and the house convention is that
 * result boxes persist rather than auto-dismiss.
 */
export type ProbeState =
  | { kind: "idle" }
  | { kind: "testing"; host: string }
  | { kind: "done"; result: LlmProbeResponse };

export function hostOf(baseUrl: string): string {
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl;
  }
}

/**
 * The single highest-value hint on the page: inside a container, `localhost` is the Enigma
 * backend, not the user's machine, which is the number-one first-run failure with Ollama and
 * LM Studio.
 */
function troubleshooting(result: LlmProbeResponse, baseUrl: string): string | null {
  if (result.ok) return null;
  const message = result.message.toLowerCase();
  const host = hostOf(baseUrl);
  const isLoopback = /^(localhost|127\.0\.0\.1|\[::1\])(:|$)/.test(host);
  if (isLoopback && message.includes("could not reach")) {
    return "Inside a container, localhost is the Enigma backend rather than your own machine. Try http://host.containers.internal:11434/v1 (or host.docker.internal on Docker).";
  }
  if (message.includes("rejected the api key")) {
    return "Check the key, and that it belongs to this endpoint.";
  }
  if (message.includes("/v1")) {
    return "Most providers expect the base URL to end in /v1.";
  }
  return null;
}

export function ConnectionResult({ state, baseUrl }: { state: ProbeState; baseUrl: string }) {
  if (state.kind === "idle") {
    return null;
  }

  if (state.kind === "testing") {
    return (
      <div className="mt-4 flex items-center gap-2 rounded-md border border-line bg-panel px-3 py-2 text-sm text-body">
        <Spinner />
        Contacting {state.host}...
      </div>
    );
  }

  const { result } = state;
  const hint = troubleshooting(result, baseUrl);

  if (result.ok) {
    return (
      <div className="mt-4 rounded-md border border-signal/30 bg-[#f0faf5] px-3 py-2 text-sm text-signal">
        <p className="flex items-center gap-2">
          <CheckCircle2 className="h-4 w-4 shrink-0" aria-hidden="true" />
          {result.message}
        </p>
        {result.latency_ms !== null && (
          <p className="mt-1 font-mono text-xs">{result.latency_ms} ms</p>
        )}
      </div>
    );
  }

  return (
    <div className="mt-4 rounded-md border border-danger/30 bg-[#fff5f2] px-3 py-2 text-sm text-danger">
      <p className="flex items-center gap-2">
        <XCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
        {result.message}
      </p>
      {hint && <p className="mt-2 text-xs text-body">{hint}</p>}
    </div>
  );
}
