export type HealthResponse = {
  status: "ok" | "degraded";
  database: "ok" | "error";
  redis: "ok" | "error";
};

const fallbackApiBaseUrl = "http://localhost:8000";

export function getApiBaseUrl() {
  return import.meta.env.VITE_API_BASE_URL ?? fallbackApiBaseUrl;
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch(`${getApiBaseUrl()}/health`, { signal });
  const payload = (await response.json()) as HealthResponse;

  if (!response.ok && response.status !== 503) {
    throw new Error("Health check failed");
  }

  return payload;
}
