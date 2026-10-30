import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceStatusPill } from "./DashboardShell";

describe("WorkspaceStatusPill", () => {
  it("tells a stopped Docker workspace that it wakes on next use", () => {
    // local-docker-runtime Epic 05: idle Docker sandboxes are stopped, not deleted.
    render(<WorkspaceStatusPill status="stopped" runtime="docker" />);

    expect(screen.getByText("Workspace stopped")).toBeInTheDocument();
    expect(screen.getByText(/wakes on next use/i)).toBeInTheDocument();
  });

  it("adds no hint for other runtimes or states", () => {
    const { rerender } = render(<WorkspaceStatusPill status="stopped" runtime="daytona" />);
    expect(screen.queryByText(/wakes on next use/i)).not.toBeInTheDocument();

    rerender(<WorkspaceStatusPill status="ready" runtime="docker" />);
    expect(screen.queryByText(/wakes on next use/i)).not.toBeInTheDocument();
  });
});
