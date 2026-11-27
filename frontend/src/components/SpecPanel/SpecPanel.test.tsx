import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SpecPanel } from "./index";
import {
  ApiError,
  approveSpecPhase,
  approveVerificationRun,
  createSpecWorkflow,
  getSpecArtifact,
  getSpecWorkflow,
  getVerificationRun,
  listSpecWorkflows,
  listVerificationRuns,
  openSpecPullRequest,
  runSpecPhase,
  verifySpecImplement,
  type VerificationRunDetailResponse,
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
    listVerificationRuns: vi.fn().mockResolvedValue([]),
    getVerificationRun: vi.fn(),
    approveVerificationRun: vi.fn(),
    rejectVerificationRun: vi.fn(),
    verifySpecImplement: vi.fn(),
    openSpecPullRequest: vi.fn(),
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

  it("disables Run with a countdown when a phase run is rate limited", async () => {
    vi.mocked(listSpecWorkflows).mockResolvedValue([
      makeWorkflow("specify", { constitution: "approved", specify: "pending" }),
    ]);
    vi.mocked(runSpecPhase).mockRejectedValue(
      new ApiError("Too many requests. Please slow down.", 429, 20),
    );

    render(<SpecPanel workspace={workspace} />);

    await waitFor(() => expect(screen.getAllByText("Add SSO login").length).toBeGreaterThan(0));
    await userEvent.click(screen.getByRole("button", { name: "Run" }));

    const notice = "Too many requests. Please slow down. Retry in 20s.";
    expect(await screen.findByText(notice)).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    expect(runButton).toHaveAttribute("title", notice);
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

  describe("implement verification (Plan 3)", () => {
    function linkedRun(
      overrides: Partial<VerificationRunDetailResponse> = {},
    ): VerificationRunDetailResponse {
      return {
        id: "vr-1",
        workspace_id: workspace.id,
        plan_ref: "specs/001-add-sso-login/plan.md",
        status: "review",
        spec_workflow_id: "wf-1",
        created_at: "2026-11-27T00:00:00Z",
        updated_at: "2026-11-27T00:01:00Z",
        evidence: [
          {
            id: "e1",
            kind: "test",
            status: "passed",
            detail: { command: "pytest -q", exit_code: 0, output: "3 passed", truncated: false },
            path: null,
            created_at: "2026-11-27T00:00:30Z",
          },
        ],
        ...overrides,
      };
    }

    function verifiedWorkflow(): SpecWorkflowResponse {
      return {
        ...makeWorkflow("implement", {
          plan: "approved",
          tasks: "approved",
          implement: "awaiting_review",
        }),
        verification: { test_command: "pytest -q", observe_port: null, max_iterations: 2 },
      };
    }

    it("sends optional verification criteria when starting a workflow", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([]);
      vi.mocked(createSpecWorkflow).mockResolvedValue(verifiedWorkflow());
      const user = userEvent.setup();

      render(<SpecPanel workspace={workspace} />);
      await user.type(await screen.findByPlaceholderText(/Feature name/), "Add SSO login");
      await user.type(screen.getByPlaceholderText(/Describe what you want built/), "We need SSO");
      await user.type(screen.getByLabelText("Verification test command (optional)"), "pytest -q");
      await user.click(screen.getByRole("button", { name: /Start spec workflow/ }));

      await waitFor(() =>
        expect(createSpecWorkflow).toHaveBeenCalledWith(workspace.id, {
          feature_name: "Add SSO login",
          description: "We need SSO",
          verification: { test_command: "pytest -q", max_iterations: 2 },
        }),
      );
    });

    it("omits verification when no test command is given", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([]);
      vi.mocked(createSpecWorkflow).mockResolvedValue(verifiedWorkflow());
      const user = userEvent.setup();

      render(<SpecPanel workspace={workspace} />);
      await user.type(await screen.findByPlaceholderText(/Feature name/), "Add SSO login");
      await user.type(screen.getByPlaceholderText(/Describe what you want built/), "We need SSO");
      await user.click(screen.getByRole("button", { name: /Start spec workflow/ }));

      await waitFor(() =>
        expect(createSpecWorkflow).toHaveBeenCalledWith(workspace.id, {
          feature_name: "Add SSO login",
          description: "We need SSO",
        }),
      );
    });

    it("gates implement through its verification evidence", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([verifiedWorkflow()]);
      vi.mocked(listVerificationRuns).mockResolvedValue([linkedRun()]);
      vi.mocked(getVerificationRun).mockResolvedValue(linkedRun());
      vi.mocked(approveVerificationRun).mockResolvedValue({ ...linkedRun(), status: "approved" });
      vi.mocked(getSpecWorkflow).mockResolvedValue(verifiedWorkflow());
      const user = userEvent.setup();

      render(<SpecPanel workspace={workspace} />);

      const evidence = await screen.findByRole("region", { name: "Evidence" });
      expect(listVerificationRuns).toHaveBeenCalledWith(workspace.id, 1, "wf-1");
      expect(evidence).toHaveTextContent("pytest -q");
      // One Approve only: the verification run's. The phase cannot be approved around it.
      const approveButtons = screen.getAllByRole("button", { name: "Approve" });
      expect(approveButtons).toHaveLength(1);
      await user.click(approveButtons[0]);

      await waitFor(() => expect(approveVerificationRun).toHaveBeenCalledWith(workspace.id, "vr-1"));
      expect(approveSpecPhase).not.toHaveBeenCalled();
    });

    it("re-runs verification for implement awaiting review", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([verifiedWorkflow()]);
      vi.mocked(listVerificationRuns).mockResolvedValue([linkedRun({ status: "rejected" })]);
      vi.mocked(getVerificationRun).mockResolvedValue(linkedRun({ status: "rejected" }));
      vi.mocked(verifySpecImplement).mockResolvedValue({ ...linkedRun({ id: "vr-2", status: "planning" }) });
      const user = userEvent.setup();

      render(<SpecPanel workspace={workspace} />);

      await user.click(await screen.findByRole("button", { name: "Re-run verification" }));
      await waitFor(() => expect(verifySpecImplement).toHaveBeenCalledWith(workspace.id, "wf-1"));
    });
  });

  describe("pull request (Plan 4)", () => {
    const PR = { number: 12, url: "https://github.com/example/demo/pull/12", state: "open" };

    function completedWorkflow(overrides: Partial<SpecWorkflowResponse> = {}): SpecWorkflowResponse {
      return {
        ...makeWorkflow("implement", {
          plan: "approved",
          tasks: "approved",
          implement: "approved",
        }),
        status: "completed",
        ...overrides,
      };
    }

    it("opens a pull request for a completed workflow and then links it", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([completedWorkflow()]);
      vi.mocked(openSpecPullRequest).mockResolvedValue(PR);
      vi.mocked(getSpecWorkflow).mockResolvedValue(completedWorkflow({ pull_request: PR }));
      const user = userEvent.setup();

      render(<SpecPanel workspace={workspace} />);

      await user.click(await screen.findByRole("button", { name: "Open pull request" }));

      await waitFor(() => expect(openSpecPullRequest).toHaveBeenCalledWith(workspace.id, "wf-1"));
      const link = await screen.findByRole("link", { name: /PR #12/ });
      expect(link).toHaveAttribute("href", PR.url);
      expect(screen.queryByRole("button", { name: "Open pull request" })).not.toBeInTheDocument();
    });

    it("shows the linked pull request instead of the button", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([completedWorkflow({ pull_request: PR })]);

      render(<SpecPanel workspace={workspace} />);

      expect(await screen.findByRole("link", { name: /PR #12/ })).toHaveAttribute("href", PR.url);
      expect(screen.queryByRole("button", { name: "Open pull request" })).not.toBeInTheDocument();
    });

    it("shows why a pull request could not be opened", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([completedWorkflow()]);
      vi.mocked(openSpecPullRequest).mockRejectedValue(
        new ApiError("Connect repository credentials before running this operation", 409),
      );
      const user = userEvent.setup();

      render(<SpecPanel workspace={workspace} />);

      await user.click(await screen.findByRole("button", { name: "Open pull request" }));

      expect(
        await screen.findByText("Connect repository credentials before running this operation"),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Open pull request" })).toBeEnabled();
    });

    it("offers no pull request before the workflow is completed", async () => {
      vi.mocked(listSpecWorkflows).mockResolvedValue([
        makeWorkflow("specify", { specify: "awaiting_review" }),
      ]);

      render(<SpecPanel workspace={workspace} />);

      await waitFor(() => expect(screen.getAllByText("Add SSO login").length).toBeGreaterThan(0));
      expect(screen.queryByRole("button", { name: "Open pull request" })).not.toBeInTheDocument();
    });
  });
});
