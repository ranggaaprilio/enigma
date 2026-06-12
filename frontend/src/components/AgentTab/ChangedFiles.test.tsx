import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChangedFiles } from "./ChangedFiles";
import type { AgentFileChangeListResponse } from "../../lib/api";

vi.mock("../../lib/api", () => ({
  getAgentFileChanges: vi.fn(),
}));

import { getAgentFileChanges } from "../../lib/api";

const mockGetAgentFileChanges = vi.mocked(getAgentFileChanges);

afterEach(() => {
  vi.restoreAllMocks();
});

const workspaceId = "ws-1";
const runId = "run-1";

function makeFileChange(
  id: string,
  path: string,
  status: "created" | "modified" | "deleted" | "renamed",
) {
  return {
    id,
    path,
    status,
    tool_call_id: null,
    created_at: "2026-06-12T00:00:00Z",
  };
}

describe("ChangedFiles", () => {
  it("renders file list with status badges", async () => {
    const response: AgentFileChangeListResponse = {
      items: [
        makeFileChange("1", "src/app.tsx", "created"),
        makeFileChange("2", "src/utils.ts", "modified"),
        makeFileChange("3", "src/old.ts", "deleted"),
        makeFileChange("4", "src/renamed.ts", "renamed"),
      ],
      total: 4,
      capped: false,
    };
    mockGetAgentFileChanges.mockResolvedValue(response);

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    await waitFor(() => {
      expect(screen.getByText("src/app.tsx")).toBeInTheDocument();
    });

    expect(screen.getByText("created")).toBeInTheDocument();
    expect(screen.getByText("modified")).toBeInTheDocument();
    expect(screen.getByText("deleted")).toBeInTheDocument();
    expect(screen.getByText("renamed")).toBeInTheDocument();
    expect(screen.getByText("src/utils.ts")).toBeInTheDocument();
    expect(screen.getByText("src/old.ts")).toBeInTheDocument();
    expect(screen.getByText("src/renamed.ts")).toBeInTheDocument();
  });

  it("caps display at 50 files with summary text", async () => {
    const items = Array.from({ length: 55 }, (_, i) =>
      makeFileChange(`id-${i}`, `src/file-${i}.ts`, "modified"),
    );
    const response: AgentFileChangeListResponse = {
      items: items.slice(0, 50),
      total: 55,
      capped: true,
    };
    mockGetAgentFileChanges.mockResolvedValue(response);

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    await waitFor(() => {
      expect(screen.getByText("src/file-0.ts")).toBeInTheDocument();
    });

    // Only 50 file paths rendered
    const fileElements = screen.getAllByText(/src\/file-\d+\.ts/);
    expect(fileElements).toHaveLength(50);

    // Summary text for remaining files
    expect(screen.getByText("5 more files")).toBeInTheDocument();
  });

  it("shows empty state when no file changes", async () => {
    const response: AgentFileChangeListResponse = {
      items: [],
      total: 0,
      capped: false,
    };
    mockGetAgentFileChanges.mockResolvedValue(response);

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    await waitFor(() => {
      expect(screen.getByText("No file changes")).toBeInTheDocument();
    });
  });

  it("shows loading skeleton while fetching", () => {
    mockGetAgentFileChanges.mockReturnValue(new Promise(() => {}));

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    // Skeleton rows should be visible (5 of them)
    const skeletonRows = screen.getAllByRole("listitem");
    expect(skeletonRows).toHaveLength(5);
  });

  it("uses correct status badge colors", async () => {
    const response: AgentFileChangeListResponse = {
      items: [
        makeFileChange("1", "a.ts", "created"),
        makeFileChange("2", "b.ts", "modified"),
        makeFileChange("3", "c.ts", "deleted"),
        makeFileChange("4", "d.ts", "renamed"),
      ],
      total: 4,
      capped: false,
    };
    mockGetAgentFileChanges.mockResolvedValue(response);

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    await waitFor(() => {
      expect(screen.getByText("created")).toBeInTheDocument();
    });

    const createdBadge = screen.getByText("created");
    expect(createdBadge.className).toContain("bg-signal");
    expect(createdBadge.className).toContain("text-signal");

    const modifiedBadge = screen.getByText("modified");
    expect(modifiedBadge.className).toContain("bg-warn");
    expect(modifiedBadge.className).toContain("text-warn");

    const deletedBadge = screen.getByText("deleted");
    expect(deletedBadge.className).toContain("bg-danger");
    expect(deletedBadge.className).toContain("text-danger");

    const renamedBadge = screen.getByText("renamed");
    expect(renamedBadge.className).toContain("bg-blue-500");
    expect(renamedBadge.className).toContain("text-blue-600");
  });

  it("uses monospace font for file paths", async () => {
    const response: AgentFileChangeListResponse = {
      items: [makeFileChange("1", "src/app.tsx", "created")],
      total: 1,
      capped: false,
    };
    mockGetAgentFileChanges.mockResolvedValue(response);

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    await waitFor(() => {
      expect(screen.getByText("src/app.tsx")).toBeInTheDocument();
    });

    const pathElement = screen.getByText("src/app.tsx");
    expect(pathElement.className).toContain("font-mono");
  });

  it("shows singular '1 more file' when exactly one remaining", async () => {
    const items = Array.from({ length: 51 }, (_, i) =>
      makeFileChange(`id-${i}`, `src/file-${i}.ts`, "modified"),
    );
    const response: AgentFileChangeListResponse = {
      items: items.slice(0, 50),
      total: 51,
      capped: true,
    };
    mockGetAgentFileChanges.mockResolvedValue(response);

    render(<ChangedFiles workspaceId={workspaceId} runId={runId} />);

    await waitFor(() => {
      expect(screen.getByText("1 more file")).toBeInTheDocument();
    });
  });
});