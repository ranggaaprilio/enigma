import type { HealthResponse, RuntimeName } from "./api";

export const RUNTIMES: RuntimeName[] = ["daytona", "opensandbox"];

const LABELS: Record<RuntimeName, string> = {
  daytona: "Daytona",
  opensandbox: "OpenSandbox",
};
const DESCRIPTIONS: Record<RuntimeName, string> = {
  daytona: "Cloud sandbox",
  opensandbox: "Sandbox managed by an OpenSandbox server",
};

/** The user-facing name of a sandbox provider (`Workspace.runtime`). A provider this build
 * does not know (a `docker` workspace left from v0.1.0) is shown by its own name. */
export function runtimeLabel(runtime: string | null | undefined): string {
  if (!runtime) return "Sandbox";
  return runtime in LABELS ? LABELS[runtime as RuntimeName] : runtime;
}

export type RuntimeOption = {
  value: RuntimeName;
  label: string;
  description: string;
  available: boolean;
  reason: string | null;
};

/** The create form's choices, from `/health`. Unknown health yields none: the server picks.
 * A provider the operator disabled (`SANDBOX_RUNTIMES_ALLOWED`) is not offered at all. */
export function runtimeOptions(health: HealthResponse | null): RuntimeOption[] {
  if (!health?.runtimes) return [];
  const offered = RUNTIMES.filter((value) => health.runtimes[value]?.status !== "disabled");
  return offered.map((value) => {
    const entry = health.runtimes[value];
    return {
      value,
      label: LABELS[value],
      description: DESCRIPTIONS[value],
      available: entry?.status === "ok",
      reason: entry?.status === "ok" ? null : (entry?.reason ?? "Unavailable"),
    };
  });
}

/** The server default when it is available, else the other provider if that one is. */
export function defaultRuntime(health: HealthResponse | null): RuntimeName | null {
  const options = runtimeOptions(health);
  const preferred = options.find((o) => o.value === health?.sandbox_runtime_default);
  if (preferred?.available) return preferred.value;
  return options.find((o) => o.available)?.value ?? null;
}

/** A next step for a workspace in `error`, when the provider suggests one. */
export function runtimeErrorHint(runtime: string | null | undefined): string | null {
  if (runtime === "opensandbox") return "Is the OpenSandbox server running?";
  return null;
}
