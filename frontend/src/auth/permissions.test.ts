import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { WorkspaceRole } from "../lib/api";
import { WorkspaceRoleContext } from "./context";
import { can, usePermissions } from "./permissions";

describe("can()", () => {
  it("grants workspace owners every capability", () => {
    expect(can("owner", "workspace.delete")).toBe(true);
    expect(can("owner", "member.manage")).toBe(true);
    expect(can("owner", "agent.run")).toBe(true);
  });

  it("lets a team lead manage members but not delete the workspace", () => {
    expect(can("team_lead", "agent.run")).toBe(true);
    expect(can("team_lead", "member.manage")).toBe(true);
    expect(can("team_lead", "workspace.delete")).toBe(false);
  });

  it("gives team members run/manage but not destructive workspace/member ops", () => {
    expect(can("team_member", "agent.run")).toBe(true);
    expect(can("team_member", "command.manage")).toBe(true);
    expect(can("team_member", "workspace.delete")).toBe(false);
    expect(can("team_member", "member.manage")).toBe(false);
  });

  it("keeps team viewers read-only", () => {
    expect(can("team_viewer", "agent.run")).toBe(false);
    expect(can("team_viewer", "command.run")).toBe(false);
    expect(can("team_viewer", "workspace.create")).toBe(false);
  });

  it("stays permissive while the workspace role is unknown", () => {
    expect(can(null, "agent.run")).toBe(true);
    expect(can(null, "workspace.delete")).toBe(true);
  });
});

describe("usePermissions()", () => {
  function withRole(role: WorkspaceRole) {
    return ({ children }: { children: ReactNode }) =>
      createElement(WorkspaceRoleContext.Provider, { value: role }, children);
  }

  it("reflects the viewed workspace's role", () => {
    const { result } = renderHook(() => usePermissions(), { wrapper: withRole("team_viewer") });
    expect(result.current.role).toBe("team_viewer");
    expect(result.current.can("agent.run")).toBe(false);
  });

  it("grants team-member capabilities", () => {
    const { result } = renderHook(() => usePermissions(), { wrapper: withRole("team_member") });
    expect(result.current.can("agent.run")).toBe(true);
    expect(result.current.can("workspace.delete")).toBe(false);
  });

  it("falls back to permissive when rendered without a workspace role", () => {
    const { result } = renderHook(() => usePermissions());
    expect(result.current.role).toBeNull();
    expect(result.current.can("workspace.delete")).toBe(true);
  });
});
