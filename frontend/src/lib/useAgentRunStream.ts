import { useCallback, useEffect, useRef, useState } from "react";

import {
  getAgentStreamUrl,
  type AgentEventResponse,
  type AgentRunStatus,
} from "./api";

export type StreamConnectionState =
  | "idle"
  | "connecting"
  | "live"
  | "reconnecting"
  | "disconnected";

export interface UseAgentRunStreamOptions {
  /** When false (or runId is null) no socket is opened. Defaults to true. */
  enabled?: boolean;
  /** Fired once when the run reaches a terminal status (completed/error/stopped). */
  onTerminal?: (status: AgentRunStatus) => void;
}

export interface UseAgentRunStreamResult {
  events: AgentEventResponse[];
  connectionState: StreamConnectionState;
  runStatus: AgentRunStatus | null;
  agentError: string | null;
}

const RECONNECT_DELAYS = [1000, 2000, 4000];
const MAX_RECONNECT_RETRIES = 3;
const TERMINAL_RUN_STATUSES: AgentRunStatus[] = ["completed", "error", "stopped"];

function isTerminal(status: string | undefined): status is AgentRunStatus {
  return status !== undefined && TERMINAL_RUN_STATUSES.includes(status as AgentRunStatus);
}

/**
 * Subscribe to a single agent run's WebSocket event stream.
 *
 * Consolidates the socket/reconnect logic that used to live in both `AgentTab` and
 * `SpecPanel`'s `PhaseStream`. Behaviour (matching `PhaseStream`, which is the correct one):
 * - Clears `events` on every (re)connect, because the backend replays all persisted events on
 *   connect — clearing avoids duplicating them.
 * - Ignores `{ type: "ping" }` keepalive frames.
 * - Detects terminal status from a `status` frame's content OR `metadata.status`, fires
 *   `onTerminal` exactly once, and stops (no reconnect).
 * - Reconnects with backoff up to 3 attempts on unexpected drops.
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
  const [connectionState, setConnectionState] = useState<StreamConnectionState>("idle");
  const [runStatus, setRunStatus] = useState<AgentRunStatus | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const intentionalCloseRef = useRef(false);
  const runDoneRef = useRef(false);

  // Keep the latest onTerminal without retriggering the connect effect.
  const onTerminalRef = useRef<UseAgentRunStreamOptions["onTerminal"]>(onTerminal);
  useEffect(() => {
    onTerminalRef.current = onTerminal;
  }, [onTerminal]);

  const active = enabled && Boolean(workspaceId) && Boolean(runId);

  const connect = useCallback(() => {
    if (!workspaceId || !runId) return;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (socketRef.current) {
      intentionalCloseRef.current = true;
      socketRef.current.close();
    }

    // Backend replays all prior events on every (re)connect; reset to avoid duplicates.
    setEvents([]);

    const socket = new WebSocket(getAgentStreamUrl(workspaceId, runId));
    socketRef.current = socket;
    intentionalCloseRef.current = false;

    socket.onopen = () => {
      setConnectionState("live");
      reconnectAttemptRef.current = 0;
    };

    socket.onmessage = (event) => {
      let message: AgentEventResponse & { type?: string };
      try {
        message = JSON.parse(event.data);
      } catch {
        return; // ignore malformed frames
      }
      if (message.type === "ping") {
        return; // keepalive frame, nothing to render
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
      const terminal = isTerminal(statusValue) ? statusValue : isTerminal(metadataStatus)
        ? metadataStatus
        : undefined;
      if (terminal && !runDoneRef.current) {
        runDoneRef.current = true;
        setRunStatus(terminal);
        onTerminalRef.current?.(terminal);
      }
    };

    socket.onclose = () => {
      if (intentionalCloseRef.current || runDoneRef.current || socketRef.current !== socket) {
        return;
      }

      const attempt = reconnectAttemptRef.current + 1;
      reconnectAttemptRef.current = attempt;

      if (attempt > MAX_RECONNECT_RETRIES) {
        setConnectionState("disconnected");
        return;
      }

      setConnectionState("reconnecting");
      const delay = RECONNECT_DELAYS[Math.min(attempt - 1, RECONNECT_DELAYS.length - 1)];
      reconnectTimeoutRef.current = setTimeout(connect, delay);
    };
  }, [workspaceId, runId]);

  useEffect(() => {
    if (!active) {
      setConnectionState("idle");
      setEvents([]);
      setRunStatus(null);
      setAgentError(null);
      return;
    }

    reconnectAttemptRef.current = 0;
    runDoneRef.current = false;
    setRunStatus(null);
    setAgentError(null);
    setConnectionState("connecting");
    connect();

    return () => {
      intentionalCloseRef.current = true;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [active, connect]);

  return { events, connectionState, runStatus, agentError };
}
