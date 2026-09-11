import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LlmSettingsPanel } from "./index";
import { AuthContext, type AuthContextValue } from "../../auth/context";
import type { LlmSettingsResponse, UserResponse, UserRole } from "../../lib/api";
import { LlmStatusContext, type LlmStatusValue } from "../../platform/llmStatus";

const REAL_KEY = "sk-secret-value";

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response;
}

function settingsResponse(overrides: Partial<LlmSettingsResponse> = {}): LlmSettingsResponse {
  return {
    base_url: "http://llm.example/v1",
    model: "test-model",
    has_api_key: true,
    masked_api_key: "********",
    source: "database",
    updated_at: "2026-09-11T00:00:00Z",
    ...overrides,
  };
}

/**
 * Routes by URL with sane defaults, so each test only overrides the endpoint it asserts on.
 * Mirrors the `stubFetch` idiom in `EnvVarEditor.test.tsx` -- there is no MSW in this repo.
 */
function stubFetch(handler: (url: string, init?: RequestInit) => Response | undefined) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handled = handler(url, init);
    if (handled) return Promise.resolve(handled);
    if (url.endsWith("/platform/llm-settings")) {
      return Promise.resolve(jsonResponse(settingsResponse()));
    }
    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function renderPanel({
  role = "owner",
  variant = "settings",
  status,
}: {
  role?: UserRole;
  variant?: "settings" | "onboarding";
  status?: Partial<LlmStatusValue>;
} = {}) {
  const auth = {
    status: "authenticated",
    user: { id: "u1", email: "a@b.c", name: null, role, is_active: true } as UserResponse,
    login: async () => undefined,
    register: async () => undefined,
    logout: async () => undefined,
  } as AuthContextValue;

  const llmStatus: LlmStatusValue = {
    state: "configured",
    source: "database",
    baseUrl: "http://llm.example/v1",
    model: "test-model",
    refresh: async () => undefined,
    markConfigured: () => undefined,
    ...status,
  };

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter>
        <AuthContext.Provider value={auth}>
          <LlmStatusContext.Provider value={llmStatus}>{children}</LlmStatusContext.Provider>
        </AuthContext.Provider>
      </MemoryRouter>
    );
  }

  return render(<LlmSettingsPanel variant={variant} />, { wrapper: Wrapper });
}

