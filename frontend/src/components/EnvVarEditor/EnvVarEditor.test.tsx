import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EnvVarEditor } from "./index";
import type {
  CustomCommandResponse,
  WorkspaceEnvVarResponse,
  WorkspaceResponse,
  WorkspaceServiceResponse,
} from "../../lib/api";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";

let canManage = true;

vi.mock("../../auth/permissions", () => ({
  usePermissions: () => ({ can: () => canManage }),
}));

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
  env_file_path: null,
} as WorkspaceResponse;

function envVar(overrides: Partial<WorkspaceEnvVarResponse>): WorkspaceEnvVarResponse {
  return {
    id: "e1",
    key: "API_KEY",
    masked_value: "****abcd",
    created_at: "2026-09-05T00:00:00Z",
    updated_at: "2026-09-05T00:00:00Z",
    scope: "workspace",
    custom_command_id: null,
    ...overrides,
  };
}

const SERVICE_COMMAND: CustomCommandResponse = {
  id: "cmd-1",
  workspace_id: WORKSPACE_ID,
  name: "Web",
  command: "npm run dev",
  description: null,
  icon: null,
  hotkey: null,
  requires_confirmation: false,
  role: "dev",
  kind: "service",
  cwd: null,
  port: 3000,
  sort_order: 10,
  enabled: true,
  timeout_seconds: null,
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
};

const SERVICE: WorkspaceServiceResponse = {
  id: "svc-1",
  workspace_id: WORKSPACE_ID,
  custom_command_id: "cmd-1",
  name: "Web",
  role: "dev",
  kind: "service",
  command: "npm run dev",
  port: 3000,
  status: "running",
  desired: true,
  worktree_id: null,
  branch: null,
  last_error: null,
  exit_code: null,
  started_at: null,
  last_seen_at: null,
  preview_session_id: null,
  public_url: null,
};

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

function textResponse(body: string, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve({}),
    text: () => Promise.resolve(body),
  } as Response;
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handled = handler(url, init);
    if (handled) {
      return Promise.resolve(handled);
    }
    if (url.match(/\/commands$/)) {
      return Promise.resolve(jsonResponse([SERVICE_COMMAND]));
    }
    if (url.includes("/services")) {
      return Promise.resolve(
        jsonResponse({ services: [SERVICE], environment: { status: "idle", message: null }, limit: 4 }),
      );
    }
    if (url.endsWith("/env-vars")) {
      return Promise.resolve(jsonResponse([envVar({})]));
    }
    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

