/**
 * The editor surface is an embedded iframe, not a launcher. What matters here is that the frame
 * always loads the *prime* URL (the bare alias URL would 404 without a grant), that reloading
 * remounts the frame without touching the sandbox process, and that the folder picker restarts
 * code-server on the chosen worktree.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EditorPanel } from "./EditorPanel";
import type { WorkspaceResponse } from "../lib/api";

const workspace = { id: "w-1", name: "Demo" } as WorkspaceResponse;

const EDITOR_URL = "http://editor-a1b2c3d4e5f6.localhost:8001/?folder=/home/daytona/workspace";

function session(overrides: Record<string, unknown> = {}) {
  return {
    id: "s-1",
    workspace_id: workspace.id,
    user_id: "u-1",
    url: EDITOR_URL,
    prime_url: `${EDITOR_URL}&__enigma_cap=grant-1`,
    status: "active",
    worktree_id: null,
    folder: "/home/daytona/workspace",
    started_at: "2026-05-23T00:00:00Z",
    last_active_at: "2026-05-23T00:00:00Z",
    ...overrides,
  };
}

const worktrees = [
  {
    id: "wt-base",
    branch: "main",
    path: "workspace",
    base_ref: "main",
    is_base: true,
    status: "ready",
    is_clean: true,
    ahead: 0,
    behind: 0,
  },
  {
    id: "wt-feature",
    branch: "feature-x",
    path: "worktrees/feature-x",
    base_ref: "main",
    is_base: false,
    status: "ready",
    is_clean: true,
    ahead: 0,
    behind: 0,
  },
];

function jsonResponse(body: unknown) {
  return { ok: true, status: 200, json: () => Promise.resolve(body) } as Response;
}

let startBodies: string[];

function mockFetch(statusBody: unknown, startSession: (body: string) => unknown) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (url.includes("/editor/status")) {
      return Promise.resolve(jsonResponse(statusBody));
    }
    if (url.includes("/editor/start")) {
      const body = String(init?.body ?? "");
      startBodies.push(body);
      return Promise.resolve(jsonResponse(startSession(body)));
    }
    if (url.includes("/editor/stop")) {
      return Promise.resolve(jsonResponse(session({ status: "stopped" })));
    }
    if (url.includes("/repo/worktrees")) {
      return Promise.resolve(jsonResponse(worktrees));
    }
    return Promise.resolve(jsonResponse({}));
  });
}

beforeEach(() => {
  startBodies = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("EditorPanel", () => {
  it("renders code-server in an iframe pointed at the prime URL", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ status: "active", session: session() }, () => session()),
    );

    render(<EditorPanel workspace={workspace} />);

    const frame = await screen.findByTitle("Demo editor");
    expect(frame).toHaveAttribute("src", `${EDITOR_URL}&__enigma_cap=grant-1`);
    // The bare URL carries no credential and would 404 on its own.
    expect(frame).not.toHaveAttribute("src", EDITOR_URL);
  });

  it("remounts the frame on reload without calling the sandbox", async () => {
    const fetchMock = mockFetch({ status: "active", session: session() }, () => session());
    vi.stubGlobal("fetch", fetchMock);

    render(<EditorPanel workspace={workspace} />);

    const before = await screen.findByTitle("Demo editor");
    const callsBefore = fetchMock.mock.calls.length;

    await userEvent.click(screen.getByRole("button", { name: "Reload frame" }));

    const after = await screen.findByTitle("Demo editor");
    expect(after).not.toBe(before);
    // Reload is purely client-side: no start/stop round trip.
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  it("restarts code-server on the selected worktree", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ status: "active", session: session() }, () =>
        session({
          worktree_id: "wt-feature",
          folder: "/home/daytona/worktrees/feature-x",
          prime_url: `${EDITOR_URL.replace("workspace", "worktrees/feature-x")}&__enigma_cap=g2`,
        }),
      ),
    );

    render(<EditorPanel workspace={workspace} />);

    const picker = await screen.findByLabelText("Folder");
    await userEvent.selectOptions(picker, "wt-feature");

    await waitFor(() => expect(startBodies).toContain(JSON.stringify({ worktree_id: "wt-feature" })));
    await waitFor(() =>
      expect(screen.getByTitle("Demo editor")).toHaveAttribute(
        "src",
        expect.stringContaining("worktrees/feature-x"),
      ),
    );
  });

  it("omits the base clone from the picker options but offers it as a value", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ status: "active", session: session() }, () => session()),
    );

    render(<EditorPanel workspace={workspace} />);

    const picker = (await screen.findByLabelText("Folder")) as HTMLSelectElement;
    const labels = Array.from(picker.options).map((option) => option.text);

    // The base clone is the "Base clone" entry, not a second row named after its branch.
    expect(labels).toEqual(["Base clone", "feature-x"]);
  });

  it("shows a start button instead of a frame when the editor is not running", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch({ status: "inactive", session: null }, () => session()),
    );

    render(<EditorPanel workspace={workspace} />);

    expect(
      await screen.findByText("Editor is not running. Start it to open code-server here."),
    ).toBeInTheDocument();
    expect(screen.queryByTitle("Demo editor")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Start editor" }));

    expect(await screen.findByTitle("Demo editor")).toBeInTheDocument();
  });

  it("keeps working when the worktree list cannot be loaded", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.includes("/editor/status")) {
        return Promise.resolve(jsonResponse({ status: "active", session: session() }));
      }
      if (url.includes("/repo/worktrees")) {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: () => Promise.resolve({ detail: "boom" }),
        } as Response);
      }
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<EditorPanel workspace={workspace} />);

    expect(await screen.findByTitle("Demo editor")).toBeInTheDocument();
    expect(screen.queryByLabelText("Folder")).not.toBeInTheDocument();
  });
});
