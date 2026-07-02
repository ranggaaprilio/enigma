import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UsageChart } from "./UsageChart";

describe("UsageChart", () => {
  it("shows an empty message when there are no points", () => {
    render(<UsageChart points={[]} ariaLabel="Cost per day" />);
    expect(screen.getByText(/no usage in this period/i)).toBeInTheDocument();
  });

  it("renders a bar per point with a tooltip", () => {
    render(
      <UsageChart
        ariaLabel="Cost per day"
        points={[
          { label: "Jun 10", value: 1, tooltip: "Jun 10 · $1.00" },
          { label: "Jun 11", value: 4, tooltip: "Jun 11 · $4.00" },
        ]}
      />,
    );
    expect(screen.getByRole("img", { name: "Cost per day" })).toBeInTheDocument();
    expect(screen.getByTitle("Jun 10 · $1.00")).toBeInTheDocument();
    expect(screen.getByTitle("Jun 11 · $4.00")).toBeInTheDocument();
  });
});
