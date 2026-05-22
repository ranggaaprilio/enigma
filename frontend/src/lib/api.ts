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

export type WorkspaceStatus = "creating" | "ready" | "busy" | "error" | "stopped";

export type WorkspaceResponse = {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  repo_url: string;
  default_branch: string;
  local_path: string | null;
  status: WorkspaceStatus;
  created_at: string;
  updated_at: string;
};

export type WorkspaceStatusResponse = Pick<
  WorkspaceResponse,
  "id" | "status" | "local_path" | "created_at" | "updated_at"
>;

export type WorkspaceCreatePayload = {
  name: string;
  repo_url: string;
  default_branch?: string;
};

export type WorkspaceUpdatePayload = Partial<WorkspaceCreatePayload>;

export type WorkspaceEnvVarResponse = {
  id: string;
  key: string;
  masked_value: string;
  created_at: string;
  updated_at: string;
};

export type WorkspaceEnvVarPayload = {
  key: string;
  value: string;
};

export type RepositoryConnectPayload = {
  repo_url: string;
  token: string;
};

export type RepositoryFileStatus = {
  path: string;
  status: string;
};

export type RepositoryStatusResponse = {
  cloned: boolean;
  branch: string | null;
  is_clean: boolean;
  files: RepositoryFileStatus[];
};

export type RepositoryDiffResponse = {
  diff: string;
};

export type RepositoryCommitPayload = {
  message: string;
};

export type RepositoryOperationResponse = {
  ok: boolean;
  message: string;
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

export function listWorkspaces(signal?: AbortSignal): Promise<WorkspaceResponse[]> {
  return requestJson<WorkspaceResponse[]>("/workspaces", { signal });
}

export function createWorkspace(payload: WorkspaceCreatePayload): Promise<WorkspaceResponse> {
  return requestJson<WorkspaceResponse>("/workspaces", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateWorkspace(
  workspaceId: string,
  payload: WorkspaceUpdatePayload,
): Promise<WorkspaceResponse> {
  return requestJson<WorkspaceResponse>(`/workspaces/${workspaceId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteWorkspace(workspaceId: string): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/workspaces/${workspaceId}`, { method: "DELETE" });
}

export function getWorkspaceStatus(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<WorkspaceStatusResponse> {
  return requestJson<WorkspaceStatusResponse>(`/workspaces/${workspaceId}/status`, { signal });
}

export function listWorkspaceEnvVars(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<WorkspaceEnvVarResponse[]> {
  return requestJson<WorkspaceEnvVarResponse[]>(`/workspaces/${workspaceId}/env-vars`, { signal });
}

export function upsertWorkspaceEnvVar(
  workspaceId: string,
  payload: WorkspaceEnvVarPayload,
): Promise<WorkspaceEnvVarResponse> {
  return requestJson<WorkspaceEnvVarResponse>(`/workspaces/${workspaceId}/env-vars`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteWorkspaceEnvVar(workspaceId: string, key: string): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(
    `/workspaces/${workspaceId}/env-vars?key=${encodeURIComponent(key)}`,
    { method: "DELETE" },
  );
}

export function connectRepository(
  workspaceId: string,
  payload: RepositoryConnectPayload,
): Promise<WorkspaceResponse> {
  return requestJson<WorkspaceResponse>(`/workspaces/${workspaceId}/repo/connect`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function cloneRepository(workspaceId: string): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(`/workspaces/${workspaceId}/repo/clone`, {
    method: "POST",
  });
}

export function getRepositoryStatus(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<RepositoryStatusResponse> {
  return requestJson<RepositoryStatusResponse>(`/workspaces/${workspaceId}/repo/status`, {
    signal,
  });
}

export function getRepositoryDiff(
  workspaceId: string,
  path?: string,
  signal?: AbortSignal,
): Promise<RepositoryDiffResponse> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return requestJson<RepositoryDiffResponse>(`/workspaces/${workspaceId}/repo/diff${query}`, {
    signal,
  });
}

export function pullRepository(workspaceId: string): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(`/workspaces/${workspaceId}/repo/pull`, {
    method: "POST",
  });
}

export function commitRepository(
  workspaceId: string,
  payload: RepositoryCommitPayload,
): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(`/workspaces/${workspaceId}/repo/commit`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function pushRepository(workspaceId: string): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(`/workspaces/${workspaceId}/repo/push`, {
    method: "POST",
  });
}
