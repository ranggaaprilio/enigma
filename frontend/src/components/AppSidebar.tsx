import { type ComponentType, type ReactNode } from "react";
import { NavLink, useNavigate } from "react-router";
import {
  Bot,
  ClipboardList,
  Code2,
  Coins,
  FileCode2,
  FolderKanban,
  FolderTree,
  GitBranch,
  LayoutDashboard,
  ListChecks,
  Monitor,
  Terminal,
  Users,
  Workflow,
} from "lucide-react";

import { useAuth } from "../auth/context";

export type AppSidebarProps = {
  projectId?: string;
  teamId?: string;
  projectName?: string;
  teamName?: string;
  /** When set, the workspace sub-menu is rendered against this base path. */
  workspaceBasePath?: string;
  /** Show the user / sign-out footer (hidden when the page header already has it). */
  showUser?: boolean;
};

type NavItem = {
  label: string;
  icon: ComponentType<{ className?: string }>;
  to: string;
  end?: boolean;
};

function workspaceNav(base: string): NavItem[] {
  return [
    { label: "Overview", icon: LayoutDashboard, to: base, end: true },
    { label: "Repository", icon: GitBranch, to: `${base}/repository` },
    { label: "Files", icon: FolderTree, to: `${base}/files` },
    { label: "Commands", icon: ListChecks, to: `${base}/commands` },
    { label: "Spec", icon: ClipboardList, to: `${base}/spec` },
    { label: "Agent", icon: Bot, to: `${base}/agents` },
    { label: "Usage", icon: Coins, to: `${base}/usage` },
    { label: "Editor", icon: FileCode2, to: `${base}/editor` },
    { label: "Terminal", icon: Terminal, to: `${base}/terminal` },
    { label: "Preview", icon: Monitor, to: `${base}/preview` },
    { label: "Audit", icon: Code2, to: `${base}/audit` },
  ];
}

const TOP_NAV: NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, to: "/", end: true },
  { label: "Projects", icon: FolderKanban, to: "/projects", end: true },
];

export function AppSidebar({
  projectId,
  teamId,
  projectName,
  teamName,
  workspaceBasePath,
  showUser = true,
}: AppSidebarProps) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login", { replace: true });
  }

  return (
    <aside className="flex shrink-0 flex-col border-b border-line bg-panel px-3 py-4 lg:sticky lg:top-0 lg:h-screen lg:w-[240px] lg:border-b-0 lg:border-r">
      <NavLink to="/" className="mb-4 flex items-center gap-3 px-2">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary text-white">
          <Workflow className="h-5 w-5" aria-hidden="true" />
        </div>
        <span className="text-xs font-semibold uppercase tracking-[0.18em] text-primary">
          Enigma
        </span>
      </NavLink>

      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto">
        <SectionLabel>Menu</SectionLabel>
        {TOP_NAV.map((item) => (
          <SidebarLink key={item.label} item={item} />
        ))}

        {projectId && (
          <>
            <SectionLabel>Project</SectionLabel>
            <SidebarLink
              item={{
                label: projectName ?? "Current project",
                icon: FolderTree,
                to: `/projects/${projectId}`,
                end: true,
              }}
            />
          </>
        )}

        {projectId && teamId && (
          <>
            <SectionLabel>Team</SectionLabel>
            <SidebarLink
              item={{
                label: teamName ?? "Current team",
                icon: Users,
                to: `/projects/${projectId}/teams/${teamId}`,
                end: true,
              }}
            />
          </>
        )}

        {workspaceBasePath && (
          <>
            <SectionLabel>Workspace</SectionLabel>
            {workspaceNav(workspaceBasePath).map((item) => (
              <SidebarLink key={item.label} item={item} />
            ))}
          </>
        )}
      </nav>

      {showUser && user && (
        <div className="mt-4 border-t border-line pt-4">
          <p className="truncate px-2 text-sm font-medium text-ink">
            {user.name ?? user.email}
          </p>
          <button
            type="button"
            onClick={() => void handleLogout()}
            className="mt-1 w-full rounded-md px-2 py-1 text-left text-sm text-body transition hover:bg-line"
          >
            Sign out
          </button>
        </div>
      )}
    </aside>
  );
}

function SidebarLink({ item }: { item: NavItem }) {
  const { label, icon: Icon, to, end } = item;
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `flex items-center gap-3 rounded-md px-3 py-2 text-sm transition ${
          isActive
            ? "bg-primary text-white"
            : "text-body hover:bg-white hover:text-ink"
        }`
      }
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="truncate">{label}</span>
    </NavLink>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="mt-4 px-3 pb-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted first:mt-0">
      {children}
    </p>
  );
}
