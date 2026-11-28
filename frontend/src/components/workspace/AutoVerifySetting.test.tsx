import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CustomCommandResponse, WorkspaceResponse } from "../../lib/api";
import { AutoVerifySetting } from "./AutoVerifySetting";

const WS = "ws-1";

const workspace = {
  id: WS,
  name: "Demo",
  auto_verify_agent_runs: false,
} as WorkspaceResponse;

function command(role: CustomCommandResponse["role"]): CustomCommandResponse {
  return { id: `cmd-${role}`, name: role, command: "x", role, enabled: true } as CustomCommandResponse;
}

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return { ok, status, headers: new Headers(), json: async () => payload };
}

function stubFetch(commands: CustomCommandResponse[], patchResult?: unknown) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url.endsWith(`/workspaces/${WS}/commands`)) return Promise.resolve(jsonResponse(commands));
    if (url.endsWith(`/workspaces/${WS}`) && init?.method === "PATCH")
      return Promise.resolve(jsonResponse(patchResult));
    return Promise.resolve(jsonResponse({ detail: "unexpected" }, false, 500));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AutoVerifySetting", () => {
  it("saves the toggle immediately and reflects the saved state", async () => {
    const fetchMock = stubFetch([command("test")], { ...workspace, auto_verify_agent_runs: true });
    const onWorkspaceChanged = vi.fn();
    render(<AutoVerifySetting workspace={workspace} onWorkspaceChanged={onWorkspaceChanged} />);

    const toggle = screen.getByRole("checkbox", { name: /auto-verify agent runs/i });
    expect(toggle).not.toBeChecked();
    expect(
      screen.getByText(/runs the workspace's test command after every completed agent run/i),
    ).toBeInTheDocument();

    await userEvent.click(toggle);

    await waitFor(() =>
      expect(onWorkspaceChanged).toHaveBeenCalledWith(
        expect.objectContaining({ auto_verify_agent_runs: true }),
      ),
    );
    const patch = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ auto_verify_agent_runs: true });
    expect(toggle).toBeChecked();
  });

  it("reverts and shows the error when saving fails", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "PATCH")
        return Promise.resolve(jsonResponse({ detail: "Not allowed" }, false, 403));
      return Promise.resolve(jsonResponse([command("test")]));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AutoVerifySetting workspace={workspace} onWorkspaceChanged={vi.fn()} />);

    const toggle = screen.getByRole("checkbox", { name: /auto-verify agent runs/i });
    await userEvent.click(toggle);

    expect(await screen.findByText("Not allowed")).toBeInTheDocument();
    expect(toggle).not.toBeChecked();
  });

  it("hints that auto-verify needs a test command when the workspace has none", async () => {
    stubFetch([command("dev"), { ...command("test"), enabled: false }]);
    render(<AutoVerifySetting workspace={workspace} onWorkspaceChanged={vi.fn()} />);

    expect(await screen.findByText(/needs a custom command with role "test"/i)).toBeInTheDocument();
  });

  it("does not hint when a test command exists", async () => {
    const fetchMock = stubFetch([command("test")]);
    render(<AutoVerifySetting workspace={workspace} onWorkspaceChanged={vi.fn()} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/needs a custom command with role "test"/i)).not.toBeInTheDocument();
  });
});
