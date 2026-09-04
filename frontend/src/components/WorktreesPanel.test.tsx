import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RepositoryCredentialResponse, WorkspaceResponse } from "../lib/api";
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

afterEach(() => {
  vi.unstubAllGlobals();
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
