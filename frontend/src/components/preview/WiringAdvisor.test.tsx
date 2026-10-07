import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { PreviewAdvisoryResponse } from "../../lib/api";
import { WiringAdvisor } from "./WiringAdvisor";

function advisory(overrides: Partial<PreviewAdvisoryResponse> = {}): PreviewAdvisoryResponse {
  return {
    code: "dev_proxy_missing",
    severity: "warning",
    title: "No dev proxy configured",
    detail: "The web service has no proxy to the api service.",
    framework: "vite",
    snippet: 'server: {\n  proxy: { "/api": "http://127.0.0.1:8000" }\n}',
    snippet_language: "javascript",
    target_file: "vite.config.ts",
    service_ids: [],
    ...overrides,
  };
}

describe("WiringAdvisor", () => {
  it("renders the framework and target file alongside the snippet", () => {
    render(<WiringAdvisor advisory={advisory()} onRecheck={vi.fn()} />);

    expect(screen.getByText("No dev proxy configured")).toBeInTheDocument();
    expect(screen.getByText(/vite config/)).toBeInTheDocument();
    expect(screen.getByText(/vite\.config\.ts/)).toBeInTheDocument();
    expect(screen.getByText(/server: \{/)).toBeInTheDocument();
  });

  it("does not render a snippet block when the advisory has none", () => {
    render(<WiringAdvisor advisory={advisory({ snippet: null })} onRecheck={vi.fn()} />);

    expect(screen.queryByText("Suggested config")).not.toBeInTheDocument();
  });

  it("copies the snippet to the clipboard via the CopyButton", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<WiringAdvisor advisory={advisory()} onRecheck={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith(advisory().snippet);
    await screen.findByText("Copied");
  });

  it("calls onRecheck when Re-check is clicked", async () => {
    const onRecheck = vi.fn().mockResolvedValue(undefined);
    render(<WiringAdvisor advisory={advisory()} onRecheck={onRecheck} />);

    await userEvent.click(screen.getByRole("button", { name: "Re-check" }));

    await waitFor(() => expect(onRecheck).toHaveBeenCalledTimes(1));
  });

  it("disables the Re-check button while a recheck is in flight", async () => {
    let resolveRecheck: () => void = () => {};
    const onRecheck = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRecheck = resolve;
        }),
    );
    render(<WiringAdvisor advisory={advisory()} onRecheck={onRecheck} />);

    await userEvent.click(screen.getByRole("button", { name: "Re-check" }));

    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();

    resolveRecheck();
    await waitFor(() => expect(screen.getByRole("button", { name: "Re-check" })).toBeEnabled());
  });
});
