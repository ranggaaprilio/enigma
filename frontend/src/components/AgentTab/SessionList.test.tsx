import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AgentSessionResponse } from "../../lib/api";
import { SessionList } from "./SessionList";

function makeSession(overrides: Partial<AgentSessionResponse> = {}): AgentSessionResponse {
  return {
    id: `sess-${Math.random().toString(36).slice(2, 10)}`,
    workspace_id: "ws-1",
    user_id: "user-1",
    title: "Fix the parser",
    status: "active",
    created_at: "2026-06-12T10:00:00Z",
    updated_at: "2026-06-12T10:00:00Z",
    ...overrides,
  };
}

const noop = () => {};

function baseProps() {
  return {
    sessions: [] as AgentSessionResponse[],
    selectedSessionId: null as string | null,
    loading: false,
    error: null as string | null,
    hasMore: false,
    onSelect: vi.fn(),
    onNewSession: vi.fn(),
    onLoadMore: vi.fn(),
    onRename: vi.fn(),
    onDelete: vi.fn(),
  };
}

describe("SessionList", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows an empty state when there are no sessions", () => {
    render(<SessionList {...baseProps()} />);
    expect(screen.getByText(/No sessions yet/i)).toBeInTheDocument();
  });

  it("renders sessions with title and status", () => {
    const sessions = [makeSession({ id: "s1", title: "Alpha", status: "active" })];
    render(<SessionList {...baseProps()} sessions={sessions} />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText(/active/i)).toBeInTheDocument();
  });

  it("fires onNewSession and onSelect", async () => {
    const props = baseProps();
    const sessions = [makeSession({ id: "s1", title: "Alpha" })];
    render(<SessionList {...props} sessions={sessions} />);

    await userEvent.click(screen.getByTestId("new-session"));
    expect(props.onNewSession).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByText("Alpha"));
    expect(props.onSelect).toHaveBeenCalledWith("s1");
  });

  it("supports inline rename", async () => {
    const props = baseProps();
    const sessions = [makeSession({ id: "s1", title: "Alpha" })];
    render(<SessionList {...props} sessions={sessions} />);

    await userEvent.click(screen.getByLabelText("Rename session"));
    const input = screen.getByLabelText("Session title");
    await userEvent.clear(input);
    await userEvent.type(input, "Renamed{Enter}");
    expect(props.onRename).toHaveBeenCalledWith("s1", "Renamed");
  });

  it("confirms before deleting", async () => {
    const props = baseProps();
    const sessions = [makeSession({ id: "s1", title: "Alpha" })];
    render(<SessionList {...props} sessions={sessions} />);

    await userEvent.click(screen.getByLabelText("Delete session"));
    expect(screen.getByText("Delete session")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(props.onDelete).toHaveBeenCalledWith("s1");
  });

  it("shows Load more when there are more sessions", async () => {
    const props = { ...baseProps(), hasMore: true };
    render(<SessionList {...props} sessions={[makeSession()]} />);
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(props.onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("renders an error state", () => {
    render(<SessionList {...baseProps()} error="Boom" />);
    expect(screen.getByText("Boom")).toBeInTheDocument();
  });

  it("does not crash when onNewSession is a noop", () => {
    render(<SessionList {...baseProps()} onNewSession={noop} />);
    expect(screen.getByTestId("new-session")).toBeInTheDocument();
  });
});
