import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RepositoryCredentialResponse, WorkspaceResponse, WorktreeResponse } from "../lib/api";
import { WorktreesPanel } from "./WorktreesPanel";

const workspace: WorkspaceResponse = {
  id: "3e15be87-2551-4d70-b972-33fdf68c49eb",
  owner_id: "owner",
  name: "Demo",
  slug: "demo",
  repo_url: "https://gitlab.com/team/demo.git",
  source: "repository",
  template_id: null,
  default_branch: "main",
  runtime: "daytona",
  sandbox_id: "sandbox-1",
  snapshot: null,
  workspace_path: "/workspace",
  app_root: null,
  status: "ready",
  created_at: "2026-09-04T00:00:00Z",
  updated_at: "2026-09-04T00:00:00Z",
  viewer_role: "owner",
};

const gitlabCredential: RepositoryCredentialResponse = {
  provider: "gitlab",
  auth_type: "personal_access_token",
  username: null,
  public_key: null,
  supports_pull_requests: false,
  repo_url: workspace.repo_url,
  created_at: "2026-09-04T00:00:00Z",
  updated_at: "2026-09-04T00:00:00Z",
};

function stubEmptyLists() {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve({ ok: true, status: 200, json: async () => [] } as Response)),
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

const baseWorktree: WorktreeResponse = {
  id: "base-1",
  branch: "main",
  path: "/workspace",
  base_ref: "main",
  is_base: true,
  status: "active",
  is_clean: true,
  ahead: 0,
  behind: 0,
};

const featureWorktree: WorktreeResponse = {
  id: "feature-1",
  branch: "feature-x",
  path: "worktrees/feature-x",
  base_ref: "main",
  is_base: false,
  status: "active",
  is_clean: true,
  ahead: 0,
  behind: 0,
};

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  localStorage.clear();
});

describe("WorktreesPanel provider gating", () => {
  it("hides the pull request form for providers without native PR support", async () => {
    stubEmptyLists();
    render(<WorktreesPanel workspace={workspace} credential={gitlabCredential} />);

    expect(
      await screen.findByText(/Merge requests for GitLab are not supported yet/),
    ).toBeInTheDocument();
    expect(screen.getByText(/open it on gitlab\.com/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open merge request/ })).not.toBeInTheDocument();
    expect(screen.getByText("No merge requests opened from this workspace yet.")).toBeInTheDocument();
  });

  it("keeps the GitHub pull request form when the credential supports it", async () => {
    stubEmptyLists();
    render(
      <WorktreesPanel
        workspace={{ ...workspace, repo_url: "https://github.com/team/demo.git" }}
        credential={{
          ...gitlabCredential,
          provider: "github",
          supports_pull_requests: true,
          repo_url: "https://github.com/team/demo.git",
        }}
      />,
    );

    expect(await screen.findByRole("button", { name: "Open pull request" })).toBeInTheDocument();
    expect(screen.getByText("No pull requests opened from this workspace yet.")).toBeInTheDocument();
  });

  it("falls back to GitHub wording while the credential is unknown", async () => {
    stubEmptyLists();
    render(<WorktreesPanel workspace={{ ...workspace, repo_url: null }} credential={null} />);

    expect(await screen.findByRole("button", { name: "Open pull request" })).toBeInTheDocument();
  });
});

describe("WorktreesPanel worktree switching with running services", () => {
  it("offers to restart services running on the other branch before switching", async () => {
    const runningService = {
      id: "svc-1",
      workspace_id: workspace.id,
      custom_command_id: "cmd-1",
      name: "API",
      role: "custom",
      kind: "service",
      command: "uvicorn app.main:app",
      port: 8000,
      status: "running",
      desired: true,
      worktree_id: null,
      branch: "main",
      last_error: null,
      exit_code: null,
      started_at: "2026-09-04T00:00:00Z",
      last_seen_at: "2026-09-04T00:00:00Z",
      preview_session_id: "prev-1",
      public_url: "http://signed-capability.localhost:8001/",
    };
    const restartCalls: { url: string; body: unknown }[] = [];
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes("/repo/worktrees")) {
        return Promise.resolve(jsonResponse([baseWorktree, featureWorktree]));
      }
      if (url.includes("/repo/pull-requests")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith("/services") && (!init || init.method === undefined)) {
        return Promise.resolve(
          jsonResponse({
            services: [runningService],
            environment: { status: "idle", message: null },
            limit: 4,
          }),
        );
      }
      if (url.includes(`/services/${runningService.id}/restart`) && init?.method === "POST") {
        restartCalls.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
        return Promise.resolve(
          jsonResponse({ service: { ...runningService, worktree_id: featureWorktree.id }, warning: null }),
        );
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WorktreesPanel workspace={workspace} credential={null} />);

    const featureRows = await screen.findAllByText("feature-x");
    await userEvent.click(featureRows[0]);

    expect(
      await screen.findByText(/Services are running elsewhere/),
    ).toBeInTheDocument();
    // Names the branch the service is *actually* running on (main), not the destination
    // (feature-x) being switched to.
    expect(screen.getByText(/running on main/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restart on feature-x" }));

    await waitFor(() => expect(restartCalls).toHaveLength(1));
    expect(restartCalls[0].body).toEqual({ worktree_id: featureWorktree.id });
    expect(
      screen.queryByText(/Services are running elsewhere/),
    ).not.toBeInTheDocument();
  });

  it("switches without restarting when the user chooses to leave services running", async () => {
    const runningService = {
      id: "svc-1",
      workspace_id: workspace.id,
      custom_command_id: "cmd-1",
      name: "API",
      role: "custom",
      kind: "service",
      command: "uvicorn app.main:app",
      port: 8000,
      status: "running",
      desired: true,
      worktree_id: null,
      branch: "main",
      last_error: null,
      exit_code: null,
      started_at: "2026-09-04T00:00:00Z",
      last_seen_at: "2026-09-04T00:00:00Z",
      preview_session_id: "prev-1",
      public_url: "http://signed-capability.localhost:8001/",
    };
    const restartCalls: string[] = [];
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.includes("/repo/worktrees")) {
        return Promise.resolve(jsonResponse([baseWorktree, featureWorktree]));
      }
      if (url.includes("/repo/pull-requests")) {
        return Promise.resolve(jsonResponse([]));
      }
      if (url.endsWith("/services") && (!init || init.method === undefined)) {
        return Promise.resolve(
          jsonResponse({
            services: [runningService],
            environment: { status: "idle", message: null },
            limit: 4,
          }),
        );
      }
      if (url.includes("/restart") && init?.method === "POST") {
        restartCalls.push(url);
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<WorktreesPanel workspace={workspace} credential={null} />);

    const featureRows = await screen.findAllByText("feature-x");
    await userEvent.click(featureRows[0]);

    expect(
      await screen.findByText(/Services are running elsewhere/),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Leave them running" }));

    expect(
      screen.queryByText(/Services are running elsewhere/),
    ).not.toBeInTheDocument();
    expect(restartCalls).toHaveLength(0);
    expect(localStorage.getItem(`enigma.activeWorktree.${workspace.id}`)).toContain("feature-1");
  });
});
