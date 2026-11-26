import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VerificationPanel } from "./VerificationPanel";
import type {
  VerificationRunDetailResponse,
  VerificationRunResponse,
  WorkspaceResponse,
} from "../lib/api";

const permissions = vi.hoisted(() => ({ allowed: true }));
vi.mock("../auth/permissions", () => ({
  usePermissions: () => ({ can: () => permissions.allowed }),
}));

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const BASE = `/workspaces/${WORKSPACE_ID}/verification/runs`;

const workspace = {
  id: WORKSPACE_ID,
  owner_id: "22222222-2222-4222-8222-222222222222",
  name: "Demo",
  slug: "demo",
  status: "ready",
} as WorkspaceResponse;

function run(overrides: Partial<VerificationRunResponse> = {}): VerificationRunResponse {
  return {
    id: "r1",
    workspace_id: WORKSPACE_ID,
    plan_ref: "specs/001-login/plan.md",
    status: "review",
    created_at: "2026-11-26T00:00:00Z",
    updated_at: "2026-11-26T00:01:00Z",
    ...overrides,
  };
}

function detail(overrides: Partial<VerificationRunDetailResponse> = {}): VerificationRunDetailResponse {
  return {
    ...run(),
    evidence: [
      {
        id: "e1",
        kind: "test",
        status: "failed",
        detail: { exit_code: 1, command: "pytest -q" },
        path: null,
        created_at: "2026-11-26T00:00:10Z",
      },
      {
        id: "e2",
        kind: "observation",
        status: "passed",
        detail: {
          port: 5173,
          url: "https://preview.example/app",
          description: "Login page renders",
          snapshot: null,
          screenshot: "screens/r1.png",
        },
        path: "screens/r1.png",
        created_at: "2026-11-26T00:00:20Z",
      },
    ],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    headers: new Headers(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

type Handler = (url: string, init?: RequestInit) => Response | undefined;

function stubFetch(handler: Handler) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(handler(String(input), init) ?? jsonResponse({ detail: "unexpected" }, 500)),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  permissions.allowed = true;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("VerificationPanel", () => {
  it("lists runs and shows the selected run's test and observation evidence", async () => {
    stubFetch((url) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run()]);
      if (url.endsWith(`${BASE}/r1`)) return jsonResponse(detail());
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);

    expect((await screen.findAllByText("specs/001-login/plan.md")).length).toBeGreaterThan(0);
    const evidence = await screen.findByRole("region", { name: "Evidence" });
    expect(within(evidence).getByText("pytest -q")).toBeInTheDocument();
    expect(within(evidence).getByText("Exit code 1")).toBeInTheDocument();
    expect(within(evidence).getByText("Login page renders")).toBeInTheDocument();
    expect(within(evidence).getByRole("link", { name: "https://preview.example/app" })).toHaveAttribute(
      "href",
      "https://preview.example/app",
    );
    expect(within(evidence).getByText("screens/r1.png")).toBeInTheDocument();
  });

  it("shows an empty state when there are no runs", async () => {
    stubFetch((url) => (url.endsWith(`${BASE}?limit=20`) ? jsonResponse([]) : undefined));

    render(<VerificationPanel workspace={workspace} />);

    expect(await screen.findByText(/No verification runs yet/)).toBeInTheDocument();
  });

  it("triggers a run with the form values and selects the result", async () => {
    const user = userEvent.setup();
    const created = detail({ id: "r2", plan_ref: "specs/002/plan.md", evidence: [] });
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([]);
      if (url.endsWith(BASE) && init?.method === "POST") return jsonResponse(created, 201);
      if (url.endsWith(`${BASE}/r2`)) return jsonResponse(created);
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    await screen.findByText(/No verification runs yet/);

    await user.type(screen.getByLabelText("Plan reference"), "specs/002/plan.md");
    await user.type(screen.getByLabelText("Test command"), "pytest -q");
    await user.type(screen.getByLabelText("Observe port (optional)"), "5173");
    await user.click(screen.getByRole("button", { name: "Run verification" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(new RegExp(`${BASE}$`)),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const postCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      plan_ref: "specs/002/plan.md",
      test_command: "pytest -q",
      observe_port: 5173,
      max_iterations: 1,
    });
    // Shown in both the run list and the selected run's header.
    expect(await screen.findAllByText("specs/002/plan.md")).toHaveLength(2);
  });

  it("reports a conflict when a run is already active", async () => {
    const user = userEvent.setup();
    stubFetch((url, init) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([]);
      if (url.endsWith(BASE) && init?.method === "POST") {
        return jsonResponse({ detail: "Workspace already has an active verification run" }, 409);
      }
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    await screen.findByText(/No verification runs yet/);
    await user.type(screen.getByLabelText("Plan reference"), "p");
    await user.type(screen.getByLabelText("Test command"), "t");
    await user.click(screen.getByRole("button", { name: "Run verification" }));

    expect(
      await screen.findByText("Workspace already has an active verification run"),
    ).toBeInTheDocument();
  });

  it("approves a run awaiting review", async () => {
    const user = userEvent.setup();
    let status: VerificationRunResponse["status"] = "review";
    stubFetch((url, init) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run({ status })]);
      if (url.endsWith(`${BASE}/r1/approve`) && init?.method === "POST") {
        status = "approved";
        return jsonResponse(run({ status }));
      }
      if (url.endsWith(`${BASE}/r1`)) return jsonResponse(detail({ status }));
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    await user.click(await screen.findByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument(),
    );
    expect(screen.getAllByText("approved").length).toBeGreaterThan(0);
  });

  it("rejects a run awaiting review", async () => {
    const user = userEvent.setup();
    const fetchMock = stubFetch((url, init) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run()]);
      if (url.endsWith(`${BASE}/r1/reject`) && init?.method === "POST") {
        return jsonResponse(run({ status: "rejected" }));
      }
      if (url.endsWith(`${BASE}/r1`)) return jsonResponse(detail());
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    await user.click(await screen.findByRole("button", { name: "Reject" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/r1\/reject$/),
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("hides the gate for runs that are not awaiting review", async () => {
    stubFetch((url) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run({ status: "approved" })]);
      if (url.endsWith(`${BASE}/r1`)) return jsonResponse(detail({ status: "approved" }));
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    await screen.findByRole("region", { name: "Evidence" });

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("explains the missing permission instead of calling the API", async () => {
    // Every verification endpoint requires drive access, so a viewer would only get 403s.
    permissions.allowed = false;
    const fetchMock = stubFetch(() => undefined);

    render(<VerificationPanel workspace={workspace} />);

    expect(screen.getByText(/You need permission to run agents/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run verification" })).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("never gates the previous run after switching selection", async () => {
    const user = userEvent.setup();
    let releaseR2: (() => void) | undefined;
    const r2 = run({ id: "r2", plan_ref: "specs/r2/plan.md" });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith(`${BASE}?limit=20`)) return Promise.resolve(jsonResponse([run(), r2]));
      if (url.endsWith(`${BASE}/r1`)) return Promise.resolve(jsonResponse(detail()));
      if (url.endsWith(`${BASE}/r2`)) {
        // Hold r2's detail so the old run's detail is still in state while r2 is selected.
        return new Promise<Response>((resolve) => {
          releaseR2 = () => resolve(jsonResponse(detail({ id: "r2", plan_ref: "specs/r2/plan.md" })));
        });
      }
      return Promise.resolve(jsonResponse({ detail: "unexpected" }, 500));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<VerificationPanel workspace={workspace} />);
    await screen.findByRole("button", { name: "Approve" });

    await user.click(screen.getByRole("button", { name: /specs\/r2\/plan\.md/ }));

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByText("Loading run…")).toBeInTheDocument();

    releaseR2?.();
    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getAllByText("specs/r2/plan.md")).toHaveLength(2);
  });

  it("refreshes the run after a gate conflict", async () => {
    const user = userEvent.setup();
    let detailCalls = 0;
    stubFetch((url, init) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run()]);
      if (url.endsWith(`${BASE}/r1/approve`) && init?.method === "POST") {
        return jsonResponse({ detail: "Verification run is not awaiting review (status=approved)" }, 409);
      }
      if (url.endsWith(`${BASE}/r1`)) {
        detailCalls += 1;
        return jsonResponse(detail({ status: detailCalls === 1 ? "review" : "approved" }));
      }
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    await user.click(await screen.findByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not awaiting review");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument(),
    );
  });

  it("polls an in-flight run until it reaches the gate", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let status: VerificationRunResponse["status"] = "testing";
      let detailCalls = 0;
      stubFetch((url) => {
        if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run({ status: "testing" })]);
        if (url.endsWith(`${BASE}/r1`)) {
          detailCalls += 1;
          return jsonResponse(detail({ status, evidence: [] }));
        }
        return undefined;
      });

      render(<VerificationPanel workspace={workspace} />);
      await screen.findByText("Loading run…").catch(() => undefined);
      await waitFor(() => expect(detailCalls).toBe(1));

      status = "fixing";
      await act(() => vi.advanceTimersByTimeAsync(3_000));
      await waitFor(() => expect(detailCalls).toBe(2));
      expect((await screen.findAllByText("fixing")).length).toBeGreaterThan(0);

      status = "review";
      await act(() => vi.advanceTimersByTimeAsync(3_000));
      expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();

      // Parked at the gate: no more polling.
      await act(() => vi.advanceTimersByTimeAsync(10_000));
      expect(detailCalls).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows fix attempts and the failing test output", async () => {
    const user = userEvent.setup();
    stubFetch((url) => {
      if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run()]);
      if (url.endsWith(`${BASE}/r1`)) {
        return jsonResponse(
          detail({
            evidence: [
              {
                id: "t1",
                kind: "test",
                status: "failed",
                detail: {
                  command: "pytest -q",
                  exit_code: 1,
                  output: "E   AssertionError: boom",
                  truncated: true,
                },
                path: null,
                created_at: "2026-11-26T00:00:10Z",
              },
              {
                id: "f1",
                kind: "fix",
                status: "passed",
                detail: { agent_run_id: "a1", agent_status: "completed" },
                path: null,
                created_at: "2026-11-26T00:00:20Z",
              },
              {
                id: "f2",
                kind: "fix",
                status: "error",
                detail: { error: "an enforcing budget is over its limit" },
                path: null,
                created_at: "2026-11-26T00:00:30Z",
              },
            ],
          }),
        );
      }
      return undefined;
    });

    render(<VerificationPanel workspace={workspace} />);
    const evidence = await screen.findByRole("region", { name: "Evidence" });

    await user.click(within(evidence).getByText("Output (tail)"));
    expect(within(evidence).getByText(/AssertionError: boom/)).toBeVisible();
    expect(within(evidence).getByText("Agent run completed")).toBeInTheDocument();
    expect(within(evidence).getByText("an enforcing budget is over its limit")).toBeInTheDocument();
  });

  it("keeps polling after a failed refresh", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let detailCalls = 0;
      stubFetch((url) => {
        if (url.endsWith(`${BASE}?limit=20`)) return jsonResponse([run({ status: "testing" })]);
        if (url.endsWith(`${BASE}/r1`)) {
          detailCalls += 1;
          if (detailCalls === 2) return jsonResponse({ detail: "Bad gateway" }, 502);
          return jsonResponse(detail({ status: detailCalls >= 3 ? "review" : "testing", evidence: [] }));
        }
        return undefined;
      });

      render(<VerificationPanel workspace={workspace} />);
      await waitFor(() => expect(detailCalls).toBe(1));

      await act(() => vi.advanceTimersByTimeAsync(3_000)); // fails
      expect(await screen.findByRole("alert")).toHaveTextContent("Bad gateway");
      await act(() => vi.advanceTimersByTimeAsync(3_000)); // retried
      expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});
