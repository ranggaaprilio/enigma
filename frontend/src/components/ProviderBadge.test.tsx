import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProviderBadge } from "./ProviderBadge";

describe("ProviderBadge", () => {
  it("names the workspace's provider", () => {
    const { rerender } = render(<ProviderBadge runtime="opensandbox" />);
    expect(screen.getByText("OpenSandbox")).toBeInTheDocument();

    rerender(<ProviderBadge runtime="daytona" />);
    expect(screen.getByText("Daytona")).toBeInTheDocument();
  });

  it("shows a provider it does not know by its own name (a workspace left from v0.1.0)", () => {
    render(<ProviderBadge runtime="docker" />);
    expect(screen.getByText("docker")).toBeInTheDocument();
  });

  it("renders nothing without a runtime", () => {
    const { container } = render(<ProviderBadge runtime={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });
});
