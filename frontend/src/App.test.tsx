import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const authUser = {
  id: "fb6cd934-74f2-404b-bff1-30f964e9326c",
  email: "owner@example.com",
  name: "Owner",
  role: "owner",
  is_active: true,
  created_at: "2026-05-22T00:00:00Z",
  updated_at: "2026-05-22T00:00:00Z",
};

function jsonResponse(payload: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => payload,
  };
}

describe("App auth routing", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.pushState({}, "", "/");
  });

  it("redirects protected dashboard routes to login when unauthenticated", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not authenticated" }, false, 401)),
    );

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Log in" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });

  it("renders the dashboard for an authenticated user", async () => {
    window.history.pushState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/auth/me")) {
          return Promise.resolve(jsonResponse(authUser));
        }
        return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
      }),
    );

    render(<App />);

    expect(await screen.findByText("AI Agent Control Room")).toBeInTheDocument();
    expect(screen.getByText("Owner")).toBeInTheDocument();
    expect(screen.getByText("owner")).toBeInTheDocument();
  });

  it("logs out and returns to the login route", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/auth/me")) {
        return Promise.resolve(jsonResponse(authUser));
      }
      if (url.endsWith("/auth/logout")) {
        return Promise.resolve(jsonResponse({ ok: true }));
      }
      return Promise.resolve(jsonResponse({ status: "ok", database: "ok", redis: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.pushState({}, "", "/");

    render(<App />);

    await screen.findByText("AI Agent Control Room");
    await userEvent.click(screen.getByRole("button", { name: "Logout" }));

    await waitFor(() => expect(window.location.pathname).toBe("/login"));
    expect(screen.getByRole("heading", { name: "Log in" })).toBeInTheDocument();
  });
});
