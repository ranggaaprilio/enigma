import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SpecPanel } from "./index";
import {
  getSpecArtifact,
  listSpecWorkflows,
  type SpecPhaseName,
  type SpecPhaseResponse,
  type SpecPhaseStatus,
  type SpecWorkflowResponse,
  type WorkspaceResponse,
} from "../../lib/api";

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    listSpecWorkflows: vi.fn(),
    getSpecWorkflow: vi.fn(),
    getSpecArtifact: vi.fn(),
    listSpecArtifacts: vi.fn().mockResolvedValue({ phase: "plan", items: [] }),
    getAnalyzeQuestions: vi.fn().mockResolvedValue({ phase: "analyze", items: [] }),
    submitAnalyzeAnswers: vi.fn(),
    approveSpecPhase: vi.fn(),
    requestSpecChanges: vi.fn(),
    runSpecPhase: vi.fn(),
    createSpecWorkflow: vi.fn(),
  };
});

const PHASE_ORDER: SpecPhaseName[] = [
  "constitution",
  "specify",
  "clarify",
  "plan",
  "analyze",
  "tasks",
  "implement",
];

const workspace = {
  id: "3e15be87-2551-4d70-b972-33fdf68c49eb",
  name: "Demo",
} as WorkspaceResponse;

function makePhases(overrides: Partial<Record<SpecPhaseName, SpecPhaseStatus>>): SpecPhaseResponse[] {
  return PHASE_ORDER.map((phase, index) => ({
    id: `phase-${index}`,
    workflow_id: "wf-1",
    phase,
    status: overrides[phase] ?? "pending",
    artifact_path: null,
    agent_run_id: null,
    reviewed_by: null,
    reviewed_at: null,
    review_notes: null,
    attempt: 1,
    created_at: "2026-06-27T00:00:00Z",
    updated_at: "2026-06-27T00:00:00Z",
  }));
}

function makeWorkflow(
  currentPhase: SpecPhaseName,
  overrides: Partial<Record<SpecPhaseName, SpecPhaseStatus>>,
): SpecWorkflowResponse {
  return {
    id: "wf-1",
    workspace_id: workspace.id,
    user_id: "user-1",
    feature_name: "Add SSO login",
    feature_slug: "001-add-sso-login",
    description: "We need SSO",
    branch: "001-add-sso-login",
    spec_dir: "specs/001-add-sso-login",
    worktree_path: "worktrees/001-add-sso-login",
    status: "awaiting_review",
    current_phase: currentPhase,
    created_at: "2026-06-27T00:00:00Z",
    updated_at: "2026-06-27T00:00:00Z",
    phases: makePhases(overrides),
  };
}

describe("SpecPanel", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders the full phase stepper", async () => {
    vi.mocked(listSpecWorkflows).mockResolvedValue([
      makeWorkflow("specify", { specify: "awaiting_review" }),
    ]);
    vi.mocked(getSpecArtifact).mockResolvedValue({
      phase: "specify",
      path: "specs/001-add-sso-login/spec.md",
      content: "# Spec",
    });

    render(<SpecPanel workspace={workspace} />);

    await waitFor(() => expect(screen.getAllByText("Add SSO login").length).toBeGreaterThan(0));
    for (const label of ["Constitution", "Specify", "Clarify", "Plan", "Analyze", "Tasks", "Implement"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  it("hard-blocks the implement Run button until plan and tasks are approved", async () => {
    vi.mocked(listSpecWorkflows).mockResolvedValue([
      makeWorkflow("implement", { plan: "pending", tasks: "pending" }),
    ]);

    render(<SpecPanel workspace={workspace} />);

    await waitFor(() => expect(screen.getAllByText("Add SSO login").length).toBeGreaterThan(0));
    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    expect(runButton).toHaveAttribute("title", "Approve the plan and tasks phases first");
  });

  it("enables the implement Run button once plan and tasks are approved", async () => {
    vi.mocked(listSpecWorkflows).mockResolvedValue([
      makeWorkflow("implement", { plan: "approved", tasks: "approved" }),
    ]);

    render(<SpecPanel workspace={workspace} />);

    await waitFor(() => expect(screen.getAllByText("Add SSO login").length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
  });

  it("shows approve and request-changes controls for a phase awaiting review", async () => {
    vi.mocked(listSpecWorkflows).mockResolvedValue([
      makeWorkflow("plan", { specify: "approved", plan: "awaiting_review" }),
    ]);
    vi.mocked(getSpecArtifact).mockResolvedValue({
      phase: "plan",
      path: "specs/001-add-sso-login/plan.md",
      content: "# Plan",
    });

    render(<SpecPanel workspace={workspace} />);

    await waitFor(() => expect(screen.getAllByText("Add SSO login").length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /request changes/i })).toBeInTheDocument();
  });
});
