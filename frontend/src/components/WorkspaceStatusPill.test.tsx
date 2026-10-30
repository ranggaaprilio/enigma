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

  it("suggests checking the daemon when a Docker workspace is in error", () => {
    const { rerender } = render(<WorkspaceStatusPill status="error" runtime="docker" />);
    expect(screen.getByText(/is the docker daemon running\?/i)).toBeInTheDocument();

    rerender(<WorkspaceStatusPill status="error" runtime="daytona" />);
    expect(screen.queryByText(/docker daemon/i)).not.toBeInTheDocument();
  });

  it("shows the provider's status detail, e.g. an out-of-memory kill", () => {
    render(
      <WorkspaceStatusPill
        status="ready"
        runtime="docker"
        detail="A process was killed: out of memory (limit 4096 MB)"
      />,
    );
    expect(screen.getByText(/out of memory \(limit 4096 MB\)/)).toBeInTheDocument();
  });
});
