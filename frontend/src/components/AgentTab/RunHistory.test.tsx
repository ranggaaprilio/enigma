import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentRunResponse } from "../../lib/api";
import { RunHistory } from "./RunHistory";

function makeRun(overrides: Partial<AgentRunResponse> = {}): AgentRunResponse {
  return {
    id: `run-${Math.random().toString(36).slice(2, 10)}`,
    workspace_id: "ws-1",
    user_id: "user-1",
    prompt: "Fix the login bug",
    status: "completed",
    provider: "mock",
    model: "gpt-4",
    tokens_used: 1500,
    started_at: "2026-06-12T10:00:00Z",
    completed_at: "2026-06-12T10:01:30Z",
    error_message: null,
    ...overrides,
  };
}

function makeRuns(count: number, startId = 0): AgentRunResponse[] {
  return Array.from({ length: count }, (_, i) =>
    makeRun({
      id: `run-${startId + i}`,
      prompt: `Prompt ${startId + i}`,
    }),
  );
}

function jsonResponse(data: unknown) {
  return {
    ok: true,
    json: async () => data,
  };
}

describe("RunHistory", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders 20 runs from the initial fetch", async () => {
    const runs = makeRuns(20);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(runs)),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(20);
    });
  });

  it('shows "Load more" button when there are more runs', async () => {
    const runs = makeRuns(20);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(runs)),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    expect(await screen.findByRole("button", { name: "Load more" })).toBeInTheDocument();
  });

  it('hides "Load more" when fewer than 20 runs are returned', async () => {
    const runs = makeRuns(10);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(runs)),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(10);
    });

    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("loads next page when Load more is clicked and appends to list", async () => {
    const firstPage = makeRuns(20, 0);
    const secondPage = makeRuns(20, 20);
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(firstPage));

    vi.stubGlobal("fetch", fetchMock);

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(20);
    });

    fetchMock.mockResolvedValue(jsonResponse(secondPage));

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    await waitFor(() => {
      expect(screen.getAllByRole("listitem")).toHaveLength(40);
    });
  });

  it("calls onSelectRun when a run is clicked", async () => {
    const run = makeRun({ id: "run-1", prompt: "Fix the bug" });
    const onSelectRun = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([run])),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={onSelectRun} />,
    );

    await waitFor(() => {
      expect(screen.getByText("Fix the bug")).toBeInTheDocument();
    });

    await userEvent.click(screen.getByText("Fix the bug"));

    expect(onSelectRun).toHaveBeenCalledWith(
      expect.objectContaining({ id: "run-1" }),
    );
  });

  it("highlights the selected run", async () => {
    const runs = [makeRun({ id: "run-1", prompt: "First" }), makeRun({ id: "run-2", prompt: "Second" })];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(runs)),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} selectedRunId="run-1" />,
    );

    await waitFor(() => {
      expect(screen.getByText("First")).toBeInTheDocument();
    });

    const firstButton = screen.getByText("First").closest("button");
    expect(firstButton).toHaveAttribute("aria-current", "true");

    const secondButton = screen.getByText("Second").closest("button");
    expect(secondButton).not.toHaveAttribute("aria-current");
  });

  it("shows status badges with correct labels", async () => {
    const runs = [
      makeRun({ id: "r1", status: "completed", prompt: "Done" }),
      makeRun({ id: "r2", status: "error", prompt: "Failed" }),
      makeRun({ id: "r3", status: "thinking", prompt: "Think task" }),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(runs)),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    expect(await screen.findByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("Thinking")).toBeInTheDocument();
  });

  it("shows model and token count in monospace", async () => {
    const run = makeRun({ id: "r1", model: "gpt-4", tokens_used: 1500, prompt: "Test" });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([run])),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    await waitFor(() => {
      expect(screen.getByText("gpt-4")).toBeInTheDocument();
    });
    expect(screen.getByText("1.5k")).toBeInTheDocument();
  });

  it("shows empty state when no runs exist", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([])),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    expect(await screen.findByText(/No agent runs yet/)).toBeInTheDocument();
  });

  it("shows error state when fetch fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({ detail: "Internal error" }),
      }),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    expect(await screen.findByText("Internal error")).toBeInTheDocument();
  });

  it("truncates long prompts", async () => {
    const longPrompt = "A".repeat(200);
    const run = makeRun({ id: "r1", prompt: longPrompt });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([run])),
    );

    render(
      <RunHistory workspaceId="ws-1" onSelectRun={vi.fn()} />,
    );

    const element = await screen.findByText(longPrompt);
    expect(element).toHaveClass("truncate");
  });
});