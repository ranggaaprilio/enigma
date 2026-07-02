import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentSessionResponse } from "../../lib/api";
import { AgentTab } from "./index";

// SessionThread opens a WebSocket only when a turn is active; stub it so nothing real happens.
class MockWebSocket {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readyState = 1;
  constructor(public url: string) {}
  close() {
    this.readyState = 3;
  }
  send() {}
}

const WS = "3e15be87-2551-4d70-b972-33fdf68c49eb";

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => payload };
}

function makeSession(overrides: Partial<AgentSessionResponse> = {}): AgentSessionResponse {
  return {
    id: "sess-1",
    workspace_id: WS,
    user_id: "user-1",
    title: "Existing session",
    status: "active",
    created_at: "2026-06-12T10:00:00Z",
    updated_at: "2026-06-12T10:00:00Z",
    ...overrides,
  };
}

function emptyDetail(session: AgentSessionResponse) {
  return { ...session, turns: [] };
}

afterEach(() => vi.restoreAllMocks());

function stubWebSocket() {
  vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
}

describe("AgentTab (session-centric)", () => {
  it("lists sessions, auto-selects the first, and loads its thread", async () => {
    stubWebSocket();
    const session = makeSession({ id: "sess-1", title: "Fix parser" });
    const fetchMock = vi.fn((url: string) => {
      if (url.includes(`/agent/sessions/sess-1`)) return Promise.resolve(jsonResponse(emptyDetail(session)));
      if (url.includes(`/agent/sessions`)) return Promise.resolve(jsonResponse([session]));
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={WS} />);

    // The title appears in both the sidebar list and the thread header; the empty-hero loads
    // for the auto-selected session.
    expect((await screen.findAllByText("Fix parser")).length).toBeGreaterThanOrEqual(1);
    expect(await screen.findByText(/What should the agent work on/i)).toBeInTheDocument();
  });

  it("shows an empty state and creates a new session", async () => {
    stubWebSocket();
    const created = makeSession({ id: "sess-new", title: null });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith(`/agent/sessions`) && init?.method === "POST")
        return Promise.resolve(jsonResponse(created, true, 201));
      if (url.includes(`/agent/sessions/sess-new`)) return Promise.resolve(jsonResponse(emptyDetail(created)));
      if (url.includes(`/agent/sessions`)) return Promise.resolve(jsonResponse([]));
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={WS} />);

    expect(await screen.findByText(/No sessions yet/i)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("new-session"));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/agent/sessions`),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    // The new (untitled) session becomes selected and its empty thread loads.
    expect(await screen.findByText(/What should the agent work on/i)).toBeInTheDocument();
    expect(screen.getAllByText("Untitled session").length).toBeGreaterThanOrEqual(1);
  });

  it("renders the no-workspace empty state", () => {
    render(<AgentTab workspaceId={null} />);
    expect(screen.getByText("No workspace selected")).toBeInTheDocument();
  });

  it("deletes a session after confirmation", async () => {
    stubWebSocket();
    const session = makeSession({ id: "sess-1", title: "Doomed" });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes(`/agent/sessions/sess-1`) && init?.method === "DELETE")
        return Promise.resolve(jsonResponse({ ok: true }));
      if (url.includes(`/agent/sessions/sess-1`)) return Promise.resolve(jsonResponse(emptyDetail(session)));
      if (url.includes(`/agent/sessions`)) return Promise.resolve(jsonResponse([session]));
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={WS} />);

    await userEvent.click(await screen.findByLabelText("Delete session"));
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/agent/sessions/sess-1`),
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
  });
});