function bodyOf(mock: ReturnType<typeof stubFetch>, path: string, method: string) {
  const call = mock.mock.calls.find(
    ([url, init]) =>
      String(url).endsWith(path) && (init as RequestInit | undefined)?.method === method,
  );
  if (!call) return null;
  return JSON.parse(String((call[1] as RequestInit).body));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LlmSettingsPanel", () => {
  it("lets the platform owner edit the configuration", async () => {
    stubFetch(() => undefined);
    renderPanel();

    expect(await screen.findByLabelText("Base URL")).toHaveValue("http://llm.example/v1");
    expect(screen.getByRole("button", { name: "Save settings" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test connection" })).toBeInTheDocument();
  });

  it("locks the card for a non-owner and never calls the owner-only endpoint", async () => {
    const fetchMock = stubFetch(() => undefined);
    renderPanel({ role: "viewer" });

    expect(
      await screen.findByText("Only the platform owner can change these settings."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Test connection" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();

    const requested = fetchMock.mock.calls.map(([url]) => String(url));
    expect(requested.some((url) => url.endsWith("/platform/llm-settings"))).toBe(false);
  });

  it("never prefills a stored API key", async () => {
    stubFetch(() => undefined);
    renderPanel();

    expect(await screen.findByText("********")).toBeInTheDocument();
    expect(screen.queryByLabelText("New API key")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Replace key" }));
    expect(screen.getByLabelText("New API key")).toHaveValue("");
  });

  it("omits api_key when the key was not replaced", async () => {
    const fetchMock = stubFetch((url, init) =>
      url.endsWith("/platform/llm-settings") && init?.method === "PUT"
        ? jsonResponse(settingsResponse({ model: "gpt-4.1-nano" }))
        : undefined,
    );
    renderPanel();

    const modelField = await screen.findByLabelText("Model");
    await userEvent.clear(modelField);
    await userEvent.type(modelField, "gpt-4.1-nano");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(screen.getByText("LLM settings saved.")).toBeInTheDocument());
    const body = bodyOf(fetchMock, "/platform/llm-settings", "PUT");
    expect(body).not.toHaveProperty("api_key");
    expect(body).toMatchObject({ base_url: "http://llm.example/v1", model: "gpt-4.1-nano" });
  });

  it("sends the new key once it has been replaced", async () => {
    const fetchMock = stubFetch((url, init) =>
      url.endsWith("/platform/llm-settings") && init?.method === "PUT"
        ? jsonResponse(settingsResponse())
        : undefined,
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Replace key" }));
    await userEvent.type(screen.getByLabelText("New API key"), REAL_KEY);
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() =>
      expect(bodyOf(fetchMock, "/platform/llm-settings", "PUT")).toMatchObject({
        api_key: REAL_KEY,
      }),
    );
  });

  it("surfaces the backend detail when saving fails", async () => {
    stubFetch((url, init) =>
      url.endsWith("/platform/llm-settings") && init?.method === "PUT"
        ? jsonResponse({ detail: "Base URL must start with http:// or https://." }, 400)
        : undefined,
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Save settings" }));

    expect(
      await screen.findByText("Base URL must start with http:// or https://."),
    ).toBeInTheDocument();
    expect(screen.queryByText("LLM settings saved.")).not.toBeInTheDocument();
  });

  it("reports a successful test and offers the returned models", async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith("/platform/llm-settings/test")
        ? jsonResponse({
            ok: true,
            message: "Connected. 2 model(s) available.",
            models: ["alpha", "beta"],
            latency_ms: 42,
          })
        : undefined,
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("Connected. 2 model(s) available.")).toBeInTheDocument();
    expect(screen.getByText("42 ms")).toBeInTheDocument();

    // The probe must carry the draft values, so it works before anything is saved.
    expect(bodyOf(fetchMock, "/platform/llm-settings/test", "POST")).toMatchObject({
      base_url: "http://llm.example/v1",
    });

    const select = screen.getByRole("combobox", { name: "Model" });
    expect(select).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "alpha" })).toBeInTheDocument();
  });

  it("explains the container localhost trap when a local endpoint is refused", async () => {
    stubFetch((url) =>
      url.endsWith("/platform/llm-settings")
        ? jsonResponse(settingsResponse({ base_url: "http://localhost:11434/v1" }))
        : url.endsWith("/platform/llm-settings/test")
          ? jsonResponse({
              ok: false,
              message: "Could not reach the endpoint. Check the base URL.",
              models: null,
              latency_ms: null,
            })
          : undefined,
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText(/host\.containers\.internal/)).toBeInTheDocument();
  });

  it("clears a stale test result as soon as a field changes", async () => {
    stubFetch((url) =>
      url.endsWith("/platform/llm-settings/test")
        ? jsonResponse({ ok: true, message: "Connected.", models: null, latency_ms: 10 })
        : undefined,
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Connected.")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Base URL"), "x");
    expect(screen.queryByText("Connected.")).not.toBeInTheDocument();
  });

  it("fills the base URL from a provider preset", async () => {
    stubFetch((url) =>
      url.endsWith("/platform/llm-settings")
        ? jsonResponse(settingsResponse({ base_url: "", model: "", has_api_key: false }))
        : undefined,
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Ollama" }));

    expect(screen.getByLabelText("Base URL")).toHaveValue("http://localhost:11434/v1");
    // The preset seeds a model only when the field is empty.
    expect(screen.getByLabelText("Model")).toHaveValue("qwen2.5-coder:7b");
  });

  it("shows an unconfigured pill and no danger zone when nothing is stored", async () => {
    stubFetch((url) =>
      url.endsWith("/platform/llm-settings")
        ? jsonResponse(
            settingsResponse({
              base_url: null,
              model: null,
              has_api_key: false,
              masked_api_key: null,
              source: null,
              updated_at: null,
            }),
          )
        : undefined,
    );
    renderPanel({ status: { state: "unconfigured", source: null, baseUrl: null, model: null } });

    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Clear configuration" }),
    ).not.toBeInTheDocument();
    // A keyless local endpoint is legitimate, so the key field is offered directly.
    expect(screen.getByLabelText("API key")).toHaveValue("");
  });

  it("labels the primary action for the onboarding variant", async () => {
    stubFetch(() => undefined);
    renderPanel({ variant: "onboarding" });

    expect(
      await screen.findByRole("button", { name: "Save and continue" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save settings" })).not.toBeInTheDocument();
  });
});
