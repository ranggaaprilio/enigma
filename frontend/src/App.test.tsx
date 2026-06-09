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
  runtime: "daytona",
  sandbox_id: "sandbox-demo",
  snapshot: null,
  workspace_path: "/workspace",
  status: "ready",
  created_at: "2026-05-23T00:00:00Z",
  updated_at: "2026-05-23T00:00:00Z",
};

const fileTree = {
  path: "",
  name: "workspace",
  type: "directory",
  size: null,
  modified_at: "2026-05-23T00:00:00Z",
  children: [
    {
      path: "src",
      name: "src",
      type: "directory",
      size: null,
      modified_at: "2026-05-23T00:00:00Z",
      children: [
        {
          path: "src/app.ts",
          name: "app.ts",
          type: "file",
          size: 12,
          modified_at: "2026-05-23T00:00:00Z",
          children: [],
        },
      ],
    },
    {
      path: "README.md",
      name: "README.md",
      type: "file",
      size: 5,
      modified_at: "2026-05-23T00:00:00Z",
      children: [],
    },
  ],
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
    if (typeof window.localStorage?.clear === "function") {
      window.localStorage.clear();
    }
    if (typeof window.localStorage?.removeItem === "function") {
      window.localStorage.removeItem("execute-dashboard-left-sidebar-collapsed");
      window.localStorage.removeItem("execute-dashboard-right-sidebar-collapsed");
    }
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

  it("collapses and expands the sidebars", async () => {
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

    await screen.findByText("AI Agent Control Room");
    await userEvent.click(screen.getByRole("button", { name: "Collapse left sidebar" }));
    await userEvent.click(screen.getByRole("button", { name: "Collapse agent panel" }));

    expect(screen.getByRole("button", { name: "Expand left sidebar" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Expand agent panel" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toBeInTheDocument();
  });

  it("renders notifications and workspace audit logs", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });
    const notification = {
      id: "db59cbaa-c95d-4bd5-8984-16a30e4dce15",
      user_id: authUser.id,
      workspace_id: workspace.id,
      type: "agent_run_completed",
      title: "Agent run completed",
      message: "The agent run completed successfully.",
      read_at: null,
      created_at: "2026-05-25T00:00:00Z",
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
      if (url.endsWith("/notifications") && !init?.method) {
        return Promise.resolve(jsonResponse([notification]));
      }
      if (url.endsWith(`/notifications/${notification.id}/read`)) {
        return Promise.resolve(
          jsonResponse({ ...notification, read_at: "2026-05-25T00:01:00Z" }),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/audit-logs`)) {
        return Promise.resolve(
          jsonResponse([
            {
              id: "c5a17725-eef1-4542-a80a-c8f79eb7983d",
              user_id: authUser.id,
              workspace_id: workspace.id,
              action: "repo_commit",
              target_type: "repository",
              target_id: workspace.id,
              metadata: { message: "Update README" },
              created_at: "2026-05-25T00:02:00Z",
            },
          ]),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([]));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Notifications" }));
    expect(await screen.findByText("Agent run completed")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Agent run completed"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://localhost:8000/notifications/${notification.id}/read`,
        expect.objectContaining({ method: "PATCH" }),
      ),
    );

    await userEvent.click(screen.getByRole("button", { name: "Audit" }));
    expect(await screen.findByText("repo_commit")).toBeInTheDocument();
    expect(screen.getByText("message: Update README")).toBeInTheDocument();
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

  it("renders file management and handles search tree download and delete flows", async () => {
    const openMock = vi.fn();
    Object.defineProperty(window, "open", { value: openMock, writable: true });
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
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/files/tree`)) {
        return Promise.resolve(jsonResponse(fileTree));
      }
      if (url.includes(`/workspaces/${workspace.id}/files/search`)) {
        return Promise.resolve(
          jsonResponse(url.endsWith("q=README") ? [fileTree.children[1]] : []),
        );
      }
      if (
        url.endsWith(`/workspaces/${workspace.id}/files?path=README.md`) &&
        init?.method === "DELETE"
      ) {
        return Promise.resolve(jsonResponse({ ok: true }));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Files" }));
    expect(await screen.findByText("src")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Expand src" }));
    await userEvent.click(await screen.findByText("src/app.ts"));
    expect(screen.getByText("12 B")).toBeInTheDocument();

    const search = screen.getByPlaceholderText("Search files");
    await userEvent.type(search, "README");
    await userEvent.click(await screen.findByText("README.md"));
    await userEvent.click(screen.getByRole("button", { name: "Download" }));
    expect(openMock).toHaveBeenCalledWith(
      `http://localhost:8000/workspaces/${workspace.id}/files/download?path=README.md`,
      "_blank",
      "noopener",
    );

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByText("Confirm delete")).toBeInTheDocument();
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    await userEvent.click(deleteButtons[deleteButtons.length - 1]);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `http://localhost:8000/workspaces/${workspace.id}/files?path=README.md`,
        expect.objectContaining({ method: "DELETE" }),
      ),
    );
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
    await waitFor(() =>
      expect(
        MockWebSocket.instances.some((socket) =>
          socket.url.endsWith(`/workspaces/${workspace.id}/agent/runs/${run.id}/stream`),
        ),
      ).toBe(true),
    );
    const agentSocket = MockWebSocket.instances.find((socket) =>
      socket.url.endsWith(`/workspaces/${workspace.id}/agent/runs/${run.id}/stream`),
    );
    act(() => {
      agentSocket?.emit({
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

  it("creates and runs a confirmed custom command with streamed output", async () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    Object.defineProperty(window, "WebSocket", {
      configurable: true,
      value: MockWebSocket,
      writable: true,
    });
    const command = {
      id: "68b44d9c-4c0a-4770-8c6a-a4a462d0528d",
      workspace_id: workspace.id,
      name: "Tests",
      command: "pnpm test",
      description: "Run frontend tests",
      icon: null,
      hotkey: "T",
      requires_confirmation: true,
      created_at: "2026-05-25T00:00:00Z",
      updated_at: "2026-05-25T00:00:00Z",
    };
    const run = {
      id: "a4b5e02f-fb93-4e0f-acf5-d7a78dc34fee",
      custom_command_id: command.id,
      workspace_id: workspace.id,
      user_id: authUser.id,
      status: "running",
      exit_code: null,
      output: "",
      started_at: "2026-05-25T00:00:00Z",
      completed_at: null,
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
      if (url.endsWith(`/workspaces/${workspace.id}/agent/runs`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/commands`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse(command, true, 201));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/commands`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/commands/${command.id}/runs`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/commands/${command.id}/run`)) {
        return Promise.resolve(jsonResponse(run, true, 201));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Commands" }));
    await userEvent.click(await screen.findByRole("button", { name: "New command" }));
    await userEvent.type(screen.getByLabelText("Name"), "Tests");
    await userEvent.type(screen.getByLabelText("Command"), "pnpm test");
    await userEvent.type(screen.getByLabelText("Description"), "Run frontend tests");
    await userEvent.type(screen.getByLabelText("Hotkey"), "T");
    await userEvent.click(screen.getByLabelText("Requires confirmation"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findAllByText("pnpm test")).not.toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Run command" }));
    expect(screen.getByRole("heading", { name: "Confirm command" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm run" }));

    await waitFor(() =>
      expect(
        MockWebSocket.instances.some((socket) =>
          socket.url.endsWith(`/workspaces/${workspace.id}/command-runs/${run.id}/stream`),
        ),
      ).toBe(true),
    );
    const commandSocket = MockWebSocket.instances.find((socket) =>
      socket.url.endsWith(`/workspaces/${workspace.id}/command-runs/${run.id}/stream`),
    );
    act(() => {
      commandSocket?.emit({ type: "output", content: "ok\n" });
      commandSocket?.emit({ type: "status", status: "completed", exit_code: 0 });
    });

    expect(await screen.findByText("ok")).toBeInTheDocument();
    expect(screen.getAllByText("completed")).not.toHaveLength(0);
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
      pty_session_id: "execute-demo",
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

    await waitFor(() =>
      expect(
        MockWebSocket.instances.some((socket) =>
          socket.url.endsWith(`/workspaces/${workspace.id}/terminal/sessions/${session.id}`),
        ),
      ).toBe(true),
    );
    expect(
      MockWebSocket.instances.some(
        (socket) =>
          socket.url ===
          `ws://localhost:8000/workspaces/${workspace.id}/terminal/sessions/${session.id}`,
      ),
    ).toBe(true);
  });

  it("opens the editor tab and launches code-server in a new browser tab", async () => {
    const editorUrl = "http://localhost:8080/?folder=/workspace/demo";
    const editorSession = {
      id: "0ea59c8a-698c-4ec1-84c7-1c40271760e7",
      workspace_id: workspace.id,
      user_id: authUser.id,
      url: editorUrl,
      status: "active",
      started_at: "2026-05-23T00:00:00Z",
      last_active_at: "2026-05-23T00:00:00Z",
    };
    const openSpy = vi.spyOn(window, "open").mockReturnValue({} as Window);
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
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
      if (url.endsWith(`/workspaces/${workspace.id}/editor/status`)) {
        return Promise.resolve(jsonResponse({ status: "inactive", session: null }));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/editor/start`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse(editorSession));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Editor" }));
    expect(await screen.findByText("Editor is not running.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Open new tab" }));

    expect(await screen.findByText("code-server is ready")).toBeInTheDocument();
    expect(openSpy).toHaveBeenCalledWith(editorUrl, "_blank", "noopener,noreferrer");
  });

  it("shows a fallback code-server link when the popup is blocked", async () => {
    const editorUrl = "http://localhost:8080/?folder=/workspace/demo";
    const editorSession = {
      id: "0ea59c8a-698c-4ec1-84c7-1c40271760e7",
      workspace_id: workspace.id,
      user_id: authUser.id,
      url: editorUrl,
      status: "active",
      started_at: "2026-05-23T00:00:00Z",
      last_active_at: "2026-05-23T00:00:00Z",
    };
    vi.spyOn(window, "open").mockReturnValue(null);
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
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
      if (url.endsWith(`/workspaces/${workspace.id}/editor/status`)) {
        return Promise.resolve(jsonResponse({ status: "inactive", session: null }));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/editor/start`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse(editorSession));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Editor" }));
    await userEvent.click(screen.getByRole("button", { name: "Open new tab" }));

    expect(await screen.findByRole("link", { name: "Continue to code-server" })).toHaveAttribute(
      "href",
      editorUrl,
    );
  });

  it("shows editor failure state and can restart", async () => {
    const editorUrl = "http://localhost:8080/?folder=/workspace/demo";
    const failedSession = {
      id: "0ea59c8a-698c-4ec1-84c7-1c40271760e7",
      workspace_id: workspace.id,
      user_id: authUser.id,
      url: editorUrl,
      status: "error",
      started_at: "2026-05-23T00:00:00Z",
      last_active_at: "2026-05-23T00:00:00Z",
    };
    const activeSession = { ...failedSession, status: "active" };
    const openSpy = vi.spyOn(window, "open").mockReturnValue({} as Window);
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
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
      if (url.endsWith(`/workspaces/${workspace.id}/editor/status`)) {
        return Promise.resolve(jsonResponse({ status: "error", session: failedSession }));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/editor/stop`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ ...failedSession, status: "stopped" }));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/editor/start`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse(activeSession));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Editor" }));
    expect(await screen.findByText("Editor session is unavailable.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restart" }));

    expect(await screen.findByText("code-server is ready")).toBeInTheDocument();
    expect(openSpy).toHaveBeenCalledWith(editorUrl, "_blank", "noopener,noreferrer");
  });

  it("opens the preview tab, detects a preview, and controls the iframe", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const previewSession = {
      id: "6f9d15db-19e2-4c04-95ad-a4e4508ef80d",
      workspace_id: workspace.id,
      port: 5173,
      public_url: "https://3000-sandbox.proxy.daytona.example/?token=signed",
      status: "active",
      created_at: "2026-05-25T00:00:00Z",
      last_seen_at: "2026-05-25T00:00:00Z",
    };
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
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
      if (url.endsWith(`/workspaces/${workspace.id}/preview/sessions`)) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/preview/detect`) && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ status: "active", session: previewSession }));
      }
      if (
        url.endsWith(`/workspaces/${workspace.id}/preview/sessions/${previewSession.id}/refresh`) &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse({ ...previewSession, last_seen_at: "2026-05-25T00:00:01Z" }));
      }
      if (
        url.endsWith(`/workspaces/${workspace.id}/preview/sessions/${previewSession.id}/stop`) &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse({ ...previewSession, status: "stopped" }));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await userEvent.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Preview is not running.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Detect/Open" }));

    const frame = await screen.findByTitle("Demo preview");
    expect(frame).toHaveAttribute(
      "src",
      previewSession.public_url,
    );

    await userEvent.click(screen.getByRole("button", { name: "Mobile" }));
    expect(frame).toHaveClass("max-w-[390px]");

    await userEvent.click(screen.getByRole("button", { name: "Copy URL" }));
    expect(writeText).toHaveBeenCalledWith(previewSession.public_url);

    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByTitle("Demo preview")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Stop preview" }));
    expect(await screen.findByText(/Preview: stopped :5173/)).toBeInTheDocument();
  });
});
