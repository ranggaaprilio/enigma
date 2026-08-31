import { useContext } from "react";

import type { UserResponse, UserRole } from "../lib/api";
import { AuthContext } from "./context";

/**
 * Coarse-grained, role-based capabilities used to hide/disable actions in the UI.
 *
 * This is a UX affordance only — the backend remains the authoritative gate for every
 * mutation. The matrix intentionally errs on the side of read-only for `viewer`.
 */
export type Permission =
  | "agent.run"
  | "command.run"
  | "command.manage"
  | "workspace.create"
  | "workspace.delete"
  | "workspace.manageEnv"
  | "repo.manage"
  | "member.manage";

const OWNER_CAPABILITIES: Permission[] = [
  "agent.run",
  "command.run",
  "command.manage",
  "workspace.create",
  "workspace.delete",
  "workspace.manageEnv",
  "repo.manage",
  "member.manage",
];

const DEVELOPER_CAPABILITIES: Permission[] = [
  "agent.run",
  "command.run",
  "command.manage",
  "workspace.create",
  "workspace.manageEnv",
  "repo.manage",
];

const VIEWER_CAPABILITIES: Permission[] = [];

const ROLE_CAPABILITIES: Record<UserRole, Permission[]> = {
  owner: OWNER_CAPABILITIES,
  developer: DEVELOPER_CAPABILITIES,
  viewer: VIEWER_CAPABILITIES,
};

/** Whether `user` (may be null when unauthenticated) may perform `permission`. */
export function can(user: UserResponse | null, permission: Permission): boolean {
  if (!user) {
    return false;
  }
  return ROLE_CAPABILITIES[user.role]?.includes(permission) ?? false;
}

export interface UsePermissionsResult {
  role: UserRole | null;
  can: (permission: Permission) => boolean;
}

/** Convenience hook binding {@link can} to the current authenticated user. */
export function usePermissions(): UsePermissionsResult {
  const auth = useContext(AuthContext);
  // No provider means the component is rendered outside the app shell (e.g. isolated tests).
  // This layer is only a UI affordance — the backend enforces authorization — so fall back to
  // permissive rather than throwing. The real app always wraps routes in AuthProvider.
  if (auth === null) {
    return { role: null, can: () => true };
  }
  const { user } = auth;
  return {
    role: user?.role ?? null,
    can: (permission: Permission) => can(user, permission),
  };
}
