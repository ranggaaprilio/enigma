import { describe, expect, it } from "vitest";

import type { HealthResponse, RuntimeHealth } from "./api";
import { defaultRuntime, runtimeErrorHint, runtimeLabel, runtimeOptions } from "./runtime";

function entry(status: RuntimeHealth["status"], reason: string | null = null): RuntimeHealth {
  return {
    configured: status !== "unconfigured",
    status,
    reason,
    managed_containers: null,
    upkeep_last_pass_at: null,
  };
}

function health(
  daytona: RuntimeHealth,
  docker: RuntimeHealth,
  sandboxDefault: "daytona" | "docker" = "daytona",
): HealthResponse {
  return {
    status: "ok",
    database: "ok",
    redis: "ok",
    daytona: daytona.status,
    sandbox_runtime_default: sandboxDefault,
    runtimes: { daytona, docker },
  };
}

describe("runtimeLabel", () => {
  it("names each provider and falls back for anything else", () => {
    expect(runtimeLabel("daytona")).toBe("Daytona");
    expect(runtimeLabel("docker")).toBe("Local Docker");
    expect(runtimeLabel(undefined)).toBe("Sandbox");
  });
});

describe("runtimeOptions", () => {
  it("offers both providers when both are available", () => {
    const options = runtimeOptions(health(entry("ok"), entry("ok")));
    expect(options.map((o) => [o.value, o.available, o.reason])).toEqual([
      ["daytona", true, null],
      ["docker", true, null],
    ]);
  });

  it("disables an unavailable provider and says why", () => {
    const options = runtimeOptions(
      health(entry("unconfigured", "No Daytona API key is configured"), entry("ok")),
    );
    expect(options[0]).toMatchObject({
      value: "daytona",
      available: false,
      reason: "No Daytona API key is configured",
    });
    expect(options[1]).toMatchObject({ value: "docker", available: true });
  });

  it("marks neither available when both are down", () => {
    const options = runtimeOptions(
      health(entry("unconfigured", "no key"), entry("error", "daemon down")),
    );
    expect(options.every((o) => !o.available)).toBe(true);
  });

  it("returns no options when health is unknown, leaving the choice to the server", () => {
    expect(runtimeOptions(null)).toEqual([]);
  });
});

describe("defaultRuntime", () => {
  it("selects the server default when it is available", () => {
    const both = health(entry("ok"), entry("ok"), "docker");
    expect(defaultRuntime(both)).toBe("docker");
  });

  it("selects the other provider when the default is unavailable", () => {
    expect(defaultRuntime(health(entry("unconfigured"), entry("ok")))).toBe("docker");
  });

  it("selects nothing when neither or nothing is known", () => {
    expect(defaultRuntime(health(entry("unconfigured"), entry("error")))).toBeNull();
    expect(defaultRuntime(null)).toBeNull();
  });
});

describe("runtimeErrorHint", () => {
  it("points a Docker workspace at the daemon", () => {
    expect(runtimeErrorHint("docker")).toBe("Is the Docker daemon running?");
    expect(runtimeErrorHint("daytona")).toBeNull();
  });
});
