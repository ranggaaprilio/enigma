import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceResponse } from "../../lib/api";
import { CreateWorkspaceWizard } from "./CreateWorkspaceWizard";

const created: WorkspaceResponse = {
  id: "3e15be87-2551-4d70-b972-33fdf68c49eb",
  owner_id: "owner",
  name: "Demo",
  slug: "demo",
  repo_url: null,
  source: "blank",
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

const template = {
  id: "00000000-0000-4000-8000-000000000001",
  slug: "react-vite",
  name: "React",
  description: "A React app.",
  source_repo_url: "https://github.com/github/codespaces-react.git",
  default_branch: "develop",
  icon: null,
  tags: ["react"],
  preview_port: 3000,
  sort_order: 10,
  created_at: "2026-09-04T00:00:00Z",
  updated_at: "2026-09-04T00:00:00Z",
};

function jsonResponse(data: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => data } as Response;
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) =>
    Promise.resolve(handler(url, init) ?? jsonResponse({ detail: "unexpected" }, false, 500)),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  const init = call[1] as RequestInit | undefined;
  return init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CreateWorkspaceWizard", () => {
  it("creates a blank workspace without sending repo_url", async () => {
    stubFetch(() => undefined);
    const createFn = vi.fn().mockResolvedValue(created);
    const onCreated = vi.fn();
    render(
      <CreateWorkspaceWizard open onClose={vi.fn()} onCreated={onCreated} createFn={createFn} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("Choose how to start the workspace.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: /Blank workspace/ }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.type(screen.getByLabelText("Workspace name"), "Scratch");
    await userEvent.clear(screen.getByLabelText("Default branch"));
    await userEvent.type(screen.getByLabelText("Default branch"), "trunk");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));

    // Blank has three steps, so the third pill is Review.
    expect(screen.getByRole("button", { name: /3\s*Review/ })).toHaveAttribute(
      "aria-current",
      "step",
    );
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
    const payload = createFn.mock.calls[0][0];
    expect(payload).toEqual({ name: "Scratch", default_branch: "trunk", source: "blank" });
    expect(payload).not.toHaveProperty("repo_url");
  });

  it("selecting a template prefills the default branch and sends template_id", async () => {
    stubFetch((url) => (url.endsWith("/workspace-templates") ? jsonResponse([template]) : undefined));
    const createFn = vi.fn().mockResolvedValue({ ...created, source: "template" });
    const onCreated = vi.fn();
    render(
      <CreateWorkspaceWizard open onClose={vi.fn()} onCreated={onCreated} createFn={createFn} />,
    );

    await userEvent.click(screen.getByRole("radio", { name: /Start from a template/ }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(await screen.findByRole("radio", { name: /React/ }));
    expect(screen.getByLabelText("Workspace name")).toHaveValue("React");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("develop")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(createFn.mock.calls[0][0]).toEqual({
      name: "React",
      default_branch: "develop",
      source: "template",
      template_id: template.id,
    });
  });

  it("connects and clones an existing repository with a token after creating", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/repo/connect")) return jsonResponse({ ...created, source: "repository" });
      if (url.endsWith("/repo/credential")) {
        return jsonResponse({
          provider: "gitlab",
          auth_type: "personal_access_token",
          username: null,
          public_key: null,
          supports_pull_requests: false,
          repo_url: "https://gitlab.com/team/demo.git",
          created_at: "",
          updated_at: "",
        });
      }
      if (url.endsWith("/repo/clone")) return jsonResponse({ ok: true, message: "Cloned." });
      if (url.endsWith("/dev-server/setup")) return jsonResponse({ ok: true });
      return undefined;
    });
    const createFn = vi.fn().mockResolvedValue({ ...created, source: "repository" });
    const onCreated = vi.fn();
    render(
      <CreateWorkspaceWizard open onClose={vi.fn()} onCreated={onCreated} createFn={createFn} />,
    );

    await userEvent.click(screen.getByRole("radio", { name: /Connect an existing repository/ }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.type(screen.getByLabelText("Workspace name"), "Demo");
    await userEvent.type(screen.getByLabelText("Repository URL"), "https://gitlab.com/team/demo");
    expect(screen.getByText("Detected: GitLab")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));

    // GitLab uses the generic "Access token" label.
    await userEvent.type(screen.getByLabelText("Access token"), "glpat-secret");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("GitLab")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(createFn.mock.calls[0][0]).toEqual({
      name: "Demo",
      default_branch: "main",
      source: "repository",
      repo_url: "https://gitlab.com/team/demo",
    });
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    const connectIndex = urls.findIndex((url) => url.endsWith("/repo/connect"));
    const cloneIndex = urls.findIndex((url) => url.endsWith("/repo/clone"));
    expect(connectIndex).toBeGreaterThan(-1);
    expect(cloneIndex).toBeGreaterThan(connectIndex);
    expect(bodyOf(fetchMock.mock.calls[connectIndex])).toEqual({
      repo_url: "https://gitlab.com/team/demo",
      provider: "auto",
      auth_type: "personal_access_token",
      token: "glpat-secret",
    });
  });

  it("shows the generated public key for SSH access and never clones automatically", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/repo/connect")) return jsonResponse({ ...created, source: "repository" });
      if (url.endsWith("/repo/credential")) {
        return jsonResponse({
          provider: "generic",
          auth_type: "ssh_key",
          username: null,
          public_key: "ssh-ed25519 AAAAC3Nza enigma-workspace",
          supports_pull_requests: false,
          repo_url: "ssh://git@git.example.com/team/demo.git",
          created_at: "",
          updated_at: "",
        });
      }
      return undefined;
    });
    const createFn = vi.fn().mockResolvedValue({ ...created, source: "repository" });
    const onCreated = vi.fn();
    render(
      <CreateWorkspaceWizard open onClose={vi.fn()} onCreated={onCreated} createFn={createFn} />,
    );

    await userEvent.click(screen.getByRole("radio", { name: /Connect an existing repository/ }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.type(screen.getByLabelText("Workspace name"), "Demo");
    await userEvent.type(
      screen.getByLabelText("Repository URL"),
      "git@git.example.com:team/demo.git",
    );
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(screen.getByRole("radio", { name: "SSH deploy key" }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    expect(await screen.findByText("Deploy key generated")).toBeInTheDocument();
    expect(screen.getByText("ssh-ed25519 AAAAC3Nza enigma-workspace")).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls.some((url) => url.endsWith("/repo/clone"))).toBe(false);
    const connectBody = bodyOf(
      fetchMock.mock.calls[urls.findIndex((url) => url.endsWith("/repo/connect"))],
    );
    expect(connectBody).toEqual({
      repo_url: "git@git.example.com:team/demo.git",
      provider: "auto",
      auth_type: "ssh_key",
    });

    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onCreated).toHaveBeenCalledWith({ ...created, source: "repository" });
  });

  it("keeps the created workspace when connecting fails afterwards", async () => {
    stubFetch((url) => {
      if (url.endsWith("/repo/connect")) {
        return jsonResponse({ detail: "Token authentication requires an HTTPS URL" }, false, 400);
      }
      return undefined;
    });
    const createFn = vi.fn().mockResolvedValue({ ...created, source: "repository" });
    const onCreated = vi.fn();
    render(
      <CreateWorkspaceWizard open onClose={vi.fn()} onCreated={onCreated} createFn={createFn} />,
    );

    await userEvent.click(screen.getByRole("radio", { name: /Connect an existing repository/ }));
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.type(screen.getByLabelText("Workspace name"), "Demo");
    await userEvent.type(screen.getByLabelText("Repository URL"), "https://github.com/e/d");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.type(screen.getByLabelText("Personal access token"), "ghp_x");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    expect(
      await screen.findByText(/Token authentication requires an HTTPS URL/),
    ).toBeInTheDocument();
    expect(onCreated).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onCreated).toHaveBeenCalled();
  });

  it("closes on Escape before anything is created", async () => {
    stubFetch(() => undefined);
    const onClose = vi.fn();
    render(
      <CreateWorkspaceWizard open onClose={onClose} onCreated={vi.fn()} createFn={vi.fn()} />,
    );
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});
