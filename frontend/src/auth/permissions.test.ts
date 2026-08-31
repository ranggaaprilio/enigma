import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { UserResponse, UserRole } from "../lib/api";
import { AuthContext, type AuthContextValue } from "./context";
import { can, usePermissions } from "./permissions";

function user(role: UserRole): UserResponse {
  // `can`/`usePermissions` only read `.role`; cast the rest for the test.
  return { role } as UserResponse;
}

describe("can()", () => {
  it("grants owners every capability", () => {
    const owner = user("owner");
    expect(can(owner, "workspace.delete")).toBe(true);
    expect(can(owner, "member.manage")).toBe(true);
    expect(can(owner, "agent.run")).toBe(true);
  });

  it("gives developers run/manage but not destructive workspace/member ops", () => {
    const developer = user("developer");
    expect(can(developer, "agent.run")).toBe(true);
    expect(can(developer, "command.manage")).toBe(true);
    expect(can(developer, "workspace.delete")).toBe(false);
    expect(can(developer, "member.manage")).toBe(false);
  });

  it("keeps viewers read-only", () => {
    const viewer = user("viewer");
    expect(can(viewer, "agent.run")).toBe(false);
    expect(can(viewer, "command.run")).toBe(false);
    expect(can(viewer, "workspace.create")).toBe(false);
  });

  it("denies a null (unauthenticated) user", () => {
    expect(can(null, "agent.run")).toBe(false);
  });
});

describe("usePermissions()", () => {
  function withRole(role: UserRole) {
    const value: AuthContextValue = {
      status: "authenticated",
      user: user(role),
      login: async () => {},
      register: async () => {},
      logout: async () => {},
    };
    return ({ children }: { children: ReactNode }) =>
      createElement(AuthContext.Provider, { value }, children);
  }

  it("reflects the current user's role", () => {
    const { result } = renderHook(() => usePermissions(), { wrapper: withRole("viewer") });
    expect(result.current.role).toBe("viewer");
    expect(result.current.can("agent.run")).toBe(false);
  });

  it("grants developer capabilities", () => {
    const { result } = renderHook(() => usePermissions(), { wrapper: withRole("developer") });
    expect(result.current.can("agent.run")).toBe(true);
    expect(result.current.can("workspace.delete")).toBe(false);
  });

  it("falls back to permissive when rendered without an AuthProvider", () => {
    const { result } = renderHook(() => usePermissions());
    expect(result.current.role).toBeNull();
    expect(result.current.can("workspace.delete")).toBe(true);
  });
});
