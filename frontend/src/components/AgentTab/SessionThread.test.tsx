import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  AgentRunResponse,
  AgentSessionDetailResponse,
  AgentSessionResponse,
  VerificationRunResponse,
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

  it("disables Run with a countdown when a turn is rate limited", async () => {
    stubWebSocket();
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith(`/agent/sessions/sess-1/turns`) && init?.method === "POST")
        return Promise.resolve({
          ...jsonResponse(
            { detail: "Your team is at its limit of concurrent agent runs.", scope: "team" },
            false,
            429,
          ),
          headers: new Headers({ "Retry-After": "15" }),
        });
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

    const notice = "Your team is at its limit of concurrent agent runs. Retry in 15s.";
    expect(await screen.findByText(notice)).toBeInTheDocument();
    const run = screen.getByRole("button", { name: "Run" });
    expect(run).toBeDisabled();
    expect(run).toHaveAttribute("title", notice);
    expect(input).toHaveValue("Add tests");
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

describe("SessionThread verification", () => {
  function verificationRun(
    overrides: Partial<VerificationRunResponse> = {},
  ): VerificationRunResponse {
    return {
      id: "vr-1",
      workspace_id: WS,
      plan_ref: "agent-run:run-1",
      status: "review",
      source_agent_run_id: "run-1",
      created_at: "2026-11-28T10:00:00Z",
      updated_at: "2026-11-28T10:00:00Z",
      ...overrides,
    };
  }

  /** Routes the session, its runs and the verification endpoints; `extra` handles the rest. */
  function stubFetch(
    turns: AgentRunResponse[],
    verifications: VerificationRunResponse[],
    extra: (url: string, init?: RequestInit) => unknown = () => undefined,
  ) {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      const handled = extra(url, init);
      if (handled !== undefined) return Promise.resolve(handled);
      if (url.includes("/verification/runs")) return Promise.resolve(jsonResponse(verifications));
      if (url.includes("/agent/sessions/sess-1")) return Promise.resolve(jsonResponse(detailWith(turns)));
      const runMatch = turns.find((t) => url.includes(`/agent/runs/${t.id}`));
      if (runMatch) return Promise.resolve(jsonResponse({ ...runMatch, events: [] }));
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  function renderThread() {
    return render(
      <MemoryRouter initialEntries={[`/workspaces/${WS}/agents`]}>
        <Routes>
          <Route
            path="/workspaces/:workspaceId/agents"
            element={
              <SessionThread
                workspaceId={WS}
                session={makeSession()}
                worktreeId={null}
                worktreeBranch={null}
                onSessionUpdated={vi.fn()}
              />
            }
          />
          <Route path="/workspaces/:workspaceId/verify" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  function LocationProbe() {
    const location = useLocation();
    return <p data-testid="location">{`${location.pathname}${location.search}`}</p>;
  }

  afterEach(() => {
    vi.useRealTimers();
  });

  it("offers Verify only on completed runs", async () => {
    stubWebSocket();
    stubFetch(
      [
        makeRun({ id: "run-1", prompt: "First", status: "completed" }),
        makeRun({ id: "run-2", prompt: "Second", status: "error" }),
        makeRun({ id: "run-3", prompt: "Third", status: "stopped" }),
      ],
      [],
    );
    renderThread();

    expect(await screen.findByText("Third")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Verify" })).toHaveLength(1));
    const turn = screen.getByText("First").closest("article") as HTMLElement;
    expect(within(turn).getByRole("button", { name: "Verify" })).toBeInTheDocument();
  });

  it("posts the edited criteria and shows the new run's chip", async () => {
    stubWebSocket();
    const fetchMock = stubFetch([makeRun({ id: "run-1", prompt: "First" })], [], (url, init) => {
      if (url.includes("/verification/agent-runs/run-1") && init?.method === "POST")
        return jsonResponse(
          { ...verificationRun({ status: "planning" }), evidence: [] },
          true,
          202,
        );
      return undefined;
    });
    renderThread();

    await userEvent.click(await screen.findByRole("button", { name: "Verify" }));
    const command = screen.getByLabelText(/test command/i);
    expect(command).toHaveAttribute("placeholder", expect.stringMatching(/workspace's test command/i));
    await userEvent.type(command, "pnpm test");
    await userEvent.type(screen.getByLabelText(/observe port/i), "5173");
    await userEvent.click(screen.getByRole("button", { name: "Start" }));

    const post = fetchMock.mock.calls.find(
      ([url, init]) => url.includes("/verification/agent-runs/run-1") && init?.method === "POST",
    );
    expect(post).toBeDefined();
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      test_command: "pnpm test",
      observe_port: 5173,
    });
    expect(await screen.findByRole("link", { name: /verification: verifying/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
  });

  it("sends an empty body when nothing is overridden", async () => {
    stubWebSocket();
    const fetchMock = stubFetch([makeRun({ id: "run-1", prompt: "First" })], [], (url, init) => {
      if (url.includes("/verification/agent-runs/run-1") && init?.method === "POST")
        return jsonResponse({ ...verificationRun({ status: "planning" }), evidence: [] }, true, 202);
      return undefined;
    });
    renderThread();

    await userEvent.click(await screen.findByRole("button", { name: "Verify" }));
    await userEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([url]) => url.includes("/verification/agent-runs/"));
      expect(JSON.parse(String(post?.[1]?.body))).toEqual({});
    });
  });

  it("shows the server's detail when verification cannot start", async () => {
    stubWebSocket();
    const detail =
      "This workspace has no test command. Add a custom command with role 'test', or give a test command to run.";
    stubFetch([makeRun({ id: "run-1", prompt: "First" })], [], (url, init) => {
      if (url.includes("/verification/agent-runs/run-1") && init?.method === "POST")
        return jsonResponse({ detail }, false, 422);
      return undefined;
    });
    renderThread();

    await userEvent.click(await screen.findByRole("button", { name: "Verify" }));
    await userEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
    // The form stays open so the user can add a command and retry.
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
  });

  it.each([
    ["planning", "Verifying"],
    ["testing", "Testing"],
    ["fixing", "Fixing"],
    ["observing", "Observing"],
    ["review", "Awaiting review"],
    ["approved", "Approved"],
    ["rejected", "Rejected"],
    ["error", "Error"],
  ] as const)("labels a %s verification as %s", async (status, label) => {
    stubWebSocket();
    stubFetch([makeRun({ id: "run-1", prompt: "First" })], [verificationRun({ status })]);
    renderThread();

    expect(
      await screen.findByRole("link", { name: `Verification: ${label}` }),
    ).toBeInTheDocument();
  });

  it("shows the newest verification of a run and opens its evidence", async () => {
    stubWebSocket();
    stubFetch(
      [makeRun({ id: "run-1", prompt: "First" }), makeRun({ id: "run-2", prompt: "Second" })],
      [
        // Newest first, as the API returns them.
        verificationRun({ id: "vr-new", status: "approved", created_at: "2026-11-28T11:00:00Z" }),
        verificationRun({ id: "vr-old", status: "rejected", created_at: "2026-11-28T10:00:00Z" }),
      ],
    );
    renderThread();

    const chip = await screen.findByRole("link", { name: "Verification: Approved" });
    expect(screen.queryByRole("link", { name: "Verification: Rejected" })).not.toBeInTheDocument();
    // Only the verified run carries a chip.
    const second = screen.getByText("Second").closest("article") as HTMLElement;
    expect(within(second).queryByRole("link", { name: /verification/i })).not.toBeInTheDocument();

    await userEvent.click(chip);
    expect(await screen.findByTestId("location")).toHaveTextContent(
      `/workspaces/${WS}/verify?run=vr-new`,
    );
  });

  it("hides Verify while the run's verification is active, and polls it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stubWebSocket();
    let status: VerificationRunResponse["status"] = "testing";
    const fetchMock = stubFetch([makeRun({ id: "run-1", prompt: "First" })], [], (url) => {
      if (url.includes("/verification/runs")) return jsonResponse([verificationRun({ status })]);
      return undefined;
    });
    renderThread();

    expect(await screen.findByRole("link", { name: "Verification: Testing" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Verify" })).not.toBeInTheDocument();

    status = "review";
    const listCalls = () =>
      fetchMock.mock.calls.filter(([url]) => url.includes("/verification/runs")).length;
    const before = listCalls();
    await vi.advanceTimersByTimeAsync(3_000);
    await waitFor(() => expect(listCalls()).toBeGreaterThan(before));
    expect(
      await screen.findByRole("link", { name: "Verification: Awaiting review" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verify" })).toBeInTheDocument();
  });
});