beforeEach(() => {
  canManage = true;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("EnvVarEditor", () => {
  it("hides edit controls from a viewer without workspace.manageEnv", async () => {
    canManage = false;
    stubFetch(() => undefined);
    render(
      <EnvVarEditor
        workspace={workspace}
        envVars={[envVar({})]}
        onEnvVarsChanged={() => undefined}
        onWorkspaceChanged={() => undefined}
      />,
    );

    expect(await screen.findByText("API_KEY")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Replace value" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Bulk edit/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Select API_KEY")).not.toBeInTheDocument();
    expect(screen.queryByText(".env file")).not.toBeInTheDocument();
  });

  it("previews bulk changes and disables Apply until a preview has run", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.includes("/env-vars/bulk") && !url.includes("bulk-delete")) {
        return jsonResponse({
          items: [
            { key: "A", status: "created", line: 1, message: null },
            { key: "B", status: "updated", line: 2, message: null },
            { key: "C", status: "invalid", line: 3, message: "Bad format" },
          ],
          created: 1,
          updated: 1,
          unchanged: 0,
          invalid: 1,
          dry_run: true,
          affected_services: [],
          warnings: [],
        });
      }
      return undefined;
    });

    render(
      <EnvVarEditor
        workspace={workspace}
        envVars={[envVar({})]}
        onEnvVarsChanged={() => undefined}
        onWorkspaceChanged={() => undefined}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Bulk edit" }));
    await userEvent.type(screen.getByLabelText("Paste .env text"), "A=1\nB=2\nC=");

    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("1 new · 1 updated · 0 unchanged · 1 invalid")).toBeInTheDocument();
    expect(screen.getByText(/Bad format/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply" })).not.toBeDisabled();

    const previewCall = fetchMock.mock.calls.find(([url]) => String(url).includes("/env-vars/bulk"));
    const body = JSON.parse(String((previewCall?.[1] as RequestInit).body));
    expect(body).toMatchObject({ dry_run: true });
  });

  it("applies bulk changes with dry_run false and the pasted text", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.includes("/env-vars/bulk") && !url.includes("bulk-delete")) {
        return jsonResponse({
          items: [],
          created: 1,
          updated: 0,
          unchanged: 0,
          invalid: 0,
          dry_run: true,
          affected_services: [],
          warnings: [],
        });
      }
      return undefined;
    });

    render(
      <EnvVarEditor
        workspace={workspace}
        envVars={[envVar({})]}
        onEnvVarsChanged={() => undefined}
        onWorkspaceChanged={() => undefined}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Bulk edit" }));
    await userEvent.type(screen.getByLabelText("Paste .env text"), "NEW_KEY=value");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByText("1 new · 0 updated · 0 unchanged · 0 invalid");

    await userEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const applyCall = fetchMock.mock.calls
        .filter(([url]) => String(url).includes("/env-vars/bulk") && !String(url).includes("bulk-delete"))
        .map(([, init]) => JSON.parse(String((init as RequestInit).body)))
        .find((body: { dry_run: boolean }) => body.dry_run === false);
      expect(applyCall).toMatchObject({ dry_run: false, text: "NEW_KEY=value" });
    });
  });

  it("requires confirming through ConfirmModal before deleting selected variables", async () => {
    const fetchMock = stubFetch(() => undefined);

    render(
      <EnvVarEditor
        workspace={workspace}
        envVars={[envVar({})]}
        onEnvVarsChanged={() => undefined}
        onWorkspaceChanged={() => undefined}
      />,
    );

    await userEvent.click(await screen.findByLabelText("Select API_KEY"));
    await userEvent.click(screen.getByRole("button", { name: "Delete selected (1)" }));

    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("bulk-delete")),
    ).toBe(false);

    await screen.findByText("Delete selected variables?");
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([url]) => String(url).includes("bulk-delete"))).toBe(true),
    );
  });

  it("exports the template as plain text and copies keys only to the clipboard", async () => {
    const templateText = "# Workspace-wide\nAPI_KEY=\n";
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    stubFetch((url) => {
      if (url.includes("/env-vars/template")) {
        return textResponse(templateText);
      }
      return undefined;
    });

    render(
      <EnvVarEditor
        workspace={workspace}
        envVars={[envVar({})]}
        onEnvVarsChanged={() => undefined}
        onWorkspaceChanged={() => undefined}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Export template" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(templateText));
    expect(templateText).not.toMatch(/=\S/); // keys only, no values
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("renders the affected-services banner and restarts a service from it", async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith("/env-vars") && init?.method === "PUT") {
        return jsonResponse({
          ...envVar({}),
          affected_services: [
            {
              service_id: "svc-1",
              custom_command_id: "cmd-1",
              name: "Web",
              status: "running",
              keys: ["API_KEY"],
            },
          ],
          warnings: [],
        });
      }
      return undefined;
    });

    render(
      <EnvVarEditor
        workspace={workspace}
        envVars={[envVar({})]}
        onEnvVarsChanged={() => undefined}
        onWorkspaceChanged={() => undefined}
      />,
    );

    await userEvent.click(await screen.findByRole("button", { name: "Replace value" }));
    const valueInput = screen.getByLabelText("New value");
    await userEvent.type(valueInput, "secret");
    const editRow = valueInput.closest("div.mt-3") as HTMLElement;
    await userEvent.click(within(editRow).getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/changed — restart/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("/services/svc-1/restart")),
      ).toBe(true),
    );
  });
});
