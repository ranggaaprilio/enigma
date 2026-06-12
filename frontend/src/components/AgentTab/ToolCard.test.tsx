import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { AgentEventResponse, AgentFileChangeResponse } from "../../lib/api";
import { ToolCard, pairToolEvents } from "./ToolCard";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeEvent(
  overrides: Partial<AgentEventResponse> & {
    event_type: AgentEventResponse["event_type"];
  },
): AgentEventResponse {
  return {
    id: overrides.id ?? `evt-${Math.random().toString(36).slice(2, 8)}`,
    agent_run_id: "run-1",
    content: overrides.content ?? "",
    metadata: overrides.metadata ?? {},
    tool_call_id: overrides.tool_call_id ?? null,
    created_at: overrides.created_at ?? new Date().toISOString(),
    ...overrides,
  };
}

function makeFileChange(
  overrides: Partial<AgentFileChangeResponse> = {},
): AgentFileChangeResponse {
  return {
    id: overrides.id ?? `fc-${Math.random().toString(36).slice(2, 8)}`,
    path: overrides.path ?? "src/app.ts",
    status: overrides.status ?? "modified",
    tool_call_id: overrides.tool_call_id ?? null,
    created_at: overrides.created_at ?? new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// pairToolEvents tests
// ---------------------------------------------------------------------------

describe("pairToolEvents", () => {
  it("pairs tool_call and tool_result by tool_call_id", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-1",
      metadata: { tool_name: "read_file" },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-1",
      content: "file contents",
    });

    const paired = pairToolEvents([call, result]);
    expect(paired).toHaveLength(1);
    expect(paired[0].toolCallId).toBe("tc-1");
    expect(paired[0].call).toBe(call);
    expect(paired[0].result).toBe(result);
  });

  it("pairs historical events using metadata tool_call_id", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: null,
      metadata: { tool_name: "read_file", tool_call_id: "legacy-1" },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: null,
      content: "file contents",
      metadata: { tool_call_id: "legacy-1" },
    });

    const paired = pairToolEvents([call, result]);

    expect(paired).toHaveLength(1);
    expect(paired[0].toolCallId).toBe("legacy-1");
    expect(paired[0].call).toBe(call);
    expect(paired[0].result).toBe(result);
  });

  it("handles tool_call without matching tool_result (running)", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-2",
      metadata: { tool_name: "run_command" },
    });

    const paired = pairToolEvents([call]);
    expect(paired).toHaveLength(1);
    expect(paired[0].call).toBe(call);
    expect(paired[0].result).toBeNull();
  });

  it("handles tool_result without matching tool_call (orphan)", () => {
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-3",
      content: "output",
    });

    const paired = pairToolEvents([result]);
    expect(paired).toHaveLength(1);
    expect(paired[0].call).toBeNull();
    expect(paired[0].result).toBe(result);
  });

  it("groups events with null tool_call_id as separate unpaired cards", () => {
    const call1 = makeEvent({
      event_type: "tool_call",
      tool_call_id: null,
      metadata: { tool_name: "search" },
    });
    const result1 = makeEvent({
      event_type: "tool_result",
      tool_call_id: null,
      content: "found 3 files",
    });

    const paired = pairToolEvents([call1, result1]);
    // null tool_call_id → each event gets its own card
    expect(paired).toHaveLength(2);
    expect(paired[0].toolCallId).toBeNull();
    expect(paired[0].call).toBe(call1);
    expect(paired[0].result).toBeNull();
    expect(paired[1].toolCallId).toBeNull();
    expect(paired[1].call).toBeNull();
    expect(paired[1].result).toBe(result1);
  });

  it("preserves insertion order for paired events", () => {
    const call1 = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-a",
      metadata: { tool_name: "first" },
    });
    const call2 = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-b",
      metadata: { tool_name: "second" },
    });
    const result1 = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-a",
      content: "result-a",
    });
    const result2 = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-b",
      content: "result-b",
    });

    const paired = pairToolEvents([call1, call2, result1, result2]);
    expect(paired).toHaveLength(2);
    expect(paired[0].toolCallId).toBe("tc-a");
    expect(paired[1].toolCallId).toBe("tc-b");
  });

  it("skips non-tool events", () => {
    const status = makeEvent({
      event_type: "status",
      content: "thinking",
    });
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-1",
      metadata: { tool_name: "test" },
    });

    const paired = pairToolEvents([status, call]);
    expect(paired).toHaveLength(1);
    expect(paired[0].toolCallId).toBe("tc-1");
  });

  it("handles mixed paired and unpaired events", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-1",
      metadata: { tool_name: "paired_tool" },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-1",
      content: "paired output",
    });
    const unpairedCall = makeEvent({
      event_type: "tool_call",
      tool_call_id: null,
      metadata: { tool_name: "unpaired" },
    });

    const paired = pairToolEvents([call, result, unpairedCall]);
    expect(paired).toHaveLength(2);
    // Paired first (insertion order), then unpaired
    expect(paired[0].toolCallId).toBe("tc-1");
    expect(paired[0].call).toBe(call);
    expect(paired[0].result).toBe(result);
    expect(paired[1].toolCallId).toBeNull();
    expect(paired[1].call).toBe(unpairedCall);
  });
});

