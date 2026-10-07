import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ComposedServiceResponse } from "../../lib/api";
import { EntryPointCard } from "./EntryPointCard";
import { HealthBadge } from "./HealthBadge";
import { PreviewUrlLabel } from "./PreviewUrlLabel";

function service(overrides: Partial<ComposedServiceResponse> = {}): ComposedServiceResponse {
  return {
    service_id: "svc-1",
    custom_command_id: null,
    name: "Web",
    role: "dev",
    kind: "service",
    port: 5173,
    status: "running",
    health_status: "unknown",
    health_checked_at: null,
    is_entry_point: true,
    entry_point_source: "inferred",
    branch: "main",
    worktree_id: null,
    preview_session_id: "prev-1",
    capability_url: "https://cap.localhost:8001/",
    capability_url_rotates: true,
    alias: null,
    alias_url: null,
    alias_prime_url: null,
    localhost_url: "http://127.0.0.1:5173",
    diagnoses: [],
    ...overrides,
  };
}

describe("HealthBadge", () => {
  it("renders nothing for unknown", () => {
    const { container } = render(<HealthBadge health="unknown" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders Checking for starting", () => {
    render(<HealthBadge health="starting" />);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
  });

  it("renders Healthy for healthy", () => {
    render(<HealthBadge health="healthy" />);
    expect(screen.getByText("Healthy")).toBeInTheDocument();
  });

  it("renders Unhealthy for unhealthy", () => {
    render(<HealthBadge health="unhealthy" />);
    expect(screen.getByText("Unhealthy")).toBeInTheDocument();
  });
});

describe("PreviewUrlLabel", () => {
  it("labels a capability URL as temporary, rotating hourly", () => {
    render(<PreviewUrlLabel kind="capability" />);
    expect(screen.getByText(/Temporary link.*rotates hourly/)).toBeInTheDocument();
  });

  it("labels an alias URL as stable", () => {
    render(<PreviewUrlLabel kind="alias" />);
    expect(screen.getByText(/Stable link/)).toBeInTheDocument();
  });
});

describe("EntryPointCard", () => {
  it("shows an Open app button when the service is running with a URL", () => {
    const onOpen = vi.fn();
    render(<EntryPointCard service={service()} isSelected={false} onOpen={onOpen} />);

    const button = screen.getByRole("button", { name: "Open app" });
    expect(button).toBeEnabled();
  });

  it("disables Open app when the service is not running", () => {
    render(
      <EntryPointCard
        service={service({ status: "stopped", capability_url: null })}
        isSelected={false}
        onOpen={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Open app" })).toBeDisabled();
  });

  it("shows Showing instead of Open app once selected, and calls onOpen when clicked", async () => {
    const onOpen = vi.fn();
    const { rerender } = render(
      <EntryPointCard service={service()} isSelected={false} onOpen={onOpen} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Open app" }));
    expect(onOpen).toHaveBeenCalledTimes(1);

    rerender(<EntryPointCard service={service()} isSelected={true} onOpen={onOpen} />);
    expect(screen.getByRole("button", { name: "Showing" })).toBeInTheDocument();
  });

  it("prefers the stable alias URL over the rotating capability URL for the label and copy", () => {
    render(
      <EntryPointCard
        service={service({ alias_url: "https://demo-dev-abc123.localhost:8001/" })}
        isSelected={false}
        onOpen={vi.fn()}
      />,
    );

    expect(screen.getByText(/Stable link/)).toBeInTheDocument();
  });

  it("labels a capability-only URL as temporary", () => {
    render(<EntryPointCard service={service()} isSelected={false} onOpen={vi.fn()} />);

    expect(screen.getByText(/Temporary link.*rotates hourly/)).toBeInTheDocument();
  });

  it("shows the health badge alongside the status pill", () => {
    render(
      <EntryPointCard
        service={service({ health_status: "unhealthy" })}
        isSelected={false}
        onOpen={vi.fn()}
      />,
    );

    expect(screen.getByText("Unhealthy")).toBeInTheDocument();
  });
});
