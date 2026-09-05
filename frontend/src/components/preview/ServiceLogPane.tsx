import { useEffect, useRef, useState } from "react";

import { getServiceLogStreamUrl } from "../../lib/api";
import { useReconnectingWebSocket } from "../../lib/useReconnectingWebSocket";

// Keep the client-side buffer bounded — the server already caps its own replay, but a
// long-lived stream should not grow the DOM node without bound.
const MAX_BUFFER_CHARS = 200_000;
// Stay pinned to the bottom only while the viewer hasn't scrolled away from it.
const AUTO_SCROLL_THRESHOLD_PX = 40;

export function ServiceLogPane({
  workspaceId,
  serviceId,
}: {
  workspaceId: string;
  serviceId: string;
}) {
  const [output, setOutput] = useState("");
  const preRef = useRef<HTMLPreElement>(null);
  const stickToBottomRef = useRef(true);

  const url = getServiceLogStreamUrl(workspaceId, serviceId);
  const { connectionState } = useReconnectingWebSocket(url, {
    maxRetries: Infinity,
    onBeforeConnect: () => {
      // The server replays its buffered log on every (re)connect, so a reconnect must start
      // from a blank buffer to avoid duplicating everything already shown.
      setOutput("");
    },
    onMessage: (event) => {
      let payload: { type?: string; content?: string };
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.type !== "output" || typeof payload.content !== "string") {
        return;
      }
      setOutput((current) => {
        const next = `${current}${payload.content}`;
        return next.length > MAX_BUFFER_CHARS ? next.slice(next.length - MAX_BUFFER_CHARS) : next;
      });
    },
  });

  useEffect(() => {
    const el = preRef.current;
    if (el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [output]);

  function handleScroll() {
    const el = preRef.current;
    if (!el) return;
    stickToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < AUTO_SCROLL_THRESHOLD_PX;
  }

  return (
    <div className="mt-2">
      {connectionState === "disconnected" && (
        <p className="mb-1 text-xs text-danger">Log stream disconnected.</p>
      )}
      <pre
        ref={preRef}
        onScroll={handleScroll}
        className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-panel p-3 font-mono text-xs leading-5 text-ink"
      >
        {output || "Waiting for output…"}
      </pre>
    </div>
  );
}
