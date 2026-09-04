import { createContext, useContext } from "react";

import type { LoginPayload, RegisterPayload, UserResponse, WorkspaceRole } from "../lib/api";

export type AuthStatus = "loading" | "authenticated" | "unauthenticated";

export type AuthContextValue = {
  status: AuthStatus;
  user: UserResponse | null;
  login: (payload: LoginPayload) => Promise<void>;
  register: (payload: RegisterPayload) => Promise<void>;
  logout: () => Promise<void>;
};

export const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * The current user's role *in the workspace being viewed* — the role UI capabilities are
 * derived from (see `./permissions`). `null` means "not known": no workspace is selected, it
 * is still loading, or the backend reported no role. Provided by the workspace route.
 */
export const WorkspaceRoleContext = createContext<WorkspaceRole | null>(null);

export function useAuth() {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return value;
}
