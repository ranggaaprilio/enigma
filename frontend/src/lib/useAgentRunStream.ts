import { useCallback, useEffect, useRef, useState } from "react";

import {
  getAgentStreamUrl,
  isServerReady,
  type AgentEventResponse,
  type AgentRunStatus,
} from "./api";
import {
  useReconnectingWebSocket,
  type StreamConnectionState,
} from "./useReconnectingWebSocket";

export type { StreamConnectionState };

export interface UseAgentRunStreamOptions {
  /** When false (or runId is null) no socket is opened. Defaults to true. */
  enabled?: boolean;
  /** Fired once when the run reaches a terminal status (completed/error/stopped). */
  onTerminal?: (status: AgentRunStatus) => void;
}

export type AgentRunViewerRole = "driver" | "observer";

export interface UseAgentRunStreamResult {
  events: AgentEventResponse[];
  connectionState: StreamConnectionState;
  runStatus: AgentRunStatus | null;
  agentError: string | null;
  /** Whether this viewer can drive the run (send input/stop it) or is only observing.
   * Null until the backend's initial "role" frame arrives. */
  viewerRole: AgentRunViewerRole | null;
}

const TERMINAL_RUN_STATUSES: AgentRunStatus[] = ["completed", "error", "stopped"];

function isTerminal(status: string | undefined): status is AgentRunStatus {
  return status !== undefined && TERMINAL_RUN_STATUSES.includes(status as AgentRunStatus);
}

/**
 * Subscribe to a single agent run's WebSocket event stream.
 *
 * Builds the agent-specific event/terminal logic on top of the shared
 * {@link useReconnectingWebSocket} state machine:
 * - Clears `events` on every (re)connect, because the backend replays all persisted events on
 *   connect — clearing avoids duplicating them.
 * - Ignores `{ type: "ping" }` keepalive frames.
 * - Detects terminal status from a `status` frame's content, `metadata.status`, or an
 *   `error` event (the backend's crash path emits one with neither), fires `onTerminal`
 *   exactly once, and stops reconnecting.
 * - Otherwise reconnects with backoff up to 3 attempts on unexpected drops.
 *
 * The hook is intentionally "dumb" about persistence: when a run finishes, the caller's
 * `onTerminal` is responsible for refetching the persisted turn/events.
 */
export function useAgentRunStream(
  workspaceId: string | null,
  runId: string | null,
  options: UseAgentRunStreamOptions = {},
): UseAgentRunStreamResult {
  const { enabled = true, onTerminal } = options;

  const [events, setEvents] = useState<AgentEventResponse[]>([]);
  const [runStatus, setRunStatus] = useState<AgentRunStatus | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [viewerRole, setViewerRole] = useState<AgentRunViewerRole | null>(null);

  const runDoneRef = useRef(false);
  const stopReconnectRef = useRef<() => void>(() => {});

  // Keep the latest onTerminal without retriggering the connect effect.
  const onTerminalRef = useRef<UseAgentRunStreamOptions["onTerminal"]>(onTerminal);
  useEffect(() => {
    onTerminalRef.current = onTerminal;
  }, [onTerminal]);

  const active = enabled && Boolean(workspaceId) && Boolean(runId);
  const url = active ? getAgentStreamUrl(workspaceId as string, runId as string) : null;

  const handleBeforeConnect = useCallback(() => {
    // Backend replays all prior events on every (re)connect; reset to avoid duplicates.
    setEvents([]);
  }, []);

  const handleMessage = useCallback((event: MessageEvent) => {
    let message: AgentEventResponse & { type?: string };
    try {
      message = JSON.parse(event.data);
    } catch {
      return; // ignore malformed frames
    }
    if (message.type === "ping") {
      return; // keepalive frame, nothing to render
    }
    if (message.type === "role") {
      const role = (message as unknown as { content?: string }).content;
      if (role === "driver" || role === "observer") {
        setViewerRole(role);
      }
      return;
    }
    setEvents((prev) => [...prev, message]);

    if (message.event_type === "status" && message.content) {
      setRunStatus(message.content as AgentRunStatus);
    }
    if (message.event_type === "error") {
      setAgentError(message.content);
    }

    const statusValue = message.event_type === "status" ? message.content : undefined;
    const metadataStatus = message.metadata?.status as string | undefined;
    // An `error` event *is* terminal, whatever its metadata says. The backend's crash path
    // persists one with no status frame, so keying only off status left `onTerminal`
    // unfired: the turn stayed on "Running", the composer stayed in stop-mode, and the
    // socket kept pinging forever.
    const errorIsTerminal = message.event_type === "error" ? "error" : undefined;
    const terminal = isTerminal(statusValue)
      ? statusValue
      : isTerminal(metadataStatus)
        ? metadataStatus
        : (errorIsTerminal as AgentRunStatus | undefined);
    if (terminal && !runDoneRef.current) {
      runDoneRef.current = true;
      setRunStatus(terminal);
      stopReconnectRef.current();
      onTerminalRef.current?.(terminal);
    }
  }, []);

  const { connectionState, stopReconnect } = useReconnectingWebSocket(url, {
    enabled,
    // A deploy drops the socket; wait for the new instance instead of burning retries.
    readinessCheck: isServerReady,
    onBeforeConnect: handleBeforeConnect,
    onMessage: handleMessage,
  });

  useEffect(() => {
    stopReconnectRef.current = stopReconnect;
  }, [stopReconnect]);

  // Reset per-run state whenever the target run changes or the stream deactivates.
  useEffect(() => {
    runDoneRef.current = false;
    setRunStatus(null);
    setAgentError(null);
    setViewerRole(null);
    if (!active) {
      setEvents([]);
    }
  }, [active, url]);

  return { events, connectionState, runStatus, agentError, viewerRole };
}
