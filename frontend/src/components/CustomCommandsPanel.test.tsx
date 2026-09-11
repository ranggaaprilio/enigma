import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CustomCommandsPanel } from "./CustomCommandsPanel";
import { setActiveWorktree } from "../lib/activeWorktree";
import type { CustomCommandResponse, WorkspaceResponse } from "../lib/api";

vi.mock("../auth/permissions", () => ({
  usePermissions: () => ({ can: () => true }),
}));

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";

const workspace = {
  id: WORKSPACE_ID,
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

function command(overrides: Partial<CustomCommandResponse>): CustomCommandResponse {
  return {
    id: "c1",
    workspace_id: WORKSPACE_ID,
    name: "Command",
    command: "echo hi",
    description: null,
    icon: null,
    hotkey: null,
    requires_confirmation: false,
    role: "custom",
    kind: "task",
    cwd: null,
    port: null,
    sort_order: 10,
    enabled: true,
    timeout_seconds: null,
    created_at: "2026-09-05T00:00:00Z",
    updated_at: "2026-09-05T00:00:00Z",
    ...overrides,
  };
}

const INSTALL = command({ id: "c1", name: "Install deps", role: "install", sort_order: 10 });
const DEV = command({
  id: "c2",
  name: "Serve API",
  role: "dev",
  cwd: "backend",
  port: 8000,
  sort_order: 20,
});
const TEST = command({ id: "c3", name: "Unit tests", role: "test", sort_order: 30 });
// A second `dev` entry whose global sort_order neighbour (`TEST`) sits in another group.
const DEV_WORKER = command({ id: "c4", name: "Serve worker", role: "dev", sort_order: 40 });

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handled = handler(url, init);
    if (handled) {
      return Promise.resolve(handled);
    }
    if (url.includes("/runs")) {
      return Promise.resolve(jsonResponse([]));
    }
    if (url.includes("/commands/presets")) {
      return Promise.resolve(jsonResponse([]));
    }
    if (url.match(/\/commands$/)) {
      return Promise.resolve(jsonResponse([INSTALL, DEV, TEST]));
    }
    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("CustomCommandsPanel registry", () => {
  it("groups commands by lifecycle role", async () => {
    stubFetch(() => undefined);
    render(<CustomCommandsPanel workspace={workspace} />);

    expect((await screen.findAllByText("Install deps")).length).toBeGreaterThan(0);
    // Roles are labelled and ordered install -> run -> test, the order you run them in.
    const headings = screen.getAllByText(/^(Install|Run|Test)$/).map((node) => node.textContent);
    expect(headings).toEqual(["Install", "Run", "Test"]);
  });

  it("shows a command's working directory and port", async () => {
    stubFetch(() => undefined);
    render(<CustomCommandsPanel workspace={workspace} />);

    expect(await screen.findByText("backend · port 8000")).toBeInTheDocument();
  });

  it("moves a command past its neighbour in the same role group, not the flat list", async () => {
    // Flat sort_order: install(10) dev(20) test(30) dev-worker(40). Rendered: Run:[API, worker].
    // Moving the worker up must swap it with the API entry two places back, skipping `test`.
    const fetchMock = stubFetch((url) => {
      if (url.includes("/commands/reorder")) {
        return jsonResponse([INSTALL, DEV_WORKER, TEST, DEV]);
      }
      if (url.match(/\/commands$/)) {
        return jsonResponse([INSTALL, DEV, TEST, DEV_WORKER]);
      }
      return undefined;
    });
    render(<CustomCommandsPanel workspace={workspace} />);
    await screen.findAllByText("Serve worker");

    await userEvent.click(screen.getByRole("button", { name: "Move Serve worker up" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("/commands/reorder")),
      ).toBe(true),
    );
    const call = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/commands/reorder"),
    );
    const body = JSON.parse(String((call?.[1] as RequestInit).body));
    expect(body.items.map((item: { id: string }) => item.id)).toEqual(["c1", "c4", "c3", "c2"]);
    expect(body.items.map((item: { sort_order: number }) => item.sort_order)).toEqual([
      10, 20, 30, 40,
    ]);
  });

  it("disables Move up/down at the edges of a role group", async () => {
    stubFetch(() => undefined);
    render(<CustomCommandsPanel workspace={workspace} />);
    await screen.findAllByText("Serve API");

    // Each of the three commands is alone in its group, so nothing can move anywhere. Before,
    // these buttons were live and issued a reorder request that changed nothing visible.
    expect(screen.getByRole("button", { name: "Move Serve API up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Serve API down" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Install deps down" })).toBeDisabled();
  });

  it("runs a command from its hotkey even when macOS reports the composed Option character", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.match(/\/commands$/)) {
        return jsonResponse([INSTALL, command({ ...DEV, hotkey: "d" })]);
      }
      if (url.endsWith("/run")) {
        return jsonResponse(
          {
            id: "r1",
            custom_command_id: "c2",
            workspace_id: WORKSPACE_ID,
            user_id: "u1",
            worktree_id: null,
            cwd: null,
            status: "running",
            exit_code: null,
            output: "",
            started_at: "2026-09-05T00:00:00Z",
            completed_at: null,
          },
          201,
        );
      }
      return undefined;
    });
    render(<CustomCommandsPanel workspace={workspace} />);
    await screen.findAllByText("Serve API");

    // Cmd+Option+D on a Mac: `key` is `∂`, only `code` still says which key was pressed.
    fireEvent.keyDown(window, { key: "∂", code: "KeyD", metaKey: true, altKey: true });

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/run"))).toBe(true),
    );
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/run"));
    expect(String(call?.[0])).toContain("/commands/c2/run");
  });

  it("does not refetch a command's run history after an unrelated reorder", async () => {
    // The runs-fetch effect used to key on the `selectedCommand` object, which `useMemo`
    // re-derives (a new reference) every time `commands` is replaced — including by a reorder
    // response for a *different* command. That refetch reset `streamingRunId` to null and tore
    // down whatever run was live-streaming for the still-selected command.
    const fetchMock = stubFetch((url) => {
      if (url.includes("/commands/reorder")) {
        return jsonResponse([DEV_WORKER, INSTALL, TEST, DEV]);
      }
      if (url.match(/\/commands$/)) {
        return jsonResponse([INSTALL, DEV, TEST, DEV_WORKER]);
      }
      return undefined;
    });
    render(<CustomCommandsPanel workspace={workspace} />);
    await screen.findAllByText("Install deps");
    // Select "Install deps" — its run history is fetched once on selection.
    await userEvent.click(screen.getAllByText("Install deps")[0]);
    const runsUrl = (call: unknown[]) => String(call[0]).includes(`/commands/${INSTALL.id}/runs`);
    await waitFor(() => expect(fetchMock.mock.calls.some(runsUrl)).toBe(true));
    const before = fetchMock.mock.calls.filter(runsUrl).length;

    // Reorder a *different* command ("Serve worker"), which replaces the whole `commands`
    // array and previously re-triggered every effect keyed on `selectedCommand`.
    await userEvent.click(screen.getByRole("button", { name: "Move Serve worker up" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("/commands/reorder")),
      ).toBe(true),
    );

    expect(fetchMock.mock.calls.filter(runsUrl).length).toBe(before);
  });

  it("runs against the active worktree", async () => {
    setActiveWorktree(WORKSPACE_ID, { id: "wt-1", branch: "feature-x" });
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/run")) {
        return jsonResponse(
          {
            id: "r1",
            custom_command_id: "c1",
            workspace_id: WORKSPACE_ID,
            user_id: "u1",
            worktree_id: "wt-1",
            cwd: null,
            status: "running",
            exit_code: null,
            output: "",
            started_at: "2026-09-05T00:00:00Z",
            completed_at: null,
          },
          201,
        );
      }
      return undefined;
    });
    render(<CustomCommandsPanel workspace={workspace} />);
    await screen.findAllByText("Install deps");

    expect(screen.getByText(/Commands run on/)).toHaveTextContent("feature-x");
    await userEvent.click(screen.getByRole("button", { name: "Run command" }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/run"))).toBe(true),
    );
    const call = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/run"));
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toMatchObject({
      worktree_id: "wt-1",
    });
  });
});
