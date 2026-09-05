import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunConfigurationTab } from "./RunConfigurationTab";
import type { CustomCommandResponse, WorkspaceResponse } from "../lib/api";

const workspace = {
  id: "11111111-1111-4111-8111-111111111111",
  owner_id: "22222222-2222-4222-8222-222222222222",
  name: "Demo",
  slug: "demo",
  repo_url: "https://github.com/example/demo",
  source: "repository",
  template_id: null,
  default_branch: "main",
  runtime: "daytona",
  sandbox_id: "sandbox-demo",
  snapshot: null,
  workspace_path: "workspace",
  app_root: null,
  status: "ready",
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
  viewer_role: "owner",
} as WorkspaceResponse;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

const CANDIDATES = [
  {
    name: "dev:web",
    command: "pnpm run dev",
    role: "dev",
    cwd: "frontend",
    port: 5173,
    source_file: "frontend/package.json",
    reason: '"dev" in frontend/package.json scripts',
  },
  {
    name: "dev:api",
    command: "uvicorn app.main:app --port 8000",
    role: "dev",
    cwd: "backend",
    port: 8000,
    source_file: "backend/pyproject.toml",
    reason: "backend/pyproject.toml depends on FastAPI/uvicorn",
  },
  {
    name: "release",
    command: "make release",
    role: "custom",
    cwd: null,
    port: null,
    source_file: "Makefile",
    reason: '"release" target in Makefile',
  },
];

function renderTab(overrides: Partial<Parameters<typeof RunConfigurationTab>[0]> = {}) {
  const onCommandsImported = vi.fn();
  const onSaveAppRoot = vi.fn().mockResolvedValue(undefined);
  render(
    <RunConfigurationTab
      workspace={workspace}
      appRoot=""
      canManage
      worktreeId={null}
      onSaveAppRoot={onSaveAppRoot}
      onCommandsImported={onCommandsImported}
      {...overrides}
    />,
  );
  return { onCommandsImported, onSaveAppRoot };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("RunConfigurationTab", () => {
  it("shows each candidate with the file it was derived from", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string) => {
        if (input.includes("/commands/detect")) {
          return Promise.resolve(jsonResponse({ candidates: CANDIDATES }));
        }
        return Promise.resolve(jsonResponse([]));
      }),
    );
    renderTab();

    await userEvent.click(screen.getByRole("button", { name: "Detect commands" }));

    expect(await screen.findByText("dev:web")).toBeInTheDocument();
    expect(screen.getByText("dev:api")).toBeInTheDocument();
    // The source file is what lets the user judge a proposal.
    expect(screen.getByText('"dev" in frontend/package.json scripts')).toBeInTheDocument();
    expect(
      screen.getByText("backend/pyproject.toml depends on FastAPI/uvicorn"),
    ).toBeInTheDocument();
    expect(screen.getByText("port 5173")).toBeInTheDocument();
    expect(screen.getByText("port 8000")).toBeInTheDocument();
  });

  it("imports only the candidates that stay ticked", async () => {
    const created: CustomCommandResponse[] = [];
    const fetchMock = vi.fn((input: string, init?: RequestInit) => {
      if (input.includes("/commands/detect")) {
        return Promise.resolve(jsonResponse({ candidates: CANDIDATES }));
      }
      if (input.includes("/commands/import")) {
        expect(init?.method).toBe("POST");
        return Promise.resolve(jsonResponse(created, 201));
      }
      return Promise.resolve(jsonResponse([]));
    });
    vi.stubGlobal("fetch", fetchMock);
    const { onCommandsImported } = renderTab();

    await userEvent.click(screen.getByRole("button", { name: "Detect commands" }));
    await screen.findByText("dev:web");

    // `custom` candidates start unticked; untick one of the two pre-selected dev commands.
    await userEvent.click(screen.getByRole("checkbox", { name: /dev:api/ }));
    await userEvent.click(screen.getByRole("button", { name: /Add 1 command/ }));

    await waitFor(() => expect(onCommandsImported).toHaveBeenCalled());
    const importCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/commands/import"),
    );
    const body = JSON.parse(String((importCall?.[1] as RequestInit).body));
    expect(body.items).toHaveLength(1);
    expect(body.items[0]).toMatchObject({ name: "dev:web", cwd: "frontend", port: 5173 });
  });

  it("saves the app root", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse([]))));
    const { onSaveAppRoot } = renderTab();

    await userEvent.type(screen.getByLabelText("App root"), "apps/api");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaveAppRoot).toHaveBeenCalledWith("apps/api"));
  });

  it("applies a preset", async () => {
    const presets = [
      {
        slug: "vite+fastapi",
        name: "Vite + FastAPI",
        description: "A Vite frontend and a FastAPI backend.",
        commands: [{ name: "dev:web", command: "pnpm dev", role: "dev", cwd: "frontend", port: 5173 }],
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn((input: string) => {
        if (input.includes("/commands/presets/")) {
          return Promise.resolve(jsonResponse([], 201));
        }
        if (input.includes("/commands/presets")) {
          return Promise.resolve(jsonResponse(presets));
        }
        return Promise.resolve(jsonResponse([]));
      }),
    );
    const { onCommandsImported } = renderTab();

    await userEvent.click(await screen.findByRole("button", { name: "Use" }));

    await waitFor(() => expect(onCommandsImported).toHaveBeenCalled());
  });
});
