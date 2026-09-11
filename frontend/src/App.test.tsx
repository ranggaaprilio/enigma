import { act, render, screen, waitFor, within } from "@testing-library/react";
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

const projectId = "1f0a9b3c-6d21-4a58-9c77-2b8e5d4f1a90";
const teamId = "7c4d2e18-5b93-4f60-8a12-e3d97f5b6c24";

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
  // The shell gates every action on this; "owner" keeps the role-gated controls enabled.
  viewer_role: "owner",
};

// Workspace views live under the project/team hierarchy — there are no top-level `/files`,
// `/terminal`, … routes, and `*` redirects to `/`.
const workspaceBase = `/projects/${projectId}/teams/${teamId}/workspaces/${workspace.id}`;

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

const healthResponse = { status: "ok", database: "ok", redis: "ok" };

// `GET /dashboard/summary`, in the shape `DashboardSummary` declares. The overview route
// renders `summary.workspaces_by_status[...]`, `summary.recent_projects` and
// `summary.recent_workspaces` as soon as `summary` is non-null, so a stand-in that is
// merely truthy is not enough.
const dashboardSummary = {
  projects_total: 0,
  teams_total: 0,
  workspaces_total: 0,
  workspaces_by_status: { creating: 0, ready: 0, busy: 0, error: 0, stopped: 0 },
  recent_projects: [],
  recent_workspaces: [],
};

// `GET /dashboard/usage`, in the shape `UsageSummary` declares (the overview route mounts
// `UsagePanel`).
const usageSummary = {
  scope: null,
  scope_id: null,
  date_from: "2026-05-01T00:00:00Z",
  date_to: "2026-05-31T00:00:00Z",
  totals: {
    run_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    tokens_used: 0,
    estimated_cost: 0,
  },
  series: [],
  breakdown_dimension: "project",
  breakdown: [],
  top_spenders: [],
};

/**
 * Fallback for requests a test does not stub itself.
 *
 * Every authenticated view mounts the app shell, which resolves the workspace from the URL
 * and reads its env vars; the overview route additionally loads the dashboard summary and
 * usage. Those are hit no matter which tab a test drives, so they are answered here in
 * their real shapes and each test only stubs what it actually asserts on. Serving one
 * catch-all health payload for all of them handed the overview route a `summary` with no
 * `recent_projects`, which crashed the tree on render.
 */
function fallbackResponse(url: string) {
  if (url.includes("/dashboard/summary")) {
    return Promise.resolve(jsonResponse(dashboardSummary));
  }
  if (url.includes("/dashboard/usage")) {
    return Promise.resolve(jsonResponse(usageSummary));
  }
  if (url.endsWith(`/workspaces/${workspace.id}`)) {
    return Promise.resolve(jsonResponse(workspace));
  }
  if (url.endsWith(`/workspaces/${workspace.id}/env-vars`)) {
    return Promise.resolve(jsonResponse([]));
  }
  if (url.endsWith("/notifications")) {
    return Promise.resolve(jsonResponse([]));
  }
  // `RepositoryPanel` indexes `data.files` as soon as the request resolves, so the
  // repository view needs a real `RepositoryStatusResponse` even in tests that only pass
  // through it on their way somewhere else.
  if (url.includes("/repo/status")) {
    return Promise.resolve(
      jsonResponse({ cloned: true, branch: "main", is_clean: true, files: [] }),
    );
  }
  if (url.includes("/repo/log")) {
    return Promise.resolve(jsonResponse({ commits: [], has_more: false }));
  }
  if (url.includes("/repo/worktrees") || url.includes("/repo/pull-requests")) {
    return Promise.resolve(jsonResponse([]));
  }
  // Collection endpoints every panel loads on mount. Each of these renders its result
  // immediately (`.map`, `[0]`, `.length`), so an unstubbed one used to crash the view the
  // moment the sidebar navigated to it.
  if (
    url.includes("/agent/runs") ||
    url.includes("/agent/sessions") ||
    url.includes("/audit-logs") ||
    url.includes("/commands") ||
    url.includes("/spec/workflows") ||
    url.includes("/terminal/sessions") ||
    url.includes("/preview/sessions") ||
    url.includes("/comments") ||
    url.includes("/budgets")
  ) {
    return Promise.resolve(jsonResponse([]));
  }
  if (url.includes("/files/tree")) {
    return Promise.resolve(jsonResponse(fileTree));
  }
  if (url.includes("/files/search")) {
    return Promise.resolve(jsonResponse([]));
  }
  if (url.includes("/editor/status")) {
    return Promise.resolve(jsonResponse({ status: "stopped", session: null }));
  }
  if (url.includes("/dev/status") || url.includes("/dev/ensure")) {
    return Promise.resolve(
      jsonResponse({
        status: "idle",
        port: null,
        message: null,
        worktree_id: null,
        branch: null,
      }),
    );
  }
  if (url.includes("/services")) {
    return Promise.resolve(
      jsonResponse({ services: [], environment: { status: "idle", message: null }, limit: 4 }),
    );
  }
  return Promise.resolve(jsonResponse(healthResponse));
}

