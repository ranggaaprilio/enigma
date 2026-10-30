import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProviderBadge } from "./ProviderBadge";

describe("ProviderBadge", () => {
  it("names the workspace's provider", () => {
    const { rerender } = render(<ProviderBadge runtime="docker" />);
    expect(screen.getByText("Local Docker")).toBeInTheDocument();

    rerender(<ProviderBadge runtime="daytona" />);
    expect(screen.getByText("Daytona")).toBeInTheDocument();
  });

  it("renders nothing without a runtime", () => {
    const { container } = render(<ProviderBadge runtime={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});
