export type HealthResponse = {
  status: "ok" | "degraded";
  database: "ok" | "error";
  redis: "ok" | "error";
};

export type UserRole = "owner" | "developer" | "viewer";

export type UserResponse = {
  id: string;
  email: string;
  name: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type LoginPayload = {
  email: string;
  password: string;
};

export type RegisterPayload = LoginPayload & {
  name?: string;
};

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  const payload = (await response.json().catch(() => null)) as { detail?: string } | T | null;

  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && "detail" in payload && payload.detail
        ? payload.detail
        : "Request failed";
    throw new ApiError(message, response.status);
  }

  return payload as T;
}

export function getCurrentUser(signal?: AbortSignal): Promise<UserResponse> {
  return requestJson<UserResponse>("/auth/me", { signal });
}

export function login(payload: LoginPayload): Promise<UserResponse> {
  return requestJson<UserResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function register(payload: RegisterPayload): Promise<UserResponse> {
  return requestJson<UserResponse>("/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function logout(): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>("/auth/logout", { method: "POST" });
}
