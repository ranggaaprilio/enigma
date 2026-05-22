import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

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
});
