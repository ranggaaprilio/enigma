import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NotificationResponse } from "../lib/api";

const listNotifications = vi.fn<() => Promise<NotificationResponse[]>>();

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  listNotifications: () => listNotifications(),
  getNotificationStreamUrl: () => "ws://example.test/notifications/stream",
  isServerReady: async () => true,
}));

import { NotificationBell } from "./DashboardShell";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;

  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  readyState = 1;
  url: string;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }

  send() {}
}

function notification(id: string, title: string, createdAt: string): NotificationResponse {
  return {
    id,
    user_id: "u1",
    workspace_id: "w1",
    type: "agent_run_completed",
    title,
    message: "",
    read_at: null,
    created_at: createdAt,
  } as NotificationResponse;
}

describe("NotificationBell", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    MockWebSocket.instances = [];
    listNotifications.mockReset();
  });

  it("backfills notifications created while the stream was down", async () => {
    const first = notification("n1", "First run done", "2026-10-08T10:00:00Z");
    const missed = notification("n2", "Missed while offline", "2026-10-08T10:05:00Z");
    listNotifications.mockResolvedValueOnce([first]).mockResolvedValue([missed, first]);

    render(<NotificationBell />);
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    act(() => MockWebSocket.instances[0].onopen?.());
    await waitFor(() => expect(listNotifications).toHaveBeenCalledTimes(2));

    // The replica serving the stream goes away; the hook reconnects to another one.
    act(() => MockWebSocket.instances[0].close());
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(2), { timeout: 3000 });
    act(() => MockWebSocket.instances[1].onopen?.());

    await waitFor(() => expect(listNotifications).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    expect(await screen.findByText("Missed while offline")).toBeInTheDocument();
    expect(screen.getAllByText("First run done")).toHaveLength(1);
  });

  it("recovers when the mount fetch failed (backend still deploying)", async () => {
    const pending = notification("n1", "Waiting for you", "2026-10-08T10:00:00Z");
    listNotifications.mockRejectedValueOnce(new Error("503")).mockResolvedValue([pending]);

    render(<NotificationBell />);
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    act(() => MockWebSocket.instances[0].onopen?.());

    fireEvent.click(screen.getByRole("button", { name: "Notifications" }));
    expect(await screen.findByText("Waiting for you")).toBeInTheDocument();
  });

  it("keeps a notification read when a stale refetch says unread", async () => {
    const unread = notification("n1", "Run done", "2026-10-08T10:00:00Z");
    listNotifications.mockResolvedValue([unread]);
    render(<NotificationBell />);
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    act(() =>
      MockWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({ ...unread, read_at: "2026-10-08T10:01:00Z" }),
      } as MessageEvent),
    );
    act(() => MockWebSocket.instances[0].onopen?.());
    await waitFor(() => expect(listNotifications).toHaveBeenCalledTimes(2));
    await act(async () => {}); // let the refetch's merge land

    expect(screen.getByRole("button", { name: "Notifications" })).not.toHaveTextContent("1");
  });
});
