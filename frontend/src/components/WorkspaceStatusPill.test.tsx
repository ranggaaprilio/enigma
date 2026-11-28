import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceStatusPill } from "./DashboardShell";

describe("WorkspaceStatusPill", () => {
  it("tells a stopped OpenSandbox workspace that it wakes on next use", () => {
    // Idle OpenSandbox sandboxes are paused, not deleted; the next use resumes them.
    render(<WorkspaceStatusPill status="stopped" runtime="opensandbox" />);

    expect(screen.getByText("Workspace stopped")).toBeInTheDocument();
    expect(screen.getByText(/wakes on next use/i)).toBeInTheDocument();
  });

  it("adds no hint for other runtimes or states", () => {
    const { rerender } = render(<WorkspaceStatusPill status="stopped" runtime="daytona" />);
    expect(screen.queryByText(/wakes on next use/i)).not.toBeInTheDocument();

    rerender(<WorkspaceStatusPill status="ready" runtime="opensandbox" />);
    expect(screen.queryByText(/wakes on next use/i)).not.toBeInTheDocument();
  });

  it("suggests checking the server when an OpenSandbox workspace is in error", () => {
    const { rerender } = render(<WorkspaceStatusPill status="error" runtime="opensandbox" />);
    expect(screen.getByText(/is the opensandbox server running\?/i)).toBeInTheDocument();

    rerender(<WorkspaceStatusPill status="error" runtime="daytona" />);
    expect(screen.queryByText(/opensandbox server/i)).not.toBeInTheDocument();
  });

  it("shows the provider's status detail, e.g. an out-of-memory kill", () => {
    render(
      <WorkspaceStatusPill
        status="ready"
        runtime="opensandbox"
        detail="A process was killed: out of memory (limit 4096 MB)"
      />,
    );
    expect(screen.getByText(/out of memory \(limit 4096 MB\)/)).toBeInTheDocument();
  });
});
