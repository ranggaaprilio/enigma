import { useContext } from "react";

import type { WorkspaceRole } from "../lib/api";
import { WorkspaceRoleContext } from "./context";

/**
 * Coarse-grained capabilities used to hide/disable actions in the UI.
 *
 * Capabilities come from the user's role in the *workspace being viewed*, not from their
 * account role: the same person can own one workspace and be a viewer on a teammate's.
 *
 * This is a UX affordance only — the backend remains the authoritative gate for every
 * mutation (see `app/services/access.py`). The matrix mirrors what that module allows.
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

// A team lead drives the workspace and manages membership, but only the owner may delete it.
const TEAM_LEAD_CAPABILITIES: Permission[] = [
  "agent.run",
  "command.run",
  "command.manage",
  "workspace.create",
  "workspace.manageEnv",
  "repo.manage",
  "member.manage",
];

const TEAM_MEMBER_CAPABILITIES: Permission[] = [
  "agent.run",
  "command.run",
  "command.manage",
  "workspace.create",
  "workspace.manageEnv",
  "repo.manage",
];

// Matches `can_drive()` returning false on the backend: observe only.
const TEAM_VIEWER_CAPABILITIES: Permission[] = [];

const ROLE_CAPABILITIES: Record<WorkspaceRole, Permission[]> = {
  owner: OWNER_CAPABILITIES,
  team_lead: TEAM_LEAD_CAPABILITIES,
  team_member: TEAM_MEMBER_CAPABILITIES,
  team_viewer: TEAM_VIEWER_CAPABILITIES,
};

/**
 * Whether a holder of `role` may perform `permission`.
 *
 * `role` is null when the workspace role is not known — no workspace selected, still loading,
 * or the backend reported none. That falls back to permissive rather than read-only: the
 * backend enforces access anyway, and hiding actions from someone entitled to them locks
 * users out of the product, which is far worse than a request that comes back 403.
 */
export function can(role: WorkspaceRole | null, permission: Permission): boolean {
  if (role === null) {
    return true;
  }
  return ROLE_CAPABILITIES[role]?.includes(permission) ?? false;
}

export interface UsePermissionsResult {
  role: WorkspaceRole | null;
  can: (permission: Permission) => boolean;
}

/** Convenience hook binding {@link can} to the workspace currently being viewed. */
export function usePermissions(): UsePermissionsResult {
  const role = useContext(WorkspaceRoleContext);
  return {
    role,
    can: (permission: Permission) => can(role, permission),
  };
}
