import { afterEach, describe, expect, it, vi } from "vitest";

import { getHealth } from "./api";

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
});
