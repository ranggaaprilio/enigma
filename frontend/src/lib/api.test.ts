import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cloneRepository,
  commitRepository,
  connectRepository,
  createAgentRun,
  createTerminalSession,
  createWorkspace,
  deleteTerminalSession,
  deleteWorkspaceEnvVar,
  getAgentRun,
  getAgentStreamUrl,
  getCurrentUser,
  getHealth,
  getRepositoryDiff,
  getRepositoryStatus,
  getTerminalSessionSocketUrl,
  getWorkspaceStatus,
  listTerminalSessions,
  listWorkspaceEnvVars,
  listWorkspaces,
  listAgentRuns,
  login,
  logout,
  pullRepository,
  pushRepository,
  register,
  stopAgentRun,
  updateWorkspace,
  upsertWorkspaceEnvVar,
} from "./api";

describe("getHealth", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns successful health payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", database: "ok", redis: "ok" }),
      }),
    );

    await expect(getHealth()).resolves.toEqual({ status: "ok", database: "ok", redis: "ok" });
  });

  it("returns degraded health payloads from 503 responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({ status: "degraded", database: "ok", redis: "error" }),
      }),
    );

    await expect(getHealth()).resolves.toEqual({
      status: "degraded",
      database: "ok",
      redis: "error",
    });
  });

  it("sends credentialed auth requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: "fb6cd934-74f2-404b-bff1-30f964e9326c",
        email: "user@example.com",
        name: null,
        role: "viewer",
        is_active: true,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await login({ email: "user@example.com", password: "password-one" });
    await register({ email: "new@example.com", password: "password-two", name: "New User" });
    await getCurrentUser();

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/auth/login",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/auth/register",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/auth/me",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("clears the session through logout", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(logout()).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/auth/logout",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
  });

  it("sends credentialed workspace requests", async () => {
    const workspace = {
      id: "3e15be87-2551-4d70-b972-33fdf68c49eb",
      owner_id: "fb6cd934-74f2-404b-bff1-30f964e9326c",
      name: "Demo",
      slug: "demo",
      repo_url: "https://github.com/example/demo",
      default_branch: "main",
      local_path: "/tmp/demo",
      status: "ready",
      created_at: "2026-05-23T00:00:00Z",
      updated_at: "2026-05-23T00:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => workspace,
    });
    vi.stubGlobal("fetch", fetchMock);

    await createWorkspace({ name: "Demo", repo_url: workspace.repo_url });
    await updateWorkspace(workspace.id, { default_branch: "develop" });
    await getWorkspaceStatus(workspace.id);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/workspaces",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspace.id}`,
      expect.objectContaining({ credentials: "include", method: "PATCH" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspace.id}/status`,
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("sends credentialed workspace collection and env var requests", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    });
    vi.stubGlobal("fetch", fetchMock);

    await listWorkspaces();
    await listWorkspaceEnvVars(workspaceId);
    await upsertWorkspaceEnvVar(workspaceId, { key: "SECRET_TOKEN", value: "raw-secret" });
    await deleteWorkspaceEnvVar(workspaceId, "SECRET_TOKEN");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/workspaces",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/env-vars`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/env-vars`,
      expect.objectContaining({ credentials: "include", method: "PUT" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `http://localhost:8000/workspaces/${workspaceId}/env-vars?key=SECRET_TOKEN`,
      expect.objectContaining({ credentials: "include", method: "DELETE" }),
    );
  });

  it("sends credentialed repository requests", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, message: "Done" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await connectRepository(workspaceId, {
      repo_url: "https://github.com/example/demo",
      token: "raw-secret",
    });
    await cloneRepository(workspaceId);
    await getRepositoryStatus(workspaceId);
    await getRepositoryDiff(workspaceId, "README.md");
    await pullRepository(workspaceId);
    await commitRepository(workspaceId, { message: "Update README" });
    await pushRepository(workspaceId);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/repo/connect`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/repo/clone`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/repo/status`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `http://localhost:8000/workspaces/${workspaceId}/repo/diff?path=README.md`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      `http://localhost:8000/workspaces/${workspaceId}/repo/pull`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      `http://localhost:8000/workspaces/${workspaceId}/repo/commit`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      7,
      `http://localhost:8000/workspaces/${workspaceId}/repo/push`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
  });

  it("sends credentialed agent requests and builds the stream URL", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const runId = "68b44d9c-4c0a-4770-8c6a-a4a462d0528d";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: runId }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await createAgentRun(workspaceId, { prompt: "Inspect files" });
    await listAgentRuns(workspaceId);
    await getAgentRun(workspaceId, runId);
    await stopAgentRun(workspaceId, runId);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/agent/runs`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/agent/runs`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/agent/runs/${runId}`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `http://localhost:8000/workspaces/${workspaceId}/agent/runs/${runId}/stop`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(getAgentStreamUrl(workspaceId, runId)).toBe(
      `ws://localhost:8000/workspaces/${workspaceId}/agent/runs/${runId}/stream`,
    );
  });

  it("sends credentialed terminal requests and builds the socket URL", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const sessionId = "a4b5e02f-fb93-4e0f-acf5-d7a78dc34fee";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: sessionId }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await createTerminalSession(workspaceId);
    await listTerminalSessions(workspaceId);
    await deleteTerminalSession(workspaceId, sessionId);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/terminal/sessions`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/terminal/sessions`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/terminal/sessions/${sessionId}`,
      expect.objectContaining({ credentials: "include", method: "DELETE" }),
    );
    expect(getTerminalSessionSocketUrl(workspaceId, sessionId)).toBe(
      `ws://localhost:8000/workspaces/${workspaceId}/terminal/sessions/${sessionId}`,
    );
  });
});
