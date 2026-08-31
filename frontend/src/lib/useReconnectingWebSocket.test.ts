import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useReconnectingWebSocket } from "./useReconnectingWebSocket";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;

  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  readyState = 1;
  binaryType = "blob";
  url: string;
  sent: unknown[] = [];

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }

  send(data: unknown) {
    this.sent.push(data);
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

const URL = "wss://example.test/stream";

function stubWebSocket() {
  vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
}

function latestSocket() {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

describe("useReconnectingWebSocket", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    MockWebSocket.instances = [];
  });

  it("opens no socket when url is null or disabled", () => {
    stubWebSocket();
    const { result, rerender } = renderHook(
      ({ url, enabled }) => useReconnectingWebSocket(url, { enabled }),
      { initialProps: { url: null as string | null, enabled: true } },
    );
    expect(MockWebSocket.instances.length).toBe(0);
    expect(result.current.connectionState).toBe("idle");

    rerender({ url: URL, enabled: false });
    expect(MockWebSocket.instances.length).toBe(0);
    expect(result.current.connectionState).toBe("idle");
  });

  it("connects, goes live on open, and forwards messages", () => {
    stubWebSocket();
    const onMessage = vi.fn();
    const { result } = renderHook(() => useReconnectingWebSocket(URL, { onMessage }));

    expect(MockWebSocket.instances.length).toBe(1);
    act(() => latestSocket().onopen?.());
    expect(result.current.connectionState).toBe("live");

    act(() => latestSocket().emit({ hello: "world" }));
    expect(onMessage).toHaveBeenCalledTimes(1);
  });

  it("runs onBeforeConnect before each connection and sets binaryType", () => {
    stubWebSocket();
    const onBeforeConnect = vi.fn();
    renderHook(() =>
      useReconnectingWebSocket(URL, { onBeforeConnect, binaryType: "arraybuffer" }),
    );
    expect(onBeforeConnect).toHaveBeenCalledTimes(1);
    expect(latestSocket().binaryType).toBe("arraybuffer");
  });

  it("reconnects with backoff up to the retry cap, then reports disconnected", () => {
    vi.useFakeTimers();
    stubWebSocket();
    const { result } = renderHook(() => useReconnectingWebSocket(URL));

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

  it("does not reconnect after stopReconnect()", () => {
    vi.useFakeTimers();
    stubWebSocket();
    const { result } = renderHook(() => useReconnectingWebSocket(URL));

    act(() => latestSocket().onopen?.());
    act(() => result.current.stopReconnect());
    act(() => latestSocket().close());
    act(() => vi.advanceTimersByTime(5000));

    expect(MockWebSocket.instances.length).toBe(1);
  });

  it("send() forwards to the open socket", () => {
    stubWebSocket();
    const { result } = renderHook(() => useReconnectingWebSocket(URL));
    act(() => latestSocket().onopen?.());
    act(() => result.current.send("ping"));
    expect(latestSocket().sent).toContain("ping");
  });

  it("closes the socket on unmount", () => {
    stubWebSocket();
    const { unmount } = renderHook(() => useReconnectingWebSocket(URL));
    const socket = latestSocket();
    act(() => socket.onopen?.());
    unmount();
    expect(socket.readyState).toBe(3);
  });
});
