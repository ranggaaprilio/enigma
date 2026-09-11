import { useCallback, useEffect, useState } from "react";

import { getPresenceStreamUrl, type PresenceUser } from "./api";
import { useReconnectingWebSocket, type StreamConnectionState } from "./useReconnectingWebSocket";

export interface UsePresenceResult {
  viewers: PresenceUser[];
  connectionState: StreamConnectionState;
}

type PresenceFrame =
  | { type: "snapshot"; viewers: PresenceUser[] }
  | ({ type: "join" } & PresenceUser)
  | ({ type: "leave" } & Pick<PresenceUser, "user_id">);

/**
 * Tracks who else is viewing a workspace, backed by the presence WebSocket.
 *
 * Builds a `Map<user_id, PresenceUser>` from the initial snapshot plus join/leave deltas.
 * Retries indefinitely (like the notification stream) rather than giving up after a few
 * attempts (like the agent-run stream), since presence has no terminal state to reach.
 */
export function usePresence(workspaceId: string | null): UsePresenceResult {
  const [viewers, setViewers] = useState<Map<string, PresenceUser>>(new Map());

  const handleBeforeConnect = useCallback(() => {
    setViewers(new Map());
  }, []);

  const handleMessage = useCallback((event: MessageEvent) => {
    let frame: PresenceFrame;
    try {
      frame = JSON.parse(event.data);
    } catch {
      return;
    }
    if (frame.type === "snapshot") {
      setViewers(new Map(frame.viewers.map((viewer) => [viewer.user_id, viewer])));
    } else if (frame.type === "join") {
      const viewer: PresenceUser = {
        user_id: frame.user_id,
        name: frame.name,
        connection_id: frame.connection_id,
        joined_at: frame.joined_at,
      };
      setViewers((prev) => new Map(prev).set(viewer.user_id, viewer));
    } else if (frame.type === "leave") {
      setViewers((prev) => {
        if (!prev.has(frame.user_id)) return prev;
        const next = new Map(prev);
        next.delete(frame.user_id);
        return next;
      });
    }
  }, []);

  const url = workspaceId ? getPresenceStreamUrl(workspaceId) : null;
  const { connectionState } = useReconnectingWebSocket(url, {
    enabled: Boolean(workspaceId),
    maxRetries: Infinity,
    onBeforeConnect: handleBeforeConnect,
    onMessage: handleMessage,
  });

  useEffect(() => {
    if (!workspaceId) setViewers(new Map());
  }, [workspaceId]);

  return { viewers: Array.from(viewers.values()), connectionState };
}
