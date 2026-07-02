import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  AgentRunResponse,
  AgentSessionDetailResponse,
  AgentSessionResponse,
} from "../../lib/api";
import { SessionThread } from "./SessionThread";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  readyState = 1;
  constructor(public url: string) {
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

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => payload };
}

function makeSession(overrides: Partial<AgentSessionResponse> = {}): AgentSessionResponse {
  return {
    id: "sess-1",
    workspace_id: WS,
    user_id: "user-1",
    title: "My session",
    status: "active",
    created_at: "2026-06-12T10:00:00Z",
    updated_at: "2026-06-12T10:00:00Z",
    ...overrides,
  };
}

function makeRun(overrides: Partial<AgentRunResponse> = {}): AgentRunResponse {
  return {
    id: "run-1",
    workspace_id: WS,
    user_id: "user-1",
    session_id: "sess-1",
    prompt: "Do the thing",
    status: "completed",
    provider: "mock",
    model: "mock-agent",
    tokens_used: 10,
    input_tokens: 6,
    output_tokens: 4,
    estimated_cost: 0.00001,
    started_at: "2026-06-12T10:00:00Z",
    completed_at: "2026-06-12T10:01:00Z",
    error_message: null,
    ...overrides,
  };
}

function detailWith(turns: AgentRunResponse[]): AgentSessionDetailResponse {
  return { ...makeSession(), turns };
}

afterEach(() => {
  vi.restoreAllMocks();
  MockWebSocket.instances = [];
});

function stubWebSocket() {
  vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
}

describe("SessionThread", () => {
  it("renders existing turns from the session detail", async () => {
    stubWebSocket();
    const turn = makeRun({ id: "run-1", prompt: "Explain the bug" });
    const fetchMock = vi.fn((url: string) => {
      if (url.includes(`/agent/sessions/sess-1`)) return Promise.resolve(jsonResponse(detailWith([turn])));
      if (url.includes(`/agent/runs/run-1`))
        return Promise.resolve(
          jsonResponse({ ...turn, events: [{ id: "e1", agent_run_id: "run-1", event_type: "output_text", content: "Here it is", metadata: {}, tool_call_id: null, created_at: "x" }] }),
        );
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SessionThread
        workspaceId={WS}
        session={makeSession()}
        worktreeId={null}
        worktreeBranch={null}
        onSessionUpdated={vi.fn()}
      />,
    );

    expect(await screen.findByText("Explain the bug")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Here it is")).toBeInTheDocument());
    expect(screen.queryByText(/What should the agent work on/i)).not.toBeInTheDocument();
  });

  it("shows the empty hero when the session has no turns", async () => {
    stubWebSocket();
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(detailWith([]))));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SessionThread
        workspaceId={WS}
        session={makeSession()}
        worktreeId={null}
        worktreeBranch={null}
        onSessionUpdated={vi.fn()}
      />,
    );

    expect(await screen.findByText(/What should the agent work on/i)).toBeInTheDocument();
  });

  it("posts a turn on continue and streams its output", async () => {
    stubWebSocket();
    const newRun = makeRun({ id: "run-2", prompt: "Add tests", status: "thinking" });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith(`/agent/sessions/sess-1/turns`) && init?.method === "POST")
        return Promise.resolve(jsonResponse(newRun, true, 201));
      if (url.includes(`/agent/sessions/sess-1`)) return Promise.resolve(jsonResponse(detailWith([])));
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SessionThread
        workspaceId={WS}
        session={makeSession()}
        worktreeId={null}
        worktreeBranch={null}
        onSessionUpdated={vi.fn()}
      />,
    );

    const input = await screen.findByPlaceholderText(/Describe what you want the agent to do/i);
    await userEvent.type(input, "Add tests");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    // The appended turn's prompt shows, and a WebSocket opens for it.
    expect(await screen.findByText("Add tests")).toBeInTheDocument();
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));

    MockWebSocket.instances[0].onopen?.();
    MockWebSocket.instances[0].emit({
      event_type: "output_text",
      content: "Working on it",
      metadata: {},
    });
    await waitFor(() => expect(screen.getByText("Working on it")).toBeInTheDocument());
  });

  it("keeps the prompt and shows an error when a turn is rejected with 409", async () => {
    stubWebSocket();
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith(`/agent/sessions/sess-1/turns`) && init?.method === "POST")
        return Promise.resolve(
          jsonResponse({ detail: "Workspace already has an active agent run" }, false, 409),
        );
      if (url.includes(`/agent/sessions/sess-1`)) return Promise.resolve(jsonResponse(detailWith([])));
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <SessionThread
        workspaceId={WS}
        session={makeSession()}
        worktreeId={null}
        worktreeBranch={null}
        onSessionUpdated={vi.fn()}
      />,
    );

    const input = await screen.findByPlaceholderText(/Describe what you want the agent to do/i);
    await userEvent.type(input, "Add tests");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    // Error surfaced and the prompt is preserved (not cleared).
    expect(
      await screen.findByText(/Workspace already has an active agent run/i),
    ).toBeInTheDocument();
    expect(input).toHaveValue("Add tests");
  });
});