describe("App auth routing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    MockWebSocket.instances = [];
    if (typeof window.localStorage?.clear === "function") {
      window.localStorage.clear();
    }
    window.history.pushState({}, "", "/");
  });

  it("renders the public landing page at the root when unauthenticated", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not authenticated" }, false, 401)),
    );

    render(<App />);

    expect(
      await screen.findByRole("heading", {
        name: "Give every coding agent a room you can see.",
      }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    expect(screen.getByRole("link", { name: "Run Enigma locally" })).toHaveAttribute(
      "href",
      "#quickstart",
    );
    expect(screen.getByRole("link", { name: "Create an account" })).toHaveAttribute(
      "href",
      "/register",
    );
  });

  it("redirects protected dashboard routes to login when unauthenticated", async () => {
    window.history.pushState({}, "", `${workspaceBase}/terminal`);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not authenticated" }, false, 401)),
    );

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });

  it("opens and closes the landing page mobile navigation", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not authenticated" }, false, 401)),
    );

    render(<App />);

    const menuButton = await screen.findByRole("button", { name: "Open navigation" });
    expect(screen.queryByRole("navigation", { name: "Mobile navigation" })).not.toBeInTheDocument();

    await userEvent.click(menuButton);

    expect(screen.getByRole("navigation", { name: "Mobile navigation" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close navigation" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    await userEvent.click(
      screen.getByRole("navigation", { name: "Mobile navigation" }).querySelector(
        'a[href="#product"]',
      )!,
    );

    expect(screen.queryByRole("navigation", { name: "Mobile navigation" })).not.toBeInTheDocument();
  });

  it("renders the workspace shell for an authenticated user", async () => {
    window.history.pushState({}, "", workspaceBase);
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([workspace]));
        }
        return fallbackResponse(url);
      }),
    );

    render(<App />);

    expect(await screen.findByText("AI Agent Control Room")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
  });

  it("renders the projects-first overview at the root for an authenticated user", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([workspace]));
        }
        return fallbackResponse(url);
      }),
    );

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Dashboard", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("System health")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /View all projects/ })).toHaveAttribute(
      "href",
      "/projects",
    );
  });

  it("uses the sidebar as the only view navigation and updates active URLs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([]));
        }
        return fallbackResponse(url);
      }),
    );
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    const destinations = [
      ["Overview", workspaceBase],
      ["Repository", `${workspaceBase}/repository`],
      ["Files", `${workspaceBase}/files`],
      ["Commands", `${workspaceBase}/commands`],
      ["Spec", `${workspaceBase}/spec`],
      ["Agent", `${workspaceBase}/agents`],
      ["Usage", `${workspaceBase}/usage`],
      ["Editor", `${workspaceBase}/editor`],
      ["Terminal", `${workspaceBase}/terminal`],
      ["Preview", `${workspaceBase}/preview`],
      ["Audit", `${workspaceBase}/audit`],
    ] as const;

    await screen.findByText("Project workspace");
    expect(screen.queryByRole("button", { name: "Overview" })).not.toBeInTheDocument();

    for (const [label, path] of destinations) {
      const link = screen.getByRole("link", { name: label });
      expect(link).toHaveAttribute("href", path);
      await userEvent.click(link);
      await waitFor(() => expect(window.location.pathname).toBe(path));
      expect(link).toHaveAttribute("aria-current", "page");
    }
  });

  it("supports direct deep links and browser back navigation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([]));
        }
        return fallbackResponse(url);
      }),
    );
    window.history.pushState({}, "", `${workspaceBase}/terminal`);

    render(<App />);

    expect(await screen.findByRole("link", { name: "Terminal" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await userEvent.click(screen.getByRole("link", { name: "Repository" }));
    await userEvent.click(screen.getByRole("link", { name: "Files" }));
    expect(window.location.pathname).toBe(`${workspaceBase}/files`);

    act(() => window.history.back());

    await waitFor(() =>
      expect(window.location.pathname).toBe(`${workspaceBase}/repository`),
    );
    expect(screen.getByRole("link", { name: "Repository" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("redirects unknown routes to the overview", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([]));
        }
        return fallbackResponse(url);
      }),
    );
    window.history.pushState({}, "", "/not-a-real-route");

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "Dashboard", level: 1 }),
    ).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/"));
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("keeps the sidebar permanently expanded with the workspace nav and breadcrumbs", async () => {
    window.history.pushState({}, "", workspaceBase);
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        if (url.endsWith("/workspaces")) {
          return Promise.resolve(jsonResponse([workspace]));
        }
        return fallbackResponse(url);
      }),
    );

    render(<App />);

    await screen.findByText("AI Agent Control Room");
    const sidebar = screen.getByRole("complementary");

    // The sidebar is a fixed-width rail — the old hover-to-expand/collapse control (and the
    // `execute-dashboard-*-sidebar-collapsed` flags it persisted) no longer exist.
    expect(sidebar).toHaveClass("lg:w-[240px]");
    expect(
      screen.queryByRole("button", { name: /Collapse .* sidebar/ }),
    ).not.toBeInTheDocument();

    // Above the workspace nav it links back out to the containing project and team.
    expect(within(sidebar).getByRole("link", { name: "Projects" })).toHaveAttribute(
      "href",
      "/projects",
    );
    expect(within(sidebar).getByRole("link", { name: "Overview" })).toHaveAttribute(
      "href",
      workspaceBase,
    );
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

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

    await userEvent.click(screen.getByRole("link", { name: "Audit" }));
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await screen.findByText("AI Agent Control Room");
    await userEvent.click(screen.getByRole("button", { name: "Logout" }));

    await waitFor(() => expect(window.location.pathname).toBe("/login"));
    expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });

  it("renders repository controls and handles diff commit and push flows", async () => {
    let connected = false;
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
        connected = true;
        return Promise.resolve(
          jsonResponse({ ...workspace, repo_url: "https://github.com/example/demo.git" }),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/credential`)) {
        if (!connected) {
          return Promise.resolve(
            jsonResponse({ detail: "No repository credential configured" }, false, 404),
          );
        }
        return Promise.resolve(
          jsonResponse({
            provider: "github",
            auth_type: "personal_access_token",
            username: null,
            public_key: null,
            supports_pull_requests: true,
            repo_url: "https://github.com/example/demo.git",
            created_at: workspace.created_at,
            updated_at: workspace.updated_at,
          }),
        );
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/commit`)) {
        return Promise.resolve(jsonResponse({ ok: true, message: "Changes committed." }));
      }
      if (url.endsWith(`/workspaces/${workspace.id}/repo/push`)) {
        return Promise.resolve(jsonResponse({ ok: true, message: "Changes pushed." }));
      }
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Repository" }));
    expect(await screen.findByRole("button", { name: "Connect repository" })).toBeInTheDocument();
    expect(await screen.findAllByText("README.md")).toHaveLength(2);
    expect(await screen.findByText(/diff --git/)).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Personal access token"), "raw-secret");
    await userEvent.click(screen.getByRole("button", { name: "Connect repository" }));
    expect(await screen.findByText("Repository credentials saved.")).toBeInTheDocument();
    const connectCall = (fetchMock.mock.calls as unknown as [string, RequestInit?][]).find(
      ([url]) => url.endsWith(`/workspaces/${workspace.id}/repo/connect`),
    );
    expect(JSON.parse(String(connectCall?.[1]?.body))).toMatchObject({
      repo_url: "https://github.com/example/demo",
      auth_type: "personal_access_token",
      token: "raw-secret",
    });

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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Files" }));
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

  // Skipped since it was written (5b4d09f) and now also out of date: the one-shot
  // "Prompt" + "Run" composer it drives was replaced by the session-based agent tab
  // (SessionList + SessionThread + PromptComposer). Those flows are covered directly by
  // SessionThread.test.tsx, SessionList.test.tsx and RunHistory.test.tsx; re-enabling this
  // would mean writing a new end-to-end session test rather than repairing this one.
  it.skip("starts an agent run, renders streamed output, and stops it", async () => {
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
      session_id: null,
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await screen.findByText("AI Agent Control Room");
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Commands" }));
    await userEvent.click(await screen.findByRole("button", { name: "New command" }));
    await userEvent.type(screen.getByLabelText("Name"), "Tests");
    await userEvent.type(screen.getByLabelText("Command"), "pnpm test");
    await userEvent.type(screen.getByLabelText("Description"), "Run frontend tests");
    await userEvent.type(screen.getByLabelText("Hotkey"), "T");
    // Renamed in Epic 10: the flag is about intent ("ask me first"), not permission.
    await userEvent.click(screen.getByLabelText(/Ask me first/));
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Terminal" }));

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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Editor" }));
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Editor" }));
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
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Editor" }));
    expect(await screen.findByText("Editor session is unavailable.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restart" }));

    expect(await screen.findByText("code-server is ready")).toBeInTheDocument();
    expect(openSpy).toHaveBeenCalledWith(editorUrl, "_blank", "noopener,noreferrer");
  });

  it("opens the preview tab, shows a running service, and controls the iframe", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const publicUrl = "http://signed-capability.localhost:8001/";
    const serviceId = "6f9d15db-19e2-4c04-95ad-a4e4508ef80d";
    let currentService = {
      id: serviceId,
      workspace_id: workspace.id,
      custom_command_id: "cmd-web",
      name: "Web",
      role: "dev",
      kind: "service",
      command: "pnpm dev",
      port: 5173,
      status: "running",
      desired: true,
      worktree_id: null,
      branch: "main",
      last_error: null,
      exit_code: null,
      started_at: "2026-05-25T00:00:00Z",
      last_seen_at: "2026-05-25T00:00:00Z",
      preview_session_id: "prev-1",
      public_url: publicUrl as string | null,
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
      if (url.endsWith(`/workspaces/${workspace.id}/services/ensure`) && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse({
            services: [currentService],
            environment: { status: "idle", message: null },
            limit: 4,
          }),
        );
      }
      if (
        url.endsWith(`/workspaces/${workspace.id}/services/${serviceId}/stop`) &&
        init?.method === "POST"
      ) {
        currentService = { ...currentService, status: "stopped", public_url: null };
        return Promise.resolve(jsonResponse({ service: currentService, warning: null }));
      }
      return fallbackResponse(url);
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", workspaceBase);

    render(<App />);

    await userEvent.click(await screen.findByRole("link", { name: "Preview" }));

    // The running service auto-opens once the poll finds it.
    const frame = await screen.findByTitle("Web preview");
    expect(frame).toHaveAttribute("src", publicUrl);

    await userEvent.click(screen.getByRole("button", { name: "Mobile" }));
    expect(frame).toHaveClass("max-w-[390px]");

    await userEvent.click(screen.getByRole("button", { name: "Copy URL" }));
    expect(writeText).toHaveBeenCalledWith(publicUrl);

    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByText(/Preview: 0\/1 running/)).toBeInTheDocument();
  });
});
