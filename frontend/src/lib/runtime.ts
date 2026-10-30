import type { HealthResponse, RuntimeName } from "./api";

export const RUNTIMES: RuntimeName[] = ["daytona", "docker"];

const LABELS: Record<RuntimeName, string> = { daytona: "Daytona", docker: "Local Docker" };
const DESCRIPTIONS: Record<RuntimeName, string> = {
  daytona: "Cloud sandbox",
  docker: "Container on this server's Docker daemon",
};

/** The user-facing name of a sandbox provider (`Workspace.runtime`). */
export function runtimeLabel(runtime: string | null | undefined): string {
  return runtime && runtime in LABELS ? LABELS[runtime as RuntimeName] : "Sandbox";
}

export type RuntimeOption = {
  value: RuntimeName;
  label: string;
  description: string;
  available: boolean;
  reason: string | null;
};

/** The create form's choices, from `/health`. Unknown health yields none: the server picks. */
export function runtimeOptions(health: HealthResponse | null): RuntimeOption[] {
  if (!health?.runtimes) return [];
  return RUNTIMES.map((value) => {
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
  return runtime === "docker" ? "Is the Docker daemon running?" : null;
}
