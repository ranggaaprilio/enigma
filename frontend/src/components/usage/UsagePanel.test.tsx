import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { UsageSummary } from "../../lib/api";
import { UsagePanel } from "./UsagePanel";

function jsonResponse(data: unknown) {
  return { ok: true, json: async () => data };
}

const SUMMARY: UsageSummary = {
  scope: "project",
  scope_id: "p1",
  date_from: "2026-06-01T00:00:00Z",
  date_to: "2026-07-01T00:00:00Z",
  totals: {
    run_count: 12,
    input_tokens: 1000,
    output_tokens: 2000,
    tokens_used: 3000,
    estimated_cost: 4.56,
  },
  series: [
    { date: "2026-06-10T00:00:00Z", run_count: 2, tokens_used: 1000, estimated_cost: 1.5 },
    { date: "2026-06-11T00:00:00Z", run_count: 10, tokens_used: 2000, estimated_cost: 3.06 },
  ],
  breakdown_dimension: "team",
  breakdown: [
    { id: "t1", name: "Team Alpha", run_count: 5, tokens_used: 2000, estimated_cost: 3.0 },
  ],
  top_spenders: [
    { id: "u1", name: "alice@example.com", run_count: 5, tokens_used: 2000, estimated_cost: 3.0 },
  ],
};

describe("UsagePanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders totals, breakdown, and top spenders", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(SUMMARY)));

    render(<UsagePanel scope="project" scopeId="p1" />);

    await waitFor(() => expect(screen.getByText("$4.56")).toBeInTheDocument());
    expect(screen.getByText("3.0k")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText(/by team/i)).toBeInTheDocument();
    expect(screen.getByText("Team Alpha")).toBeInTheDocument();
    expect(screen.getByText(/top spenders/i)).toBeInTheDocument();
    expect(screen.getByText("alice@example.com")).toBeInTheDocument();
  });

  it("shows an error state when the request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 403,
        json: async () => ({ detail: "No access to this project" }),
      }),
    );

    render(<UsagePanel scope="project" scopeId="p1" />);

    await waitFor(() =>
      expect(screen.getByText("No access to this project")).toBeInTheDocument(),
    );
  });
});
