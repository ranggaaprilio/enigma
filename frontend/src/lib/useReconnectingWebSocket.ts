import { useCallback, useEffect, useRef, useState } from "react";

export type StreamConnectionState =
  | "idle"
  | "connecting"
  | "live"
  | "reconnecting"
  | "disconnected";

export const DEFAULT_RECONNECT_DELAYS = [1000, 2000, 4000];
export const DEFAULT_MAX_RECONNECT_RETRIES = 3;
/** How often a held reconnect re-probes `readinessCheck`. */
export const READY_POLL_INTERVAL_MS = 2000;
/** Stop holding for readiness after this long and fall back to plain retries. */
export const READY_WAIT_LIMIT_MS = 60_000;

export interface UseReconnectingWebSocketOptions {
  /** When false (or url is null) no socket is opened. Defaults to true. */
  enabled?: boolean;
  /** Set on the socket before connecting (e.g. "arraybuffer" for xterm binary). */
  binaryType?: BinaryType;
  /** Backoff schedule; the last entry is reused once exhausted. */
  reconnectDelays?: number[];
  /** Give up after this many consecutive failed reconnects. Use Infinity to retry forever. */
  maxRetries?: number;
  /** Called immediately before each (re)connect — use to reset per-connection state. */
  onBeforeConnect?: () => void;
  /**
   * Probed before each reconnect. While it resolves false (or rejects) — e.g. the server is
   * draining for a deploy — the reconnect is held in "reconnecting" without spending a retry,
   * re-probing every {@link READY_POLL_INTERVAL_MS} for up to {@link READY_WAIT_LIMIT_MS}.
   */
  readinessCheck?: () => Promise<boolean>;
  onOpen?: (socket: WebSocket) => void;
  onMessage?: (event: MessageEvent) => void;
  onError?: (event: Event) => void;
  onClose?: (event: CloseEvent) => void;
}

export interface UseReconnectingWebSocketResult {
  connectionState: StreamConnectionState;
  reconnectAttempt: number;
  /** Send on the live socket; a no-op when not open. */
  send: (data: string | ArrayBufferLike | Blob | ArrayBufferView) => void;
  /** Stop reconnecting until `url`/`enabled` changes (e.g. the stream reached a terminal state). */
  stopReconnect: () => void;
}

/**
 * A single WebSocket connection with automatic backoff reconnection.
 *
 * Generalises the socket/reconnect state machine that originated in `useAgentRunStream`:
 * exponential-ish backoff, a cap on retries, and guards against reconnecting after an
 * intentional close, a caller-signalled stop, or a superseded socket. Callbacks are read
 * through a ref at call time (not destructured once per connection), so passing fresh
 * closures each render both avoids tearing down the connection and is reflected immediately
 * on the next event — the connect effect itself only re-runs when `enabled` or `url` change.
 */
export function useReconnectingWebSocket(
  url: string | null,
  options: UseReconnectingWebSocketOptions = {},
): UseReconnectingWebSocketResult {
  const { enabled = true } = options;

  const [connectionState, setConnectionState] = useState<StreamConnectionState>("idle");
  const [reconnectAttempt, setReconnectAttempt] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const intentionalCloseRef = useRef(false);
  const stoppedRef = useRef(false);
  const readyWaitStartedRef = useRef<number | null>(null);

  // Keep the latest options without retriggering the connect effect.
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  });

  const connect = useCallback(() => {
    if (!url) return;

    const {
      binaryType,
      reconnectDelays = DEFAULT_RECONNECT_DELAYS,
      maxRetries = DEFAULT_MAX_RECONNECT_RETRIES,
    } = optionsRef.current;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (socketRef.current) {
      intentionalCloseRef.current = true;
      socketRef.current.close();
    }

    optionsRef.current.onBeforeConnect?.();

    const socket = new WebSocket(url);
    if (binaryType) socket.binaryType = binaryType;
    socketRef.current = socket;
    intentionalCloseRef.current = false;

    const reconnect = async () => {
      reconnectTimeoutRef.current = null;
      const { readinessCheck } = optionsRef.current;
      if (readinessCheck) {
        const ready = await readinessCheck().catch(() => false);
        // Unmounted, re-targeted or stopped while the probe was in flight.
        if (intentionalCloseRef.current || stoppedRef.current || socketRef.current !== socket) {
          readyWaitStartedRef.current = null;
          return;
        }
        if (!ready) {
          readyWaitStartedRef.current ??= Date.now();
          if (Date.now() - readyWaitStartedRef.current < READY_WAIT_LIMIT_MS) {
            reconnectTimeoutRef.current = setTimeout(reconnect, READY_POLL_INTERVAL_MS);
            return;
          }
        }
      }
      readyWaitStartedRef.current = null;
      connect();
    };

    socket.onopen = () => {
      setConnectionState("live");
      reconnectAttemptRef.current = 0;
      setReconnectAttempt(0);
      optionsRef.current.onOpen?.(socket);
    };

    socket.onmessage = (event) => optionsRef.current.onMessage?.(event);
    socket.onerror = (event) => optionsRef.current.onError?.(event);

    socket.onclose = (event) => {
      optionsRef.current.onClose?.(event);

      // Do not reconnect if we closed on purpose, the caller stopped us, or this socket
      // has already been superseded by a newer one.
      if (intentionalCloseRef.current || stoppedRef.current || socketRef.current !== socket) {
        return;
      }

      const attempt = reconnectAttemptRef.current + 1;
      reconnectAttemptRef.current = attempt;
      setReconnectAttempt(attempt);

      if (attempt > maxRetries) {
        setConnectionState("disconnected");
        return;
      }

      setConnectionState("reconnecting");
      const delay = reconnectDelays[Math.min(attempt - 1, reconnectDelays.length - 1)];
      reconnectTimeoutRef.current = setTimeout(reconnect, delay);
    };
  }, [url]);

  useEffect(() => {
    if (!enabled || !url) {
      setConnectionState("idle");
      return;
    }

    stoppedRef.current = false;
    reconnectAttemptRef.current = 0;
    readyWaitStartedRef.current = null;
    setReconnectAttempt(0);
    setConnectionState("connecting");
    connect();

    return () => {
      intentionalCloseRef.current = true;
      readyWaitStartedRef.current = null;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [enabled, url, connect]);

  const send = useCallback(
    (data: string | ArrayBufferLike | Blob | ArrayBufferView) => {
      const socket = socketRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(data);
      }
    },
    [],
  );

  const stopReconnect = useCallback(() => {
    stoppedRef.current = true;
  }, []);

  return { connectionState, reconnectAttempt, send, stopReconnect };
}
