import { afterEach, describe, expect, it, vi } from "vitest";

import { getCurrentUser, getHealth, login, logout, register } from "./api";

describe("getHealth", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns successful health payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ status: "ok", database: "ok", redis: "ok" }),
      }),
    );

    await expect(getHealth()).resolves.toEqual({ status: "ok", database: "ok", redis: "ok" });
  });

  it("returns degraded health payloads from 503 responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({ status: "degraded", database: "ok", redis: "error" }),
      }),
    );

    await expect(getHealth()).resolves.toEqual({
      status: "degraded",
      database: "ok",
      redis: "error",
    });
  });

  it("sends credentialed auth requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id: "fb6cd934-74f2-404b-bff1-30f964e9326c",
        email: "user@example.com",
        name: null,
        role: "viewer",
        is_active: true,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await login({ email: "user@example.com", password: "password-one" });
    await register({ email: "new@example.com", password: "password-two", name: "New User" });
    await getCurrentUser();

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8000/auth/login",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8000/auth/register",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://localhost:8000/auth/me",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("clears the session through logout", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(logout()).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/auth/logout",
      expect.objectContaining({ credentials: "include", method: "POST" }),
    );
  });
});
