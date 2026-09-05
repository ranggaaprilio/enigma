import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Budget } from "../../lib/api";
import { BudgetConfig } from "./BudgetConfig";

function jsonResponse(data: unknown) {
  return { ok: true, json: async () => data };
}

function makeBudget(overrides: Partial<Budget> = {}): Budget {
  return {
    id: "b1",
    scope: "workspace",
    scope_id: "ws-1",
    period: "monthly",
    limit_cost: 10,
    limit_tokens: null,
    threshold_percent: 80,
    enforce: false,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    current_cost: 6,
    current_tokens: 1200,
    status: "warning",
    ...overrides,
  };
}

describe("BudgetConfig", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows empty state and a create form when the user can manage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([])));

    render(<BudgetConfig scope="workspace" scopeId="ws-1" canManage />);

    await waitFor(() =>
      expect(screen.getByText(/no budgets configured/i)).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /add budget/i })).toBeInTheDocument();
  });

  it("hides management controls when the user cannot manage", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse([makeBudget()])));

    render(<BudgetConfig scope="workspace" scopeId="ws-1" canManage={false} />);

    await waitFor(() => expect(screen.getByText("$6.00 / $10.00")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /delete/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add budget/i })).not.toBeInTheDocument();
  });

  it("renders a budget row with its status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse([makeBudget({ status: "over", enforce: true })])),
    );

    render(<BudgetConfig scope="workspace" scopeId="ws-1" canManage />);

    await waitFor(() => expect(screen.getByText("over")).toBeInTheDocument());
    expect(screen.getByText("Enforced")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /delete/i })).toBeInTheDocument();
  });

  it("creates a budget through the form", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([])) // initial list
      .mockResolvedValueOnce(jsonResponse(makeBudget())) // POST create
      .mockResolvedValueOnce(jsonResponse([makeBudget()])); // refetch
    vi.stubGlobal("fetch", fetchMock);

    render(<BudgetConfig scope="workspace" scopeId="ws-1" canManage />);

    await waitFor(() =>
      expect(screen.getByText(/no budgets configured/i)).toBeInTheDocument(),
    );

    await userEvent.type(screen.getByPlaceholderText("50.00"), "10");
    await userEvent.click(screen.getByRole("button", { name: /add budget/i }));

    await waitFor(() => expect(screen.getByText("$6.00 / $10.00")).toBeInTheDocument());

    const postCall = fetchMock.mock.calls.find((call) => call[1]?.method === "POST");
    expect(postCall).toBeTruthy();
    expect(postCall?.[0]).toContain("/budgets");
  });
});
