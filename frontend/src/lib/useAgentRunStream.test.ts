import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAgentRunStream } from "./useAgentRunStream";

class MockWebSocket {
  static instances: MockWebSocket[] = [];

  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readyState = 1;
  url: string;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }

  send() {}

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

const WS = "ws-1";
const RUN = "run-1";

function stubWebSocket() {
  vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
}

function latestSocket() {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

describe("useAgentRunStream", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    MockWebSocket.instances = [];
  });

  it("opens no socket when disabled or runId is null", () => {
    stubWebSocket();
    const { result, rerender } = renderHook(
      ({ runId, enabled }) => useAgentRunStream(WS, runId, { enabled }),
      { initialProps: { runId: null as string | null, enabled: true } },
    );
    expect(MockWebSocket.instances.length).toBe(0);
    expect(result.current.connectionState).toBe("idle");

    rerender({ runId: RUN, enabled: false });
    expect(MockWebSocket.instances.length).toBe(0);
    expect(result.current.connectionState).toBe("idle");
  });

  it("connects and goes live, tracking status and error frames", () => {
    stubWebSocket();
    const { result } = renderHook(() => useAgentRunStream(WS, RUN));

    expect(MockWebSocket.instances.length).toBe(1);
    act(() => latestSocket().onopen?.());
    expect(result.current.connectionState).toBe("live");

    act(() =>
      latestSocket().emit({ event_type: "output_text", content: "hello", metadata: {} }),
    );
    act(() =>
      latestSocket().emit({ event_type: "status", content: "executing", metadata: {} }),
    );
    expect(result.current.events).toHaveLength(2);
    expect(result.current.runStatus).toBe("executing");

    act(() => latestSocket().emit({ event_type: "error", content: "boom", metadata: {} }));
    expect(result.current.agentError).toBe("boom");
  });

  it("ignores ping keepalive frames", () => {
    stubWebSocket();
    const { result } = renderHook(() => useAgentRunStream(WS, RUN));
    act(() => latestSocket().onopen?.());
    act(() => latestSocket().emit({ type: "ping" }));
    expect(result.current.events).toHaveLength(0);
  });

  it("clears events on reconnect so replayed events are not duplicated", () => {
    vi.useFakeTimers();
    stubWebSocket();
    const { result } = renderHook(() => useAgentRunStream(WS, RUN));

    act(() => latestSocket().onopen?.());
    act(() => latestSocket().emit({ event_type: "output_text", content: "a", metadata: {} }));
    expect(result.current.events).toHaveLength(1);

    // Unexpected drop → reconnect.
    act(() => latestSocket().close());
    expect(result.current.connectionState).toBe("reconnecting");

    act(() => vi.advanceTimersByTime(1000));
    expect(MockWebSocket.instances.length).toBe(2);

    // The backend replays the same event on the new connection; because the hook cleared
    // events on connect, there is exactly one, not two.
    act(() => latestSocket().onopen?.());
    act(() => latestSocket().emit({ event_type: "output_text", content: "a", metadata: {} }));
    expect(result.current.events).toHaveLength(1);
  });

  it("fires onTerminal once and does not reconnect after a terminal status", () => {
    vi.useFakeTimers();
    stubWebSocket();
    const onTerminal = vi.fn();
    renderHook(() => useAgentRunStream(WS, RUN, { onTerminal }));

    act(() => latestSocket().onopen?.());
    act(() =>
      latestSocket().emit({ event_type: "status", content: "completed", metadata: {} }),
    );
    expect(onTerminal).toHaveBeenCalledTimes(1);
    expect(onTerminal).toHaveBeenCalledWith("completed");

    // Socket closes after finishing — must NOT reconnect.
    act(() => latestSocket().close());
    act(() => vi.advanceTimersByTime(5000));
    expect(MockWebSocket.instances.length).toBe(1);
  });

  it("detects terminal status from metadata.status too", () => {
    stubWebSocket();
    const onTerminal = vi.fn();
    renderHook(() => useAgentRunStream(WS, RUN, { onTerminal }));
    act(() => latestSocket().onopen?.());
    act(() =>
      latestSocket().emit({
        event_type: "tool_result",
        content: "done",
        metadata: { status: "error" },
      }),
    );
    expect(onTerminal).toHaveBeenCalledWith("error");
  });

  it("reconnects up to 3 times then reports disconnected", () => {
    vi.useFakeTimers();
    stubWebSocket();
    const { result } = renderHook(() => useAgentRunStream(WS, RUN));

    // Never call onopen, so reconnectAttempt keeps climbing on each drop.
    act(() => latestSocket().close());
    act(() => vi.advanceTimersByTime(1000)); // attempt 1 → socket 2
    act(() => latestSocket().close());
    act(() => vi.advanceTimersByTime(2000)); // attempt 2 → socket 3
    act(() => latestSocket().close());
    act(() => vi.advanceTimersByTime(4000)); // attempt 3 → socket 4
    act(() => latestSocket().close()); // attempt 4 > max → disconnected

    expect(MockWebSocket.instances.length).toBe(4);
    expect(result.current.connectionState).toBe("disconnected");
  });

  it("closes the socket on unmount", () => {
    stubWebSocket();
    const { unmount } = renderHook(() => useAgentRunStream(WS, RUN));
    const socket = latestSocket();
    act(() => socket.onopen?.());
    unmount();
    expect(socket.readyState).toBe(3);
  });
});
