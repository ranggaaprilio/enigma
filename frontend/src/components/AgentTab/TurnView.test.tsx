import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TurnView } from "./TurnView";
import type { AgentEventResponse, AgentRunResponse } from "../../lib/api";

vi.mock("../comments/CommentThread", () => ({ CommentThread: () => null }));
vi.mock("./ChangedFiles", () => ({ ChangedFiles: () => null }));

const INTERRUPTED_TEXT = "Run interrupted by a server restart. Please retry.";

function turn(overrides: Partial<AgentRunResponse> = {}): AgentRunResponse {
  return {
    id: "run-1",
    workspace_id: "ws-1",
    user_id: "u-1",
    session_id: "s-1",
    prompt: "Do the thing",
    status: "error",
    provider: "mock",
    model: "mock-model",
    tokens_used: null,
    input_tokens: null,
    output_tokens: null,
    estimated_cost: null,
    started_at: "2026-10-07T00:00:00Z",
    completed_at: "2026-10-07T00:01:00Z",
    error_message: null,
    error_reason: null,
    ...overrides,
  };
}

function errorEvent(metadata: Record<string, unknown>, content: string): AgentEventResponse {
  return {
    id: "e-1",
    agent_run_id: "run-1",
    event_type: "error",
    content,
    tool_call_id: null,
    metadata,
    created_at: "2026-10-07T00:01:00Z",
  };
}

describe("TurnView interruption", () => {
  it("shows a retry notice instead of an error for a persisted interrupted run", () => {
    render(
      <TurnView
        turn={turn({ error_message: INTERRUPTED_TEXT, error_reason: "interrupted_by_restart" })}
        events={[]}
        isActive={false}
        workspaceId="ws-1"
      />,
    );

    expect(screen.getByTestId("turn-interrupted")).toHaveTextContent(
      "Run interrupted by a server restart — please retry.",
    );
    expect(screen.getByTestId("turn-interrupted")).toHaveAttribute("role", "status");
    expect(screen.queryByTestId("turn-error-message")).not.toBeInTheDocument();
    expect(screen.queryByText(/Agent error/)).not.toBeInTheDocument();
  });

  it("recognises interruption from the live stream's terminal error event", () => {
    render(
      <TurnView
        turn={turn({ status: "executing", completed_at: null })}
        events={[
          errorEvent({ status: "error", reason: "interrupted_by_restart" }, INTERRUPTED_TEXT),
        ]}
        isActive={false}
        workspaceId="ws-1"
        agentError={INTERRUPTED_TEXT}
      />,
    );

    expect(screen.getByTestId("turn-interrupted")).toBeInTheDocument();
    expect(screen.queryByText(/Agent error/)).not.toBeInTheDocument();
  });

  it("still shows ordinary run errors as errors", () => {
    render(
      <TurnView
        turn={turn({ error_message: "provider exploded" })}
        events={[errorEvent({ status: "error" }, "provider exploded")]}
        isActive={false}
        workspaceId="ws-1"
        agentError="provider exploded"
      />,
    );

    expect(screen.queryByTestId("turn-interrupted")).not.toBeInTheDocument();
    expect(screen.getByTestId("turn-error-message")).toHaveTextContent("provider exploded");
    expect(screen.getByText(/Agent error/)).toBeInTheDocument();
  });
});
