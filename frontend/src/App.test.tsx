import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 100;
    rows = 30;

    loadAddon() {
      return undefined;
    }

    open() {
      return undefined;
    }

    focus() {
      return undefined;
    }

    onData() {
      return { dispose: () => undefined };
    }

    write() {
      return undefined;
    }

    getSelection() {
      return "selected text";
    }

    dispose() {
      return undefined;
    }
  },
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {
      return undefined;
    }
  },
}));

import App from "./App";

const authUser = {
  id: "fb6cd934-74f2-404b-bff1-30f964e9326c",
  email: "owner@example.com",
  name: "Owner",
  role: "owner",
  is_active: true,
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-22T00:00:00Z",
};

const workspace = {
  id: "3e15be87-2551-4d70-b972-33fdf68c49eb",
  owner_id: authUser.id,
  name: "Demo",
  slug: "demo",
  repo_url: "https://github.com/example/demo",
  default_branch: "main",
  local_path: "/tmp/demo",
  status: "ready",
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};

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

describe("App auth routing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    MockWebSocket.instances = [];
    window.localStorage?.clear();
    window.history.pushState({}, "", "/");
  });

  it("redirects protected dashboard routes to login when unauthenticated", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not authenticated" }, false, 401)),
    );

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });

  it("renders the dashboard for an authenticated user", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([]));
        }
        return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
      }),
    );

    render(<App />);

    expect(await screen.findByText("AI Agent Control Room")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
  });

  it("logs out and returns to the login route", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(jsonResponse(authUser));
      }
      if (url.endsWith("/auth/logout")) {
        return Promise.resolve(jsonResponse({ ok: true }));
      }
      if (url.endsWith("/workspaces")) {
        return Promise.resolve(jsonResponse([]));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await screen.findByText("AI Agent Control Room");
    await userEvent.click(screen.getByRole("button", { name: "Logout" }));

    await waitFor(() => expect(window.location.pathname).toBe("/login"));
    expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });

  it("renders repository controls and handles diff commit and push flows", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(jsonResponse(authUser));
      }
      if (url.endsWith("/workspaces")) {
        return Promise.resolve(jsonResponse([workspace]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/env-vars`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/status`)) {
        return Promise.resolve(
          jsonResponse({
            cloned: true,
            branch: "main",
            is_clean: false,
            files: [{ path: "README.md", status: "M" }],
          }),
        );
      }
      if (url.includes(`/workspaces/${workspace.id}/repo/diff`)) {
        return Promise.resolve(
          jsonResponse({ diff: "diff --git a/README.md b/README.md\n+hello" }),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/connect`)) {
        return Promise.resolve(
          jsonResponse({ ...workspace, repo_url: "https://github.com/example/demo.git" }),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/commit`)) {
        return Promise.resolve(jsonResponse({ ok: true, message: "Changes committed." }));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/push`)) {
        return Promise.resolve(jsonResponse({ ok: true, message: "Changes pushed." }));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Repository" }));
    expect(await screen.findByRole("button", { name: "Connect repository" })).toBeInTheDocument();
    expect(await screen.findAllByText("README.md")).toHaveLength(2);
    expect(await screen.findByText(/diff --git/)).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Personal access token"), "raw-secret");
    await userEvent.click(screen.getByRole("button", { name: "Connect repository" }));
    expect(await screen.findByText("Repository credentials saved.")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Commit message"), "Update README");
    await userEvent.click(screen.getByRole("button", { name: "Commit changes" }));
    expect(await screen.findByText("Changes committed.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Push" }));
    expect(await screen.findByText("Changes pushed.")).toBeInTheDocument();
  });

  it("starts an agent run, renders streamed output, and stops it", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });
    const run = {
      id: "68b44d9c-4c0a-4770-8c6a-a4a462d0528d",
      workspace_id: workspace.id,
      user_id: authUser.id,
      prompt: "Inspect files",
      status: "thinking",
      provider: "mock",
      model: "mock-agent",
      tokens_used: null,
      started_at: "2026-05-23T00:00:00Z",
      completed_at: null,
      error_message: null,
    };
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(jsonResponse(authUser));
      }
      if (url.endsWith("/workspaces")) {
        return Promise.resolve(jsonResponse([workspace]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/env-vars`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/status`)) {
        return Promise.resolve(
          jsonResponse({ cloned: true, branch: "main", is_clean: true, files: [] }),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse(run));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([run]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs/${run.id}/stop`)) {
        return Promise.resolve(jsonResponse({ ...run, status: "stopped" }));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await screen.findByText("Agent Panel");
    await userEvent.type(screen.getByLabelText("Prompt"), "Inspect files");
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://localhost:8000/workspaces/${workspace.id}/agent/runs`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    act(() => {
      MockWebSocket.instances[0].emit({
        id: "9f835690-6ac1-4bbf-a53f-68cde51f21e5",
        agent_run_id: run.id,
        event_type: "output_text",
        content: "Hello from agent",
        metadata: {},
        created_at: "2026-05-23T00:00:01Z",
      });
    });

    expect(await screen.findByText("Hello from agent")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findAllByText("stopped")).not.toHaveLength(0);
  });

  it("opens the terminal tab and connects a websocket session", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });
    Object.defineProperty(window, "ResizeObserver", {
      configurable: true,
      value: class {
        observe() {
          return undefined;
        }
        disconnect() {
          return undefined;
        }
      },
      writable: true,
    });
    const session = {
      id: "a4b5e02f-fb93-4e0f-acf5-d7a78dc34fee",
      workspace_id: workspace.id,
      user_id: authUser.id,
      name: "Terminal",
      tmux_session_name: "execute-demo",
      status: "active",
      created_at: "2026-05-23T00:00:00Z",
      last_active_at: "2026-05-23T00:00:00Z",
    };
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(jsonResponse(authUser));
      }
      if (url.endsWith("/workspaces")) {
        return Promise.resolve(jsonResponse([workspace]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/env-vars`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/terminal/sessions`)) {
        return Promise.resolve(jsonResponse(session));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Terminal" }));

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1));
    expect(MockWebSocket.instances[0].url).toBe(
      `ws://localhost:8000/workspaces/${workspace.id}/terminal/sessions/${session.id}`,
    );
  });
});
