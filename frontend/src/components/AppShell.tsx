import { type ReactNode } from "react";

import { AppSidebar, type AppSidebarProps } from "./AppSidebar";

type AppShellProps = AppSidebarProps & { children: ReactNode };

/**
 * App-wide layout: a persistent left sidebar plus a content column.
 * Pass the current project/team/workspace context through to the sidebar.
 */
export function AppShell({ children, ...sidebar }: AppShellProps) {
  return (
    <div className="flex min-h-screen flex-col bg-canvas lg:flex-row">
      <AppSidebar {...sidebar} />
      <div className="flex min-h-screen min-w-0 flex-1 flex-col">{children}</div>
    </div>
  );
}