// ---------------------------------------------------------------------------
// ToolCard rendering tests
// ---------------------------------------------------------------------------

describe("ToolCard", () => {
  it("renders paired events as a single card with tool name and status", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-1",
      metadata: { tool_name: "read_file" },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-1",
      content: "file contents here",
    });

    render(
      <ToolCard event={{ toolCallId: "tc-1", call, result }} />,
    );

    expect(screen.getByText("read_file")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });

  it("renders running status when no result event", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-2",
      metadata: { tool_name: "run_command" },
    });

    render(
      <ToolCard event={{ toolCallId: "tc-2", call, result: null }} />,
    );

    expect(screen.getByText("run_command")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("renders error status when exit code is non-zero", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-3",
      metadata: { tool_name: "bash" },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-3",
      metadata: { exit_code: 1 },
    });

    render(
      <ToolCard event={{ toolCallId: "tc-3", call, result }} />,
    );

    expect(screen.getByText("Error")).toBeInTheDocument();
  });

  it("expands to show arguments, output, and exit code on click", async () => {
    const user = userEvent.setup();
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-4",
      metadata: { tool_name: "write_file", arguments: '{"path": "/tmp/f.txt"}' },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-4",
      metadata: { exit_code: 0, output: "File written successfully" },
    });

    render(
      <ToolCard event={{ toolCallId: "tc-4", call, result }} />,
    );

    // Not expanded yet — details should not be visible
    expect(screen.queryByText("Arguments")).not.toBeInTheDocument();

    // Click to expand
    await user.click(screen.getByRole("button"));
    expect(screen.getByText("Arguments")).toBeInTheDocument();
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText("Exit code:")).toBeInTheDocument();
    expect(screen.getByText('{"path": "/tmp/f.txt"}')).toBeInTheDocument();
    expect(screen.getByText("File written successfully")).toBeInTheDocument();
  });

  it("shows stream disconnect error separately from tool errors", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-5",
      metadata: { tool_name: "test_tool" },
    });

    render(
      <ToolCard
        event={{ toolCallId: "tc-5", call, result: null }}
        streamError="Connection lost"
      />,
    );

    expect(screen.getByText(/Stream disconnected/)).toBeInTheDocument();
    const errorElements = screen.getAllByText((_content: string, node: Element | null) =>
      node?.textContent?.includes("Connection lost") ?? false,
    );
    expect(errorElements.length).toBeGreaterThanOrEqual(1);
  });

  it("renders file changes when provided", async () => {
    const user = userEvent.setup();
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: "tc-6",
      metadata: { tool_name: "edit_file" },
    });
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: "tc-6",
      metadata: { exit_code: 0 },
    });
    const changes: AgentFileChangeResponse[] = [
      makeFileChange({
        path: "src/index.ts",
        status: "modified",
        tool_call_id: "tc-6",
      }),
      makeFileChange({
        path: "src/new.ts",
        status: "created",
        tool_call_id: "tc-6",
      }),
    ];

    render(
      <ToolCard
        event={{ toolCallId: "tc-6", call, result }}
        fileChanges={changes}
      />,
    );

    // Expand to see file changes
    await user.click(screen.getByRole("button"));
    expect(screen.getByText("Changed files")).toBeInTheDocument();
    expect(screen.getByText("src/index.ts")).toBeInTheDocument();
    expect(screen.getByText("src/new.ts")).toBeInTheDocument();
  });

  it("renders unpaired tool_call with null tool_call_id", () => {
    const call = makeEvent({
      event_type: "tool_call",
      tool_call_id: null,
      metadata: { tool_name: "legacy_tool" },
    });

    render(
      <ToolCard event={{ toolCallId: null, call, result: null }} />,
    );

    expect(screen.getByText("legacy_tool")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("renders unpaired tool_result with null tool_call_id", () => {
    const result = makeEvent({
      event_type: "tool_result",
      tool_call_id: null,
      content: "orphan result",
    });

    render(
      <ToolCard event={{ toolCallId: null, call: null, result }} />,
    );

    // Falls back to content for name when no call
    expect(screen.getByText("orphan result")).toBeInTheDocument();
    expect(screen.getByText("Completed")).toBeInTheDocument();
  });

  it("shows Unknown tool when both call and result are null", () => {
    render(
      <ToolCard event={{ toolCallId: null, call: null, result: null }} />,
    );

    expect(screen.getByText("Unknown tool")).toBeInTheDocument();
  });
});
