export type HealthResponse = {
  status: "ok" | "degraded";
  database: "ok" | "error";
  redis: "ok" | "error";
  daytona: "ok" | "error";
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
export type AgentRunStatus = "thinking" | "executing" | "completed" | "error" | "stopped";
export type AgentEventType = "status" | "output_text" | "tool_call" | "tool_result" | "error";
export type CommandRunStatus = "running" | "completed" | "failed" | "error";
export type TerminalSessionStatus = "active" | "stopped" | "error";
export type EditorSessionStatus = "active" | "stopped" | "error";
export type EditorAvailability = "inactive" | EditorSessionStatus;
export type PreviewSessionStatus = "active" | "stopped" | "error";
export type PreviewAvailability = "inactive" | PreviewSessionStatus;
export type WorkspaceFileType = "file" | "directory";

export type NotificationResponse = {
  id: string;
  user_id: string;
  workspace_id: string;
  type: string;
  title: string;
  message: string;
  read_at: string | null;
  created_at: string;
};

export type AuditLogResponse = {
  id: string;
  user_id: string;
  workspace_id: string | null;
  action: string;
  target_type: string;
  target_id: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type WorkspaceResponse = {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  repo_url: string;
  default_branch: string;
  runtime: string;
  sandbox_id: string | null;
  snapshot: string | null;
  workspace_path: string;
  status: WorkspaceStatus;
  created_at: string;
  updated_at: string;
};

export type WorkspaceStatusResponse = Pick<
  WorkspaceResponse,
  | "id"
  | "status"
  | "runtime"
  | "sandbox_id"
  | "snapshot"
  | "workspace_path"
  | "created_at"
  | "updated_at"
>;

export type WorkspaceCreatePayload = {
  name: string;
  repo_url: string;
  default_branch?: string;
  snapshot?: string | null;
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

export type WorktreeResponse = {
  id: string;
  branch: string;
  path: string;
  base_ref: string;
  is_base: boolean;
  status: string;
  is_clean: boolean;
  ahead: number;
  behind: number;
};

export type WorktreeCreatePayload = {
  branch: string;
  base?: string | null;
};

export type PullRequestResponse = {
  id: string;
  worktree_id: string | null;
  number: number;
  url: string;
  state: string;
  head: string;
  base: string;
  title: string;
};

export type PullRequestCreatePayload = {
  worktree_id?: string | null;
  head?: string | null;
  base?: string | null;
  title: string;
  body?: string | null;
  draft?: boolean;
};

export type WorkspaceFileNode = {
  path: string;
  name: string;
  type: WorkspaceFileType;
  size: number | null;
  modified_at: string;
  children: WorkspaceFileNode[];
};

export type WorkspaceFileReadResponse = {
  path: string;
  name: string;
  type: "file";
  size: number;
  modified_at: string;
  content: string;
};

export type WorkspaceFileOperationResponse = {
  ok: boolean;
  path: string;
};

export type WorkspaceFileWritePayload = {
  path: string;
  content: string;
};

export type WorkspaceFolderCreatePayload = {
  path: string;
};

export type WorkspaceFileRenamePayload = {
  path: string;
  new_path: string;
};

export type AgentEventResponse = {
  id: string;
  agent_run_id: string;
  event_type: AgentEventType;
  content: string;
  metadata: Record<string, unknown>;
  tool_call_id: string | null;
  created_at: string;
};

export type AgentRunResponse = {
  id: string;
  workspace_id: string;
  user_id: string;
  prompt: string;
  status: AgentRunStatus;
  provider: string;
  model: string;
  tokens_used: number | null;
  started_at: string;
  completed_at: string | null;
  error_message: string | null;
};

export type AgentRunDetailResponse = AgentRunResponse & {
  events: AgentEventResponse[];
};

export type AgentFileChangeResponse = {
  id: string;
  path: string;
  status: "created" | "modified" | "deleted" | "renamed";
  tool_call_id: string | null;
  created_at: string;
};

export type AgentFileChangeListResponse = {
  items: AgentFileChangeResponse[];
  total: number;
  capped: boolean;
};

export type AgentRunCreatePayload = {
  prompt: string;
  worktree_id?: string | null;
};

export type CustomCommandResponse = {
  id: string;
  workspace_id: string;
  name: string;
  command: string;
  description: string | null;
  icon: string | null;
  hotkey: string | null;
  requires_confirmation: boolean;
  created_at: string;
  updated_at: string;
};

export type CustomCommandPayload = {
  name: string;
  command: string;
  description?: string | null;
  icon?: string | null;
  hotkey?: string | null;
  requires_confirmation: boolean;
};

export type CustomCommandUpdatePayload = Partial<CustomCommandPayload>;

export type CommandRunResponse = {
  id: string;
  custom_command_id: string;
  workspace_id: string;
  user_id: string;
  status: CommandRunStatus;
  exit_code: number | null;
  output: string;
  started_at: string;
  completed_at: string | null;
};

export type CommandRunCreatePayload = {
  confirmed?: boolean;
};

export type CommandRunStreamMessage =
  | { type: "output"; content: string }
  | { type: "status"; status: CommandRunStatus; exit_code: number | null };

export type TerminalSessionResponse = {
  id: string;
  workspace_id: string;
  user_id: string;
  name: string;
  pty_session_id: string;
  status: TerminalSessionStatus;
  created_at: string;
  last_active_at: string;
};

export type TerminalSessionCreatePayload = {
  name?: string;
};

export type EditorSessionResponse = {
  id: string;
  workspace_id: string;
  user_id: string;
  url: string;
  status: EditorSessionStatus;
  started_at: string;
  last_active_at: string;
};

export type EditorStatusResponse = {
  status: EditorAvailability;
  session: EditorSessionResponse | null;
};

export type PreviewSessionResponse = {
  id: string;
  workspace_id: string;
  port: number;
  public_url: string;
  status: PreviewSessionStatus;
  created_at: string;
  last_seen_at: string;
};

export type PreviewDetectResponse = {
  status: PreviewAvailability;
  session: PreviewSessionResponse | null;
};

export type DevServerStatus =
  | "idle"
  | "installing"
  | "starting"
  | "running"
  | "stopped"
  | "no-dev-task"
  | "no-config"
  | "error";

export type DevServerStatusResponse = {
  status: DevServerStatus;
  port: number | null;
  message: string | null;
};

export type DevServerActionResponse = {
  ok: boolean;
  status: DevServerStatus;
  message: string | null;
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

export function getAgentStreamUrl(workspaceId: string, runId: string) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.protocol = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  baseUrl.pathname = `/workspaces/${workspaceId}/agent/runs/${runId}/stream`;
  baseUrl.search = "";
  return baseUrl.toString();
}

export function getTerminalSessionSocketUrl(
  workspaceId: string,
  sessionId: string,
  worktreeId?: string | null,
) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.protocol = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  baseUrl.pathname = `/workspaces/${workspaceId}/terminal/sessions/${sessionId}`;
  baseUrl.search = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return baseUrl.toString();
}

export function getCommandRunStreamUrl(workspaceId: string, runId: string) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.protocol = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  baseUrl.pathname = `/workspaces/${workspaceId}/command-runs/${runId}/stream`;
  baseUrl.search = "";
  return baseUrl.toString();
}

export function getNotificationStreamUrl() {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.protocol = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  baseUrl.pathname = "/notifications/stream";
  baseUrl.search = "";
  return baseUrl.toString();
}

export function getWorkspaceFileDownloadUrl(workspaceId: string, path: string) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.pathname = `/workspaces/${workspaceId}/files/download`;
  baseUrl.search = `?path=${encodeURIComponent(path)}`;
  return baseUrl.toString();
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

export function listNotifications(signal?: AbortSignal): Promise<NotificationResponse[]> {
  return requestJson<NotificationResponse[]>("/notifications", { signal });
}

export function markNotificationRead(
  notificationId: string,
): Promise<NotificationResponse> {
  return requestJson<NotificationResponse>(`/notifications/${notificationId}/read`, {
    method: "PATCH",
  });
}

export function markAllNotificationsRead(): Promise<NotificationResponse[]> {
  return requestJson<NotificationResponse[]>("/notifications/read-all", { method: "POST" });
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

export function listAuditLogs(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<AuditLogResponse[]> {
  return requestJson<AuditLogResponse[]>(`/workspaces/${workspaceId}/audit-logs`, { signal });
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
  worktreeId?: string,
  signal?: AbortSignal,
): Promise<RepositoryStatusResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<RepositoryStatusResponse>(`/workspaces/${workspaceId}/repo/status${query}`, {
    signal,
  });
}

export function getRepositoryDiff(
  workspaceId: string,
  path?: string,
  worktreeId?: string,
  signal?: AbortSignal,
): Promise<RepositoryDiffResponse> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  if (worktreeId) params.set("worktree_id", worktreeId);
  const query = params.toString() ? `?${params.toString()}` : "";
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

export function listWorktrees(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<WorktreeResponse[]> {
  return requestJson<WorktreeResponse[]>(`/workspaces/${workspaceId}/repo/worktrees`, {
    signal,
  });
}

export function createWorktree(
  workspaceId: string,
  payload: WorktreeCreatePayload,
): Promise<WorktreeResponse> {
  return requestJson<WorktreeResponse>(`/workspaces/${workspaceId}/repo/worktrees`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function removeWorktree(
  workspaceId: string,
  worktreeId: string,
  force = false,
): Promise<RepositoryOperationResponse> {
  const query = force ? "?force=true" : "";
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/worktrees/${worktreeId}${query}`,
    { method: "DELETE" },
  );
}

export function commitWorktree(
  workspaceId: string,
  worktreeId: string,
  payload: RepositoryCommitPayload,
): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/worktrees/${worktreeId}/commit`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function pushWorktree(
  workspaceId: string,
  worktreeId: string,
): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/worktrees/${worktreeId}/push`,
    { method: "POST" },
  );
}

export function createPullRequest(
  workspaceId: string,
  payload: PullRequestCreatePayload,
): Promise<PullRequestResponse> {
  return requestJson<PullRequestResponse>(`/workspaces/${workspaceId}/repo/pull-requests`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listPullRequests(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PullRequestResponse[]> {
  return requestJson<PullRequestResponse[]>(
    `/workspaces/${workspaceId}/repo/pull-requests`,
    { signal },
  );
}

export function getPullRequest(
  workspaceId: string,
  number: number,
  signal?: AbortSignal,
): Promise<PullRequestResponse> {
  return requestJson<PullRequestResponse>(
    `/workspaces/${workspaceId}/repo/pull-requests/${number}`,
    { signal },
  );
}

export function getWorkspaceFileTree(
  workspaceId: string,
  path = "",
  signal?: AbortSignal,
): Promise<WorkspaceFileNode> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return requestJson<WorkspaceFileNode>(`/workspaces/${workspaceId}/files/tree${query}`, {
    signal,
  });
}

export function readWorkspaceFile(
  workspaceId: string,
  path: string,
  signal?: AbortSignal,
): Promise<WorkspaceFileReadResponse> {
  return requestJson<WorkspaceFileReadResponse>(
    `/workspaces/${workspaceId}/files/read?path=${encodeURIComponent(path)}`,
    { signal },
  );
}

export function writeWorkspaceFile(
  workspaceId: string,
  payload: WorkspaceFileWritePayload,
): Promise<WorkspaceFileOperationResponse> {
  return requestJson<WorkspaceFileOperationResponse>(`/workspaces/${workspaceId}/files/write`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function uploadWorkspaceFile(
  workspaceId: string,
  path: string,
  file: File | Blob,
  filename?: string,
): Promise<WorkspaceFileOperationResponse> {
  const name =
    filename ??
    (typeof File !== "undefined" && file instanceof File ? file.name : undefined) ??
    "upload.bin";
  const response = await fetch(
    `${getApiBaseUrl()}/workspaces/${workspaceId}/files/upload?path=${encodeURIComponent(
      path,
    )}&filename=${encodeURIComponent(name)}`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    },
  );
  const payload = (await response.json().catch(() => null)) as
    | { detail?: string }
    | WorkspaceFileOperationResponse
    | null;

  if (!response.ok) {
    const message =
      payload && typeof payload === "object" && "detail" in payload && payload.detail
        ? payload.detail
        : "Request failed";
    throw new ApiError(message, response.status);
  }

  return payload as WorkspaceFileOperationResponse;
}

export function createWorkspaceFolder(
  workspaceId: string,
  payload: WorkspaceFolderCreatePayload,
): Promise<WorkspaceFileOperationResponse> {
  return requestJson<WorkspaceFileOperationResponse>(`/workspaces/${workspaceId}/files/folders`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function renameWorkspaceFile(
  workspaceId: string,
  payload: WorkspaceFileRenamePayload,
): Promise<WorkspaceFileOperationResponse> {
  return requestJson<WorkspaceFileOperationResponse>(`/workspaces/${workspaceId}/files/rename`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteWorkspaceFile(workspaceId: string, path: string): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(
    `/workspaces/${workspaceId}/files?path=${encodeURIComponent(path)}`,
    { method: "DELETE" },
  );
}

export function searchWorkspaceFiles(
  workspaceId: string,
  query: string,
  signal?: AbortSignal,
): Promise<WorkspaceFileNode[]> {
  return requestJson<WorkspaceFileNode[]>(
    `/workspaces/${workspaceId}/files/search?q=${encodeURIComponent(query)}`,
    { signal },
  );
}

export function createAgentRun(
  workspaceId: string,
  payload: AgentRunCreatePayload,
): Promise<AgentRunResponse> {
  return requestJson<AgentRunResponse>(`/workspaces/${workspaceId}/agent/runs`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listAgentRuns(
  workspaceId: string,
  signal?: AbortSignal,
  limit?: number,
  offset?: number,
): Promise<AgentRunResponse[]> {
  const params = new URLSearchParams();
  if (limit !== undefined) params.set("limit", String(limit));
  if (offset !== undefined) params.set("offset", String(offset));
  const query = params.toString() ? `?${params.toString()}` : "";
  return requestJson<AgentRunResponse[]>(`/workspaces/${workspaceId}/agent/runs${query}`, { signal });
}

export function getAgentRun(
  workspaceId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<AgentRunDetailResponse> {
  return requestJson<AgentRunDetailResponse>(
    `/workspaces/${workspaceId}/agent/runs/${runId}`,
    { signal },
  );
}

export function stopAgentRun(workspaceId: string, runId: string): Promise<AgentRunResponse> {
  return requestJson<AgentRunResponse>(`/workspaces/${workspaceId}/agent/runs/${runId}/stop`, {
    method: "POST",
  });
}

export function getAgentFileChanges(
  workspaceId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<AgentFileChangeListResponse> {
  return requestJson<AgentFileChangeListResponse>(
    `/workspaces/${workspaceId}/agent/runs/${runId}/file-changes`,
    { signal },
  );
}

export function createCustomCommand(
  workspaceId: string,
  payload: CustomCommandPayload,
): Promise<CustomCommandResponse> {
  return requestJson<CustomCommandResponse>(`/workspaces/${workspaceId}/commands`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listCustomCommands(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<CustomCommandResponse[]> {
  return requestJson<CustomCommandResponse[]>(`/workspaces/${workspaceId}/commands`, { signal });
}

export function updateCustomCommand(
  workspaceId: string,
  commandId: string,
  payload: CustomCommandUpdatePayload,
): Promise<CustomCommandResponse> {
  return requestJson<CustomCommandResponse>(`/workspaces/${workspaceId}/commands/${commandId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteCustomCommand(
  workspaceId: string,
  commandId: string,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(`/workspaces/${workspaceId}/commands/${commandId}`, {
    method: "DELETE",
  });
}

export function runCustomCommand(
  workspaceId: string,
  commandId: string,
  payload: CommandRunCreatePayload = {},
): Promise<CommandRunResponse> {
  return requestJson<CommandRunResponse>(`/workspaces/${workspaceId}/commands/${commandId}/run`, {
    method: "POST",
    body: JSON.stringify({ confirmed: payload.confirmed ?? false }),
  });
}

export function listCustomCommandRuns(
  workspaceId: string,
  commandId: string,
  signal?: AbortSignal,
): Promise<CommandRunResponse[]> {
  return requestJson<CommandRunResponse[]>(
    `/workspaces/${workspaceId}/commands/${commandId}/runs`,
    { signal },
  );
}

export function createTerminalSession(
  workspaceId: string,
  payload: TerminalSessionCreatePayload = {},
): Promise<TerminalSessionResponse> {
  return requestJson<TerminalSessionResponse>(`/workspaces/${workspaceId}/terminal/sessions`, {
    method: "POST",
    body: JSON.stringify({ name: payload.name ?? "Terminal" }),
  });
}

export function listTerminalSessions(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<TerminalSessionResponse[]> {
  return requestJson<TerminalSessionResponse[]>(
    `/workspaces/${workspaceId}/terminal/sessions`,
    { signal },
  );
}

export function deleteTerminalSession(
  workspaceId: string,
  sessionId: string,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(
    `/workspaces/${workspaceId}/terminal/sessions/${sessionId}`,
    { method: "DELETE" },
  );
}

export function startEditorSession(workspaceId: string): Promise<EditorSessionResponse> {
  return requestJson<EditorSessionResponse>(`/workspaces/${workspaceId}/editor/start`, {
    method: "POST",
  });
}

export function getEditorStatus(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<EditorStatusResponse> {
  return requestJson<EditorStatusResponse>(`/workspaces/${workspaceId}/editor/status`, {
    signal,
  });
}

export function stopEditorSession(workspaceId: string): Promise<EditorSessionResponse> {
  return requestJson<EditorSessionResponse>(`/workspaces/${workspaceId}/editor/stop`, {
    method: "POST",
  });
}

export function listPreviewSessions(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<PreviewSessionResponse[]> {
  return requestJson<PreviewSessionResponse[]>(
    `/workspaces/${workspaceId}/preview/sessions`,
    { signal },
  );
}

export function detectPreviewSession(workspaceId: string): Promise<PreviewDetectResponse> {
  return requestJson<PreviewDetectResponse>(`/workspaces/${workspaceId}/preview/detect`, {
    method: "POST",
  });
}

export function refreshPreviewSession(
  workspaceId: string,
  sessionId: string,
): Promise<PreviewSessionResponse> {
  return requestJson<PreviewSessionResponse>(
    `/workspaces/${workspaceId}/preview/sessions/${sessionId}/refresh`,
    { method: "POST" },
  );
}

export function stopPreviewSession(
  workspaceId: string,
  sessionId: string,
): Promise<PreviewSessionResponse> {
  return requestJson<PreviewSessionResponse>(
    `/workspaces/${workspaceId}/preview/sessions/${sessionId}/stop`,
    { method: "POST" },
  );
}

export function startDevServer(workspaceId: string): Promise<DevServerActionResponse> {
  return requestJson<DevServerActionResponse>(`/workspaces/${workspaceId}/dev/start`, {
    method: "POST",
  });
}

export function getDevServerStatus(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<DevServerStatusResponse> {
  return requestJson<DevServerStatusResponse>(`/workspaces/${workspaceId}/dev/status`, {
    signal,
  });
}

/**
 * Reports dev-server status and, if it was running before the sandbox slept,
 * auto-restarts it (waking the sandbox). Safe to poll. Same shape as status.
 */
export function ensureDevServer(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<DevServerStatusResponse> {
  return requestJson<DevServerStatusResponse>(`/workspaces/${workspaceId}/dev/ensure`, {
    method: "POST",
    signal,
  });
}

export function stopDevServer(workspaceId: string): Promise<DevServerActionResponse> {
  return requestJson<DevServerActionResponse>(`/workspaces/${workspaceId}/dev/stop`, {
    method: "POST",
  });
}

// ── Projects & Teams ──────────────────────────────────────────────────────────

export type ProjectRole = "admin" | "member" | "viewer";
export type TeamRole = "lead" | "member" | "viewer";

export type ProjectResponse = {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectMemberResponse = {
  id: string;
  project_id: string;
  user_id: string;
  role: ProjectRole;
  joined_at: string;
};

export type TeamResponse = {
  id: string;
  project_id: string;
  name: string;
  slug: string;
  description: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

export type TeamMemberResponse = {
  id: string;
  team_id: string;
  user_id: string;
  role: TeamRole;
  joined_at: string;
};

export type ProjectCreatePayload = { name: string; description?: string | null };
export type ProjectUpdatePayload = Partial<ProjectCreatePayload>;
export type AddMemberPayload = { user_id: string; role: string };
export type UpdateMemberRolePayload = { role: string };
export type TeamCreatePayload = { name: string; description?: string | null };
export type TeamUpdatePayload = Partial<TeamCreatePayload>;

// Projects

export function listProjects(signal?: AbortSignal): Promise<ProjectResponse[]> {
  return requestJson<ProjectResponse[]>("/projects", { signal });
}

export function createProject(payload: ProjectCreatePayload): Promise<ProjectResponse> {
  return requestJson<ProjectResponse>("/projects", { method: "POST", body: JSON.stringify(payload) });
}

export function getProject(projectId: string, signal?: AbortSignal): Promise<ProjectResponse> {
  return requestJson<ProjectResponse>(`/projects/${projectId}`, { signal });
}

export function updateProject(
  projectId: string,
  payload: ProjectUpdatePayload,
): Promise<ProjectResponse> {
  return requestJson<ProjectResponse>(`/projects/${projectId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteProject(projectId: string): Promise<void> {
  return requestJson<void>(`/projects/${projectId}`, { method: "DELETE" });
}

export function listProjectMembers(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectMemberResponse[]> {
  return requestJson<ProjectMemberResponse[]>(`/projects/${projectId}/members`, { signal });
}

export function addProjectMember(
  projectId: string,
  payload: AddMemberPayload,
): Promise<ProjectMemberResponse> {
  return requestJson<ProjectMemberResponse>(`/projects/${projectId}/members`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateProjectMemberRole(
  projectId: string,
  userId: string,
  payload: UpdateMemberRolePayload,
): Promise<ProjectMemberResponse> {
  return requestJson<ProjectMemberResponse>(`/projects/${projectId}/members/${userId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function removeProjectMember(projectId: string, userId: string): Promise<void> {
  return requestJson<void>(`/projects/${projectId}/members/${userId}`, { method: "DELETE" });
}

// Teams

export function listTeams(projectId: string, signal?: AbortSignal): Promise<TeamResponse[]> {
  return requestJson<TeamResponse[]>(`/projects/${projectId}/teams`, { signal });
}

export function createTeam(projectId: string, payload: TeamCreatePayload): Promise<TeamResponse> {
  return requestJson<TeamResponse>(`/projects/${projectId}/teams`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getTeam(
  projectId: string,
  teamId: string,
  signal?: AbortSignal,
): Promise<TeamResponse> {
  return requestJson<TeamResponse>(`/projects/${projectId}/teams/${teamId}`, { signal });
}

export function updateTeam(
  projectId: string,
  teamId: string,
  payload: TeamUpdatePayload,
): Promise<TeamResponse> {
  return requestJson<TeamResponse>(`/projects/${projectId}/teams/${teamId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteTeam(projectId: string, teamId: string): Promise<void> {
  return requestJson<void>(`/projects/${projectId}/teams/${teamId}`, { method: "DELETE" });
}

export function listTeamMembers(
  projectId: string,
  teamId: string,
  signal?: AbortSignal,
): Promise<TeamMemberResponse[]> {
  return requestJson<TeamMemberResponse[]>(`/projects/${projectId}/teams/${teamId}/members`, {
    signal,
  });
}

export function addTeamMember(
  projectId: string,
  teamId: string,
  payload: AddMemberPayload,
): Promise<TeamMemberResponse> {
  return requestJson<TeamMemberResponse>(`/projects/${projectId}/teams/${teamId}/members`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateTeamMemberRole(
  projectId: string,
  teamId: string,
  userId: string,
  payload: UpdateMemberRolePayload,
): Promise<TeamMemberResponse> {
  return requestJson<TeamMemberResponse>(
    `/projects/${projectId}/teams/${teamId}/members/${userId}`,
    { method: "PATCH", body: JSON.stringify(payload) },
  );
}

export function removeTeamMember(
  projectId: string,
  teamId: string,
  userId: string,
): Promise<void> {
  return requestJson<void>(`/projects/${projectId}/teams/${teamId}/members/${userId}`, {
    method: "DELETE",
  });
}

export function listTeamWorkspaces(
  projectId: string,
  teamId: string,
  signal?: AbortSignal,
): Promise<WorkspaceResponse[]> {
  return requestJson<WorkspaceResponse[]>(
    `/projects/${projectId}/teams/${teamId}/workspaces`,
    { signal },
  );
}

export function createTeamWorkspace(
  projectId: string,
  teamId: string,
  payload: WorkspaceCreatePayload,
): Promise<WorkspaceResponse> {
  return requestJson<WorkspaceResponse>(
    `/projects/${projectId}/teams/${teamId}/workspaces`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function getWorkspace(workspaceId: string, signal?: AbortSignal): Promise<WorkspaceResponse> {
  return requestJson<WorkspaceResponse>(`/workspaces/${workspaceId}`, { signal });
}

// Dashboard

export type WorkspaceStatusCounts = {
  creating: number;
  ready: number;
  busy: number;
  error: number;
  stopped: number;
};

export type DashboardProjectItem = {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  team_count: number;
  workspace_count: number;
  created_at: string;
};

export type DashboardWorkspaceItem = {
  id: string;
  name: string;
  status: WorkspaceStatus;
  project_id: string | null;
  project_name: string | null;
  team_id: string | null;
  team_name: string | null;
  created_at: string;
};

export type DashboardSummary = {
  projects_total: number;
  teams_total: number;
  workspaces_total: number;
  workspaces_by_status: WorkspaceStatusCounts;
  recent_projects: DashboardProjectItem[];
  recent_workspaces: DashboardWorkspaceItem[];
};

export function getDashboardSummary(signal?: AbortSignal): Promise<DashboardSummary> {
  return requestJson<DashboardSummary>("/dashboard/summary", { signal });
}

// --- Spec-Driven Development (SDD) ---------------------------------------------------

export type SpecPhaseName =
  | "constitution"
  | "specify"
  | "clarify"
  | "plan"
  | "analyze"
  | "tasks"
  | "implement";

export type SpecWorkflowStatus =
  | "pending"
  | "running"
  | "awaiting_review"
  | "completed"
  | "error";

export type SpecPhaseStatus =
  | "pending"
  | "running"
  | "awaiting_review"
  | "approved"
  | "changes_requested"
  | "completed"
  | "error";

export type SpecPhaseResponse = {
  id: string;
  workflow_id: string;
  phase: SpecPhaseName;
  status: SpecPhaseStatus;
  artifact_path: string | null;
  agent_run_id: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_notes: string | null;
  attempt: number;
  created_at: string;
  updated_at: string;
};

export type SpecWorkflowResponse = {
  id: string;
  workspace_id: string;
  user_id: string;
  feature_name: string;
  feature_slug: string;
  description: string;
  branch: string;
  spec_dir: string;
  worktree_path: string | null;
  status: SpecWorkflowStatus;
  current_phase: SpecPhaseName;
  created_at: string;
  updated_at: string;
  phases: SpecPhaseResponse[];
};

export type SpecArtifactResponse = {
  phase: SpecPhaseName;
  path: string | null;
  content: string;
};

export type SpecArtifactItem = {
  name: string;
  path: string;
};

export type SpecArtifactListResponse = {
  phase: SpecPhaseName;
  items: SpecArtifactItem[];
};

export type OpenQuestion = {
  id: string;
  category: string | null;
  severity: string | null;
  location: string | null;
  summary: string | null;
  recommendation: string | null;
};

export type OpenQuestionsResponse = {
  phase: SpecPhaseName;
  items: OpenQuestion[];
};

export type SpecWorkflowCreatePayload = {
  feature_name: string;
  description: string;
};

export function listSpecWorkflows(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<SpecWorkflowResponse[]> {
  return requestJson<SpecWorkflowResponse[]>(`/workspaces/${workspaceId}/spec/workflows`, {
    signal,
  });
}

export function getSpecWorkflow(
  workspaceId: string,
  workflowId: string,
  signal?: AbortSignal,
): Promise<SpecWorkflowResponse> {
  return requestJson<SpecWorkflowResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}`,
    { signal },
  );
}

export function createSpecWorkflow(
  workspaceId: string,
  payload: SpecWorkflowCreatePayload,
): Promise<SpecWorkflowResponse> {
  return requestJson<SpecWorkflowResponse>(`/workspaces/${workspaceId}/spec/workflows`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getSpecArtifact(
  workspaceId: string,
  workflowId: string,
  phase: SpecPhaseName,
  path?: string,
  signal?: AbortSignal,
): Promise<SpecArtifactResponse> {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return requestJson<SpecArtifactResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/${phase}/artifact${query}`,
    { signal },
  );
}

export function listSpecArtifacts(
  workspaceId: string,
  workflowId: string,
  phase: SpecPhaseName,
  signal?: AbortSignal,
): Promise<SpecArtifactListResponse> {
  return requestJson<SpecArtifactListResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/${phase}/artifacts`,
    { signal },
  );
}

export function getAnalyzeQuestions(
  workspaceId: string,
  workflowId: string,
  signal?: AbortSignal,
): Promise<OpenQuestionsResponse> {
  return requestJson<OpenQuestionsResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/analyze/questions`,
    { signal },
  );
}

export function submitAnalyzeAnswers(
  workspaceId: string,
  workflowId: string,
  answers: { id: string; answer: string }[],
): Promise<SpecPhaseResponse> {
  return requestJson<SpecPhaseResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/analyze/answers`,
    { method: "POST", body: JSON.stringify({ answers }) },
  );
}

export function runSpecPhase(
  workspaceId: string,
  workflowId: string,
  phase: SpecPhaseName,
): Promise<SpecPhaseResponse> {
  return requestJson<SpecPhaseResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/${phase}/run`,
    { method: "POST" },
  );
}

export function approveSpecPhase(
  workspaceId: string,
  workflowId: string,
  phase: SpecPhaseName,
): Promise<SpecPhaseResponse> {
  return requestJson<SpecPhaseResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/${phase}/approve`,
    { method: "POST" },
  );
}

export function requestSpecChanges(
  workspaceId: string,
  workflowId: string,
  phase: SpecPhaseName,
  notes: string,
): Promise<SpecPhaseResponse> {
  return requestJson<SpecPhaseResponse>(
    `/workspaces/${workspaceId}/spec/workflows/${workflowId}/phases/${phase}/request-changes`,
    { method: "POST", body: JSON.stringify({ notes }) },
  );
}
