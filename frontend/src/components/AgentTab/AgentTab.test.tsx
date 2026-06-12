import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentTab } from "./index";
import { ChangedFiles } from "./ChangedFiles";
import type { AgentFileChangeListResponse } from "../../lib/api";

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
    return undefined;
  }

  send() {
    return undefined;
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => payload,
  };
}

const workspace = {
  id: "3e15be87-2551-4d70-b972-33fdf68c49eb",
  owner_id: "user-1",
  name: "Demo",
  slug: "demo",
  repo_url: "https://github.com/example/demo",
  default_branch: "main",
  runtime: "daytona",
  sandbox_id: "sandbox-demo",
  snapshot: null,
  workspace_path: "/workspace",
  status: "ready",
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};

const run = {
  id: "68b44d9c-4c0a-4770-8c6a-a4a462d0528d",
  workspace_id: workspace.id,
  user_id: "user-1",
  prompt: "Inspect files",
  status: "thinking",
  provider: "mock",
  model: "mock-agent",
  tokens_used: null,
  started_at: "2026-05-23T00:00:00Z",
  completed_at: null,
  error_message: null,
};

describe("AgentTab WebSocket reconnection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    MockWebSocket.instances = [];
  });

  it("WebSocket reconnects on close, max 3 retries, shows disconnect status", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (
        url.includes(`/workspaces/${workspace.id}/agent/runs`) &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(run));
      }
      if (
        url.includes(`/workspaces/${workspace.id}/agent/runs/${run.id}`)
      ) {
        return Promise.resolve(jsonResponse({ status: "ok" }));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([run]));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={workspace.id} />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/workspaces/${workspace.id}/agent/runs`),
        expect.anything(),
      );
    });

    const promptInput = screen.getByPlaceholderText(
      "Describe what you want the agent to do…",
    );
    await userEvent.type(promptInput, "Inspect files");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(1);
    });

    const socket1 = MockWebSocket.instances[0];
    expect(socket1.url).toContain(
      `/workspaces/${workspace.id}/agent/runs/${run.id}/stream`,
    );

    act(() => {
      socket1.onopen?.();
    });

    act(() => {
      socket1.close();
    });

    await waitFor(() => {
      expect(screen.getByTestId("reconnecting-status")).toBeInTheDocument();
    });

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(2);
    });

    act(() => {
      MockWebSocket.instances[1].close();
    });

    act(() => {
      vi.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(3);
    });

    act(() => {
      MockWebSocket.instances[2].close();
    });

    act(() => {
      vi.advanceTimersByTime(4000);
    });

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(4);
    });

    act(() => {
      MockWebSocket.instances[3].close();
    });

    await waitFor(() => {
      expect(screen.getByTestId("disconnected-status")).toBeInTheDocument();
    });

    expect(MockWebSocket.instances.length).toBe(4);
  });

  it("refreshes from REST API when reconnecting after a completed run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const completedRun = { ...run, status: "completed" as const };
    const detail = {
      ...completedRun,
      tokens_used: 42,
      completed_at: "2026-05-23T00:01:00Z",
      error_message: null,
      events: [
        {
          id: "evt-1",
          agent_run_id: run.id,
          event_type: "tool_call",
          content: "read_file",
          metadata: { tool_name: "read_file" },
          tool_call_id: "tc-1",
          created_at: "2026-05-23T00:00:01Z",
        },
      ],
    };

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (
        url.includes(`/workspaces/${workspace.id}/agent/runs`) &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(completedRun));
      }
      if (
        url.includes(`/workspaces/${workspace.id}/agent/runs/${run.id}`) &&
        !url.includes("/stop")
      ) {
        return Promise.resolve(jsonResponse(detail));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([completedRun]));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={workspace.id} />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/workspaces/${workspace.id}/agent/runs`),
        expect.anything(),
      );
    });

    const promptInput = screen.getByPlaceholderText(
      "Describe what you want the agent to do…",
    );
    await userEvent.type(promptInput, "Inspect files");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(1);
    });

    act(() => {
      MockWebSocket.instances[0].onopen?.();
    });

    act(() => {
      MockWebSocket.instances[0].emit({
        id: "evt-status",
        agent_run_id: run.id,
        event_type: "status",
        content: "completed",
        metadata: {},
        tool_call_id: null,
        created_at: "2026-05-23T00:00:01Z",
      });
    });

    await waitFor(() => {
      expect(screen.getByText("completed")).toBeInTheDocument();
    });

    act(() => {
      MockWebSocket.instances[0].close();
    });

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(
          `/workspaces/${workspace.id}/agent/runs/${run.id}`,
        ),
        expect.anything(),
      );
    });

    await waitFor(() => {
      expect(screen.getByText("read_file")).toBeInTheDocument();
    });

    expect(screen.queryByTestId("run-error-message")).not.toBeInTheDocument();
  });

  it("shows error_message from API when reconnecting after an error run", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const errorRun = { ...run, status: "error" as const };
    const detail = {
      ...errorRun,
      tokens_used: 7,
      completed_at: "2026-05-23T00:00:30Z",
      error_message: "LLM provider returned 500",
      events: [
        {
          id: "evt-1",
          agent_run_id: run.id,
          event_type: "tool_call",
          content: "read_file",
          metadata: { tool_name: "read_file" },
          tool_call_id: "tc-1",
          created_at: "2026-05-23T00:00:01Z",
        },
      ],
    };

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (
        url.includes(`/workspaces/${workspace.id}/agent/runs`) &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(errorRun));
      }
      if (
        url.includes(`/workspaces/${workspace.id}/agent/runs/${run.id}`) &&
        !url.includes("/stop")
      ) {
        return Promise.resolve(jsonResponse(detail));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([errorRun]));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={workspace.id} />);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/workspaces/${workspace.id}/agent/runs`),
        expect.anything(),
      );
    });

    const promptInput = screen.getByPlaceholderText(
      "Describe what you want the agent to do…",
    );
    await userEvent.type(promptInput, "Inspect files");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => {
      expect(MockWebSocket.instances.length).toBe(1);
    });

    act(() => {
      MockWebSocket.instances[0].onopen?.();
    });

    act(() => {
      MockWebSocket.instances[0].emit({
        id: "evt-status",
        agent_run_id: run.id,
        event_type: "status",
        content: "error",
        metadata: {},
        tool_call_id: null,
        created_at: "2026-05-23T00:00:01Z",
      });
    });

    await waitFor(() => {
      expect(screen.getByText("error")).toBeInTheDocument();
    });

    act(() => {
      MockWebSocket.instances[0].close();
    });

    act(() => {
      vi.advanceTimersByTime(1000);
    });

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(
          `/workspaces/${workspace.id}/agent/runs/${run.id}`,
        ),
        expect.anything(),
      );
    });

    await waitFor(() => {
      expect(
        screen.getByText("Run error: LLM provider returned 500"),
      ).toBeInTheDocument();
    });

    expect(screen.getByTestId("run-error-message")).toBeInTheDocument();
  });
});

describe("AgentTab integration: run lifecycle, tool cards, file changes", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
    MockWebSocket.instances = [];
  });

  it("selects a completed run from history and loads events and file changes", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const pastRun = {
      ...run,
      id: "past-run-001",
      status: "completed",
      prompt: "Past run prompt",
      tokens_used: 300,
      completed_at: "2026-05-22T00:01:00Z",
    };

    const pastDetail = {
      ...pastRun,
      events: [
        {
          id: "evt-past-tc",
          agent_run_id: pastRun.id,
          event_type: "tool_call",
          content: "read_file",
          metadata: { tool_name: "read_file" },
          tool_call_id: "call-past-1",
          created_at: "2026-05-22T00:00:01Z",
        },
        {
          id: "evt-past-tr",
          agent_run_id: pastRun.id,
          event_type: "tool_result",
          content: "file contents",
          metadata: { exit_code: 0 },
          tool_call_id: "call-past-1",
          created_at: "2026-05-22T00:00:02Z",
        },
      ],
    };

    const pastFileChanges: AgentFileChangeListResponse = {
      items: [
        { id: "fc-p1", path: "src/main.py", status: "modified", tool_call_id: "call-past-1", created_at: "2026-05-22T00:00:02Z" },
      ],
      total: 1,
      capped: false,
    };

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes(`/workspaces/${workspace.id}/agent/runs/${pastRun.id}`) && !url.includes("/stop") && !url.includes("/file-changes")) {
        return Promise.resolve(jsonResponse(pastDetail));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs/${pastRun.id}/file-changes`)) {
        return Promise.resolve(jsonResponse(pastFileChanges));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs`) && !init?.method) {
        return Promise.resolve(jsonResponse([pastRun]));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <>
        <AgentTab workspaceId={workspace.id} />
        <ChangedFiles workspaceId={workspace.id} runId={pastRun.id} />
      </>,
    );

    expect(await screen.findByText("Past run prompt")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Past run prompt"));

    expect(await screen.findByText("read_file")).toBeInTheDocument();
    expect(screen.getAllByText("Completed").length).toBeGreaterThanOrEqual(1);

    await waitFor(() => {
      expect(screen.getByText("src/main.py")).toBeInTheDocument();
    });
    expect(screen.getByText("modified")).toBeInTheDocument();
  });

  it("renders empty state when no workspace is provided", () => {
    render(<AgentTab workspaceId={null} />);

    expect(screen.getByText("No workspace selected")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Describe what you want the agent to do…")).not.toBeInTheDocument();
  });

  it("shows no tool events for a run with only output_text", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const completedRun = { ...run, status: "completed" as const, prompt: "Just thinking" };
    const detail = {
      ...completedRun,
      tokens_used: 50,
      completed_at: "2026-05-23T00:01:00Z",
      error_message: null,
      events: [
        {
          id: "evt-out1",
          agent_run_id: run.id,
          event_type: "output_text",
          content: "I am thinking",
          metadata: {},
          tool_call_id: null,
          created_at: "2026-05-23T00:00:01Z",
        },
        {
          id: "evt-out2",
          agent_run_id: run.id,
          event_type: "output_text",
          content: " about this problem.",
          metadata: {},
          tool_call_id: null,
          created_at: "2026-05-23T00:00:02Z",
        },
      ],
    };

    const fileChanges: AgentFileChangeListResponse = {
      items: [],
      total: 0,
      capped: false,
    };

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes(`/workspaces/${workspace.id}/agent/runs/${run.id}`) && !url.includes("/stop") && !url.includes("/file-changes")) {
        return Promise.resolve(jsonResponse(detail));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs/${run.id}/file-changes`)) {
        return Promise.resolve(jsonResponse(fileChanges));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs`) && !init?.method) {
        return Promise.resolve(jsonResponse([completedRun]));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <>
        <AgentTab workspaceId={workspace.id} />
        <ChangedFiles workspaceId={workspace.id} runId={run.id} />
      </>,
    );

    expect(await screen.findByText("Just thinking")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Just thinking"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/workspaces/${workspace.id}/agent/runs/${run.id}`),
        expect.anything(),
      );
    });

    await waitFor(() => {
      expect(screen.getByText("No tool events for this run yet.")).toBeInTheDocument();
    });
    expect(screen.getByText("I am thinking about this problem.")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText("No file changes")).toBeInTheDocument();
    });
  });

  it("shows error run with partial file changes and tool_call_id pairing", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const errorRun = {
      ...run,
      id: "error-run-001",
      status: "error",
      prompt: "Fix the bug",
      error_message: "LLM provider returned 500",
      completed_at: "2026-05-23T00:00:30Z",
    };

    const errorDetail = {
      ...errorRun,
      tokens_used: 200,
      events: [
        {
          id: "evt-tc1",
          agent_run_id: errorRun.id,
          event_type: "tool_call",
          content: "write_file",
          metadata: { tool_name: "write_file" },
          tool_call_id: "call-err-1",
          created_at: "2026-05-23T00:00:01Z",
        },
        {
          id: "evt-tr1",
          agent_run_id: errorRun.id,
          event_type: "tool_result",
          content: "File written",
          metadata: { exit_code: 0 },
          tool_call_id: "call-err-1",
          created_at: "2026-05-23T00:00:02Z",
        },
      ],
    };

    const errorFileChanges: AgentFileChangeListResponse = {
      items: [
        { id: "fc-err-1", path: "src/bugfix.py", status: "created", tool_call_id: "call-err-1", created_at: "2026-05-23T00:00:02Z" },
        { id: "fc-err-2", path: "src/config.py", status: "modified", tool_call_id: "call-err-1", created_at: "2026-05-23T00:00:02Z" },
      ],
      total: 2,
      capped: false,
    };

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes(`/workspaces/${workspace.id}/agent/runs/${errorRun.id}`) && !url.includes("/stop") && !url.includes("/file-changes")) {
        return Promise.resolve(jsonResponse(errorDetail));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs/${errorRun.id}/file-changes`)) {
        return Promise.resolve(jsonResponse(errorFileChanges));
      }
      if (url.includes(`/workspaces/${workspace.id}/agent/runs`) && !init?.method) {
        return Promise.resolve(jsonResponse([errorRun]));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <>
        <AgentTab workspaceId={workspace.id} />
        <ChangedFiles workspaceId={workspace.id} runId={errorRun.id} />
      </>,
    );

    expect(await screen.findByText("Fix the bug")).toBeInTheDocument();

    await userEvent.click(screen.getByText("Fix the bug"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining(`/workspaces/${workspace.id}/agent/runs/${errorRun.id}`),
        expect.anything(),
      );
    });

    await waitFor(() => {
      expect(screen.getByText("write_file")).toBeInTheDocument();
    });

    expect(screen.getAllByText("Completed").length).toBeGreaterThanOrEqual(1);

    await waitFor(() => {
      expect(screen.getByText("src/bugfix.py")).toBeInTheDocument();
    });
    expect(screen.getByText("src/config.py")).toBeInTheDocument();
    expect(screen.getByText("created")).toBeInTheDocument();
    expect(screen.getByText("modified")).toBeInTheDocument();

    expect(screen.getByTestId("run-error-message")).toBeInTheDocument();
  });

  it("loads more runs via pagination when clicking Load more", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });

    const runs = Array.from({ length: 20 }, (_, i) => ({
      ...run,
      id: `run-page1-${i}`,
      prompt: `Run ${i}`,
      status: "completed" as const,
      tokens_used: 100 + i,
      completed_at: "2026-05-23T00:01:00Z",
    }));

    const moreRuns = Array.from({ length: 5 }, (_, i) => ({
      ...run,
      id: `run-page2-${i}`,
      prompt: `More run ${i}`,
      status: "completed" as const,
      tokens_used: 200 + i,
      completed_at: "2026-05-23T00:02:00Z",
    }));

    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes("/agent/runs") && !init?.method) {
        if (url.includes("offset=20")) {
          return Promise.resolve(jsonResponse(moreRuns));
        }
        return Promise.resolve(jsonResponse(runs));
      }
      return Promise.resolve(jsonResponse({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentTab workspaceId={workspace.id} />);

    await waitFor(() => {
      expect(screen.getByText("Run 0")).toBeInTheDocument();
    });

    expect(screen.getAllByRole("listitem")).toHaveLength(20);

    const loadMoreButton = await screen.findByRole("button", { name: "Load more" });
    expect(loadMoreButton).toBeInTheDocument();

    fetchMock.mockClear();

    await userEvent.click(loadMoreButton);

    await waitFor(() => {
      expect(screen.getByText("More run 0")).toBeInTheDocument();
    });

    expect(screen.getAllByRole("listitem").length).toBe(25);
  });
});
