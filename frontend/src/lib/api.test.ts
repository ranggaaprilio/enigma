import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cloneRepository,
  commitRepository,
  connectRepository,
  createAgentRun,
  createCustomCommand,
  createTerminalSession,
  createWorkspaceFolder,
  createWorkspace,
  deleteCustomCommand,
  deleteTerminalSession,
  deleteWorkspaceFile,
  deleteWorkspaceEnvVar,
  detectPreviewSession,
  getAgentRun,
  getAgentStreamUrl,
  getCommandRunStreamUrl,
  getCurrentUser,
  getEditorStatus,
  getHealth,
  getRepositoryDiff,
  getRepositoryStatus,
  getTerminalSessionSocketUrl,
  getWorkspaceFileDownloadUrl,
  getWorkspaceFileTree,
  getWorkspaceStatus,
  listTerminalSessions,
  listCustomCommandRuns,
  listCustomCommands,
  listPreviewSessions,
  listWorkspaceEnvVars,
  listWorkspaces,
  listAgentRuns,
  login,
  logout,
  pullRepository,
  pushRepository,
  readWorkspaceFile,
  register,
  renameWorkspaceFile,
  runCustomCommand,
  searchWorkspaceFiles,
  startEditorSession,
  stopAgentRun,
  stopEditorSession,
  refreshPreviewSession,
  stopPreviewSession,
  updateCustomCommand,
  updateWorkspace,
  uploadWorkspaceFile,
  upsertWorkspaceEnvVar,
  writeWorkspaceFile,
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

  it("sends credentialed workspace file requests", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, path: "src/app.ts" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await getWorkspaceFileTree(workspaceId, "src");
    await readWorkspaceFile(workspaceId, "src/app.ts");
    await writeWorkspaceFile(workspaceId, { path: "src/app.ts", content: "hello" });
    await uploadWorkspaceFile(
      workspaceId,
      "src",
      new Blob(["hello"], { type: "text/plain" }),
      "app.ts",
    );
    await createWorkspaceFolder(workspaceId, { path: "docs" });
    await renameWorkspaceFile(workspaceId, { path: "docs", new_path: "guides" });
    await deleteWorkspaceFile(workspaceId, "guides");
    await searchWorkspaceFiles(workspaceId, "app");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/files/tree?path=src`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/files/read?path=src%2Fapp.ts`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/files/write`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `http://localhost:8000/workspaces/${workspaceId}/files/upload?path=src&filename=app.ts`,
      expect.objectContaining({
        body: expect.any(Blob),
        credentials: "include",
        method: "POST",
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      `http://localhost:8000/workspaces/${workspaceId}/files/folders`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      `http://localhost:8000/workspaces/${workspaceId}/files/rename`,
      expect.objectContaining({ credentials: "include", method: "PATCH" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      7,
      `http://localhost:8000/workspaces/${workspaceId}/files?path=guides`,
      expect.objectContaining({ credentials: "include", method: "DELETE" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      8,
      `http://localhost:8000/workspaces/${workspaceId}/files/search?q=app`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(getWorkspaceFileDownloadUrl(workspaceId, "src/app.ts")).toBe(
      `http://localhost:8000/workspaces/${workspaceId}/files/download?path=src%2Fapp.ts`,
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

  it("sends credentialed custom command requests and builds the stream URL", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const commandId = "68b44d9c-4c0a-4770-8c6a-a4a462d0528d";
    const runId = "a4b5e02f-fb93-4e0f-acf5-d7a78dc34fee";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: commandId }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await createCustomCommand(workspaceId, {
      name: "Tests",
      command: "pytest --version",
      requires_confirmation: false,
    });
    await listCustomCommands(workspaceId);
    await updateCustomCommand(workspaceId, commandId, { name: "Pytest" });
    await runCustomCommand(workspaceId, commandId, { confirmed: true });
    await listCustomCommandRuns(workspaceId, commandId);
    await deleteCustomCommand(workspaceId, commandId);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/commands`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/commands`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/commands/${commandId}`,
      expect.objectContaining({ credentials: "include", method: "PATCH" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `http://localhost:8000/workspaces/${workspaceId}/commands/${commandId}/run`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      `http://localhost:8000/workspaces/${workspaceId}/commands/${commandId}/runs`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      `http://localhost:8000/workspaces/${workspaceId}/commands/${commandId}`,
      expect.objectContaining({ credentials: "include", method: "DELETE" }),
    );
    expect(getCommandRunStreamUrl(workspaceId, runId)).toBe(
      `ws://localhost:8000/workspaces/${workspaceId}/command-runs/${runId}/stream`,
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

  it("sends credentialed editor requests", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "inactive", session: null }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await startEditorSession(workspaceId);
    await getEditorStatus(workspaceId);
    await stopEditorSession(workspaceId);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/editor/start`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/editor/status`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/editor/stop`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
  });

  it("sends credentialed preview requests", async () => {
    const workspaceId = "3e15be87-2551-4d70-b972-33fdf68c49eb";
    const sessionId = "6f9d15db-19e2-4c04-95ad-a4e4508ef80d";
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "inactive", session: null }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await listPreviewSessions(workspaceId);
    await detectPreviewSession(workspaceId);
    await refreshPreviewSession(workspaceId, sessionId);
    await stopPreviewSession(workspaceId, sessionId);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `http://localhost:8000/workspaces/${workspaceId}/preview/sessions`,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `http://localhost:8000/workspaces/${workspaceId}/preview/detect`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      `http://localhost:8000/workspaces/${workspaceId}/preview/sessions/${sessionId}/refresh`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      `http://localhost:8000/workspaces/${workspaceId}/preview/sessions/${sessionId}/stop`,
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
  });
});
