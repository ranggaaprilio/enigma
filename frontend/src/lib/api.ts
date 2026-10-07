export type HealthResponse = {
  status: "ok" | "degraded";
  database: "ok" | "error";
  redis: "ok" | "error";
  daytona: "ok" | "error";
};

export type UserRole = "owner" | "developer" | "viewer";
/** The requesting user's role in a specific workspace, as reported by the backend. */
export type WorkspaceRole = "owner" | "team_lead" | "team_member" | "team_viewer";

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

export type CommentTargetType = "run" | "spec_phase" | "pr";

export type CommentResponse = {
  id: string;
  target_type: CommentTargetType;
  target_id: string;
  user_id: string;
  body: string;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
  updated_at: string;
};

export type PresenceUser = {
  user_id: string;
  name: string;
  connection_id: string;
  joined_at: string;
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

/** Where a workspace's repository came from. Template/blank workspaces have no remote yet. */
export type WorkspaceSource = "repository" | "template" | "blank";
export type GitProvider = "github" | "gitlab" | "bitbucket" | "generic";
/** `auto` lets the backend detect the provider from the URL host. */
export type GitProviderChoice = GitProvider | "auto";
export type RepositoryAuthType = "personal_access_token" | "ssh_key";

export type WorkspaceTemplateResponse = {
  id: string;
  slug: string;
  name: string;
  description: string;
  source_repo_url: string;
  default_branch: string;
  icon: string | null;
  tags: string[];
  preview_port: number | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
};

/** Public view of a workspace's git credential — never contains the token or private key. */
export type RepositoryCredentialResponse = {
  provider: GitProvider;
  auth_type: RepositoryAuthType;
  username: string | null;
  /** Present for `ssh_key` credentials: add it as a deploy key with write access on the host. */
  public_key: string | null;
  supports_pull_requests: boolean;
  repo_url: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkspaceResponse = {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  /** `null` for template/blank workspaces until a remote is connected. */
  repo_url: string | null;
  source?: WorkspaceSource;
  template_id?: string | null;
  default_branch: string;
  runtime: string;
  sandbox_id: string | null;
  snapshot: string | null;
  workspace_path: string;
  /** Repo-relative subdirectory commands and the dev server run in; null = checkout root. */
  app_root: string | null;
  status: WorkspaceStatus;
  created_at: string;
  updated_at: string;
  /** Caller's role in this workspace; null when the backend could not resolve one. */
  viewer_role: WorkspaceRole | null;
  /** Path (relative to the sandbox) a `.env` file is written to on service start; null = disabled. */
  env_file_path: string | null;
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
  /** Required when `source` is `repository`; omit for `template` and `blank`. */
  repo_url?: string;
  source?: WorkspaceSource;
  template_id?: string;
  default_branch?: string;
  snapshot?: string | null;
};

export type WorkspaceUpdatePayload = Partial<
  Pick<WorkspaceCreatePayload, "name" | "repo_url" | "default_branch" | "snapshot">
> & {
  /** Send `""` to clear it back to the checkout root. Validated server-side. */
  app_root?: string | null;
  /** Send `null` to disable writing a `.env` file. */
  env_file_path?: string | null;
};

export type EnvVarScope = "workspace" | "service";

export type WorkspaceEnvVarResponse = {
  id: string;
  key: string;
  masked_value: string;
  created_at: string;
  updated_at: string;
  scope: EnvVarScope;
  /** Which service command this variable is scoped to; null when `scope` is `workspace`. */
  custom_command_id: string | null;
};

export type WorkspaceEnvVarPayload = {
  key: string;
  value: string;
  /** Scope the variable to one service command instead of the whole workspace. */
  custom_command_id?: string | null;
};

/** A workspace service affected by an env-var change, surfaced so the caller can offer a restart. */
export type AffectedService = {
  service_id: string;
  custom_command_id: string | null;
  name: string;
  status: string;
  keys: string[];
};

export type WorkspaceEnvVarUpsertResponse = WorkspaceEnvVarResponse & {
  affected_services: AffectedService[];
  warnings: string[];
};

export type EnvVarBulkItem = {
  key: string;
  status: "created" | "updated" | "unchanged" | "invalid";
  line: number | null;
  message: string | null;
};

export type EnvVarBulkResponse = {
  items: EnvVarBulkItem[];
  created: number;
  updated: number;
  unchanged: number;
  invalid: number;
  dry_run: boolean;
  affected_services: AffectedService[];
  warnings: string[];
};

/** Exactly one of `text`/`values` must be provided. */
export type EnvVarBulkRequest = {
  text?: string;
  values?: Record<string, string>;
  custom_command_id?: string | null;
  dry_run: boolean;
};

export type EnvVarBulkDeleteRequest = {
  keys: string[];
  custom_command_id?: string | null;
};

export type EnvVarBulkDeleteResponse = {
  deleted: string[];
  missing: string[];
  affected_services: AffectedService[];
};

export type RepositoryConnectPayload = {
  repo_url: string;
  provider?: GitProviderChoice;
  auth_type: RepositoryAuthType;
  /** Required for `personal_access_token`; must be omitted for `ssh_key`. */
  token?: string;
  /** Optional basic-auth username override (Bitbucket app passwords, generic hosts). */
  username?: string;
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

export type GitCommitResponse = {
  hash: string;
  short_hash: string;
  author_name: string;
  author_email: string;
  date: string;
  subject: string;
};

export type RepositoryLogResponse = {
  commits: GitCommitResponse[];
};

export type RepositoryCommitDetailResponse = {
  hash: string;
  short_hash: string;
  author_name: string;
  author_email: string;
  date: string;
  subject: string;
  body: string;
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
  session_id: string | null;
  prompt: string;
  status: AgentRunStatus;
  provider: string;
  model: string;
  tokens_used: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  estimated_cost: number | null;
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

export type AgentSessionStatus = "active" | "archived";

export type AgentSessionResponse = {
  id: string;
  workspace_id: string;
  user_id: string;
  title: string | null;
  status: AgentSessionStatus;
  created_at: string;
  updated_at: string;
};

// Detail returns turns as AgentRun summaries (no events); per-turn events come from
// getAgentRun / the WebSocket stream.
export type AgentSessionDetailResponse = AgentSessionResponse & {
  turns: AgentRunResponse[]; // ordered oldest → newest
};

export type AgentSessionCreatePayload = {
  title?: string | null;
};

export type AgentSessionUpdatePayload = {
  title?: string | null;
  status?: AgentSessionStatus;
};

export type AgentTurnCreatePayload = {
  prompt: string;
  worktree_id?: string | null;
};

/** Lifecycle role of a registry entry. Drives grouping and what the dev server runs. */
export type CommandRole =
  | "install"
  | "migrate"
  | "dev"
  | "test"
  | "lint"
  | "seed"
  | "custom";

/** `service` (a long-running process with a port) is executed from Epic 11 onwards. */
export type CommandKind = "task" | "service";

export const COMMAND_ROLES: CommandRole[] = [
  "install",
  "migrate",
  "dev",
  "test",
  "lint",
  "seed",
  "custom",
];

export type CustomCommandResponse = {
  id: string;
  workspace_id: string;
  name: string;
  command: string;
  description: string | null;
  icon: string | null;
  hotkey: string | null;
  requires_confirmation: boolean;
  role: CommandRole;
  kind: CommandKind;
  /** Relative to the workspace's app root; `null` means the app root itself. */
  cwd: string | null;
  port: number | null;
  sort_order: number;
  enabled: boolean;
  /** `null` falls back to the platform default (15 minutes). */
  timeout_seconds: number | null;
  /** Root-relative HTTP path probed for readiness; `null` means only a TCP check applies. */
  health_path: string | null;
  /** The workspace's primary "Open app" service. Only a `kind="service"` command can be one. */
  is_entry_point: boolean;
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
  role?: CommandRole;
  kind?: CommandKind;
  cwd?: string | null;
  port?: number | null;
  enabled?: boolean;
  timeout_seconds?: number | null;
  health_path?: string | null;
  is_entry_point?: boolean;
};

export type CustomCommandUpdatePayload = Partial<CustomCommandPayload>;

export type CommandReorderPayload = {
  items: { id: string; sort_order: number }[];
};

export type CommandRunResponse = {
  id: string;
  custom_command_id: string;
  workspace_id: string;
  user_id: string;
  /** Which worktree the run targeted; `null` means the base clone. */
  worktree_id: string | null;
  cwd: string | null;
  status: CommandRunStatus;
  exit_code: number | null;
  output: string;
  started_at: string;
  completed_at: string | null;
};

export type CommandRunCreatePayload = {
  confirmed?: boolean;
  worktree_id?: string | null;
};

/** A detection *proposal*. Nothing is persisted until the user imports it. */
export type DetectedCommand = {
  name: string;
  command: string;
  role: CommandRole;
  cwd: string | null;
  port: number | null;
  /** The manifest this was derived from, so the UI can explain the suggestion. */
  source_file: string;
  reason: string;
};

export type DetectCommandsResponse = {
  candidates: DetectedCommand[];
};

export type ImportCommandItem = {
  name: string;
  command: string;
  role?: CommandRole;
  // Omit to let the server default it from role (a `dev`-role candidate becomes `service`).
  kind?: CommandKind;
  cwd?: string | null;
  port?: number | null;
};

export type RunPresetResponse = {
  slug: string;
  name: string;
  description: string;
  commands: {
    name: string;
    command: string;
    role: CommandRole;
    cwd: string | null;
    port: number | null;
  }[];
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
  /** Stable alias URL. Carries no credential, so it is safe to display or copy. */
  url: string;
  /**
   * The same URL plus a short-lived grant. This is what the iframe must load: only a request
   * carrying the grant gets the proxy's host-scoped cookie set. Minted per response, and empty
   * when the session is not active.
   */
  prime_url: string;
  status: EditorSessionStatus;
  worktree_id: string | null;
  folder: string | null;
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
  // The worktree this preview belongs to (null = base clone) and its branch.
  worktree_id: string | null;
  branch: string | null;
  // Which workspace service this preview was detected for (Epic 11); null for a session
  // detected via the legacy single-port scan.
  service_id: string | null;
};

export type PreviewDetectResponse = {
  status: PreviewAvailability;
  session: PreviewSessionResponse | null;
};

export type DevServerStatus =
  | "idle"
  | "installing"
  | "ready"
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
  // The worktree the dev server is running in (null = base clone) and its branch.
  worktree_id: string | null;
  branch: string | null;
};

export type DevServerActionResponse = {
  ok: boolean;
  status: DevServerStatus;
  message: string | null;
};

/** A running (or desired-to-run) instance of a workspace's service command (Epic 11). */
export type WorkspaceServiceStatus =
  | "installing"
  | "starting"
  | "running"
  | "stopped"
  | "exited"
  | "error";

// Epic 13: readiness, orthogonal to WorkspaceServiceStatus -- a service can be "running" (port
// bound) and "unhealthy" (failing its health check) at the same time.
export type WorkspaceServiceHealth = "unknown" | "starting" | "healthy" | "unhealthy";

export type WorkspaceServiceResponse = {
  id: string;
  workspace_id: string;
  // null = the built-in mise `dev` service (no registry command owns it).
  custom_command_id: string | null;
  name: string;
  role: CommandRole;
  kind: CommandKind;
  command: string | null;
  port: number | null;
  status: WorkspaceServiceStatus;
  desired: boolean;
  worktree_id: string | null;
  branch: string | null;
  last_error: string | null;
  exit_code: number | null;
  started_at: string | null;
  last_seen_at: string | null;
  preview_session_id: string | null;
  public_url: string | null;
  health_status: WorkspaceServiceHealth;
  health_checked_at: string | null;
  health_path: string | null;
  is_entry_point: boolean;
};

/** The workspace-wide mise environment (install/mise-trust), separate from any one
 * service's run status — the pre-Epic-11 `/dev/setup` concept. */
export type WorkspaceEnvironmentState = {
  status: string;
  message: string | null;
};

export type WorkspaceServicesResponse = {
  services: WorkspaceServiceResponse[];
  environment: WorkspaceEnvironmentState;
  limit: number;
};

export type ServiceActionResponse = {
  service: WorkspaceServiceResponse;
  warning: string | null;
};

export type ServiceLogsResponse = {
  content: string;
};

export type LoginPayload = {
  email: string;
  password: string;
};

export type RegisterPayload = LoginPayload & {
  name?: string;
};

/** Where the active LLM configuration came from. `null` when nothing is configured. */
export type LlmSettingsSource = "database" | "environment";

/**
 * Readable by any authenticated user, so the first-run gate can tell whether setup is still
 * pending. `base_url`/`model` are not secrets and are included so a non-owner's read-only view
 * has something to show; the API key never appears here.
 */
export type LlmSettingsStatusResponse = {
  configured: boolean;
  source: LlmSettingsSource | null;
  base_url: string | null;
  model: string | null;
};

/** Owner-only. The API key is write-only: only `has_api_key` and a constant mask come back. */
export type LlmSettingsResponse = {
  base_url: string | null;
  model: string | null;
  has_api_key: boolean;
  masked_api_key: string | null;
  source: LlmSettingsSource | null;
  updated_at: string | null;
};

export type LlmSettingsUpdatePayload = {
  base_url: string;
  model: string;
  /** Omit to keep the stored key; "" clears it. Never send a value the user did not type. */
  api_key?: string;
};

/**
 * Draft values, so the endpoint can be verified *before* it is saved -- the first-run case,
 * where nothing is stored yet. Omitted fields fall back to the stored configuration.
 */
export type LlmSettingsTestPayload = {
  base_url?: string;
  model?: string;
  api_key?: string;
};

export type LlmProbeResponse = {
  ok: boolean;
  message: string;
  /** Model ids from `GET {base_url}/models`; null when the endpoint does not expose them. */
  models: string[] | null;
  latency_ms: number | null;
};

export type LlmSettingsDeleteResponse = {
  ok: boolean;
  cleared: boolean;
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

export function getServiceLogStreamUrl(workspaceId: string, serviceId: string) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.protocol = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  baseUrl.pathname = `/workspaces/${workspaceId}/services/${serviceId}/logs/stream`;
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

export function getPresenceStreamUrl(workspaceId: string) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.protocol = baseUrl.protocol === "https:" ? "wss:" : "ws:";
  baseUrl.pathname = `/workspaces/${workspaceId}/presence/stream`;
  baseUrl.search = "";
  return baseUrl.toString();
}

export function getWorkspaceFileDownloadUrl(
  workspaceId: string,
  path: string,
  worktreeId?: string,
) {
  const baseUrl = new URL(getApiBaseUrl());
  baseUrl.pathname = `/workspaces/${workspaceId}/files/download`;
  const params = new URLSearchParams({ path });
  if (worktreeId) params.set("worktree_id", worktreeId);
  baseUrl.search = params.toString();
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

/** Like {@link requestJson}, but for endpoints that respond with `text/plain` instead of JSON. */
async function requestText(path: string, init: RequestInit = {}): Promise<string> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    const message =
      payload && typeof payload === "object" && "detail" in payload && payload.detail
        ? payload.detail
        : "Request failed";
    throw new ApiError(message, response.status);
  }

  return response.text();
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

export function getLlmSettingsStatus(signal?: AbortSignal): Promise<LlmSettingsStatusResponse> {
  return requestJson<LlmSettingsStatusResponse>("/platform/llm-settings/status", { signal });
}

export function getLlmSettings(signal?: AbortSignal): Promise<LlmSettingsResponse> {
  return requestJson<LlmSettingsResponse>("/platform/llm-settings", { signal });
}

export function updateLlmSettings(
  payload: LlmSettingsUpdatePayload,
): Promise<LlmSettingsResponse> {
  return requestJson<LlmSettingsResponse>("/platform/llm-settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function testLlmSettings(payload: LlmSettingsTestPayload): Promise<LlmProbeResponse> {
  return requestJson<LlmProbeResponse>("/platform/llm-settings/test", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteLlmSettings(): Promise<LlmSettingsDeleteResponse> {
  return requestJson<LlmSettingsDeleteResponse>("/platform/llm-settings", { method: "DELETE" });
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

export function listComments(
  targetType: CommentTargetType,
  targetId: string,
  signal?: AbortSignal,
): Promise<CommentResponse[]> {
  const params = new URLSearchParams({ target_type: targetType, target_id: targetId });
  return requestJson<CommentResponse[]>(`/comments?${params.toString()}`, { signal });
}

export function createComment(
  targetType: CommentTargetType,
  targetId: string,
  body: string,
): Promise<CommentResponse> {
  return requestJson<CommentResponse>("/comments", {
    method: "POST",
    body: JSON.stringify({ target_type: targetType, target_id: targetId, body }),
  });
}

export function resolveComment(commentId: string): Promise<CommentResponse> {
  return requestJson<CommentResponse>(`/comments/${commentId}/resolve`, { method: "PATCH" });
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

/**
 * Retry provisioning a workspace whose sandbox failed to come up. Idempotent — the backend
 * reuses the existing sandbox when there is one. `getWorkspaceStatus` cannot recover such a
 * workspace because it has no sandbox to query.
 */
export function reprovisionWorkspace(workspaceId: string): Promise<WorkspaceResponse> {
  return requestJson<WorkspaceResponse>(`/workspaces/${workspaceId}/provision`, {
    method: "POST",
  });
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
): Promise<WorkspaceEnvVarUpsertResponse> {
  return requestJson<WorkspaceEnvVarUpsertResponse>(`/workspaces/${workspaceId}/env-vars`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function deleteWorkspaceEnvVar(
  workspaceId: string,
  key: string,
  customCommandId?: string | null,
): Promise<{ ok: boolean }> {
  const params = new URLSearchParams({ key });
  if (customCommandId) params.set("custom_command_id", customCommandId);
  return requestJson<{ ok: boolean }>(
    `/workspaces/${workspaceId}/env-vars?${params.toString()}`,
    { method: "DELETE" },
  );
}

export function bulkUpsertWorkspaceEnvVars(
  workspaceId: string,
  payload: EnvVarBulkRequest,
): Promise<EnvVarBulkResponse> {
  return requestJson<EnvVarBulkResponse>(`/workspaces/${workspaceId}/env-vars/bulk`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function bulkDeleteWorkspaceEnvVars(
  workspaceId: string,
  payload: EnvVarBulkDeleteRequest,
): Promise<EnvVarBulkDeleteResponse> {
  return requestJson<EnvVarBulkDeleteResponse>(`/workspaces/${workspaceId}/env-vars/bulk-delete`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** `.env`-shaped template with keys only (no values), grouped by scope as `#` comments. */
export function getWorkspaceEnvVarTemplate(workspaceId: string): Promise<string> {
  return requestText(`/workspaces/${workspaceId}/env-vars/template`);
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

export function getRepositoryCredential(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<RepositoryCredentialResponse> {
  return requestJson<RepositoryCredentialResponse>(`/workspaces/${workspaceId}/repo/credential`, {
    signal,
  });
}

export function ensureRepositorySshKey(
  workspaceId: string,
  rotate = false,
): Promise<RepositoryCredentialResponse> {
  return requestJson<RepositoryCredentialResponse>(`/workspaces/${workspaceId}/repo/ssh-key`, {
    method: "POST",
    body: JSON.stringify({ rotate }),
  });
}

export function listWorkspaceTemplates(signal?: AbortSignal): Promise<WorkspaceTemplateResponse[]> {
  return requestJson<WorkspaceTemplateResponse[]>("/workspace-templates", { signal });
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

export function getRepositoryLog(
  workspaceId: string,
  worktreeId?: string,
  limit?: number,
  skip?: number,
  signal?: AbortSignal,
): Promise<RepositoryLogResponse> {
  const params = new URLSearchParams();
  if (worktreeId) params.set("worktree_id", worktreeId);
  if (limit != null) params.set("limit", String(limit));
  if (skip != null) params.set("skip", String(skip));
  const query = params.toString() ? `?${params.toString()}` : "";
  return requestJson<RepositoryLogResponse>(`/workspaces/${workspaceId}/repo/log${query}`, {
    signal,
  });
}

export function getCommitDetail(
  workspaceId: string,
  sha: string,
  worktreeId?: string,
  signal?: AbortSignal,
): Promise<RepositoryCommitDetailResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<RepositoryCommitDetailResponse>(
    `/workspaces/${workspaceId}/repo/commits/${encodeURIComponent(sha)}${query}`,
    { signal },
  );
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

export function discardRepositoryChanges(
  workspaceId: string,
  path?: string,
  worktreeId?: string,
): Promise<RepositoryOperationResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/discard${query}`,
    { method: "POST", body: JSON.stringify({ path: path ?? null }) },
  );
}

export function fetchRepository(
  workspaceId: string,
  worktreeId?: string,
): Promise<RepositoryOperationResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/fetch${query}`,
    { method: "POST" },
  );
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

export function pullWorktree(
  workspaceId: string,
  worktreeId: string,
): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/worktrees/${worktreeId}/pull`,
    { method: "POST" },
  );
}

export function syncWorktree(
  workspaceId: string,
  worktreeId: string,
  strategy: "merge" | "rebase" = "merge",
): Promise<RepositoryOperationResponse> {
  return requestJson<RepositoryOperationResponse>(
    `/workspaces/${workspaceId}/repo/worktrees/${worktreeId}/sync`,
    { method: "POST", body: JSON.stringify({ strategy }) },
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
  worktreeId?: string,
  signal?: AbortSignal,
): Promise<WorkspaceFileNode> {
  const params = new URLSearchParams();
  if (path) params.set("path", path);
  if (worktreeId) params.set("worktree_id", worktreeId);
  const query = params.toString() ? `?${params.toString()}` : "";
  return requestJson<WorkspaceFileNode>(`/workspaces/${workspaceId}/files/tree${query}`, {
    signal,
  });
}

export function readWorkspaceFile(
  workspaceId: string,
  path: string,
  worktreeId?: string,
  signal?: AbortSignal,
): Promise<WorkspaceFileReadResponse> {
  const params = new URLSearchParams({ path });
  if (worktreeId) params.set("worktree_id", worktreeId);
  return requestJson<WorkspaceFileReadResponse>(
    `/workspaces/${workspaceId}/files/read?${params.toString()}`,
    { signal },
  );
}

export function writeWorkspaceFile(
  workspaceId: string,
  payload: WorkspaceFileWritePayload,
  worktreeId?: string,
): Promise<WorkspaceFileOperationResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<WorkspaceFileOperationResponse>(
    `/workspaces/${workspaceId}/files/write${query}`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function uploadWorkspaceFile(
  workspaceId: string,
  path: string,
  file: File | Blob,
  filename?: string,
  worktreeId?: string,
): Promise<WorkspaceFileOperationResponse> {
  const name =
    filename ??
    (typeof File !== "undefined" && file instanceof File ? file.name : undefined) ??
    "upload.bin";
  const params = new URLSearchParams({ path, filename: name });
  if (worktreeId) params.set("worktree_id", worktreeId);
  const response = await fetch(
    `${getApiBaseUrl()}/workspaces/${workspaceId}/files/upload?${params.toString()}`,
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
  worktreeId?: string,
): Promise<WorkspaceFileOperationResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<WorkspaceFileOperationResponse>(
    `/workspaces/${workspaceId}/files/folders${query}`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function renameWorkspaceFile(
  workspaceId: string,
  payload: WorkspaceFileRenamePayload,
  worktreeId?: string,
): Promise<WorkspaceFileOperationResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<WorkspaceFileOperationResponse>(
    `/workspaces/${workspaceId}/files/rename${query}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export function deleteWorkspaceFile(
  workspaceId: string,
  path: string,
  worktreeId?: string,
): Promise<{ ok: boolean }> {
  const params = new URLSearchParams({ path });
  if (worktreeId) params.set("worktree_id", worktreeId);
  return requestJson<{ ok: boolean }>(`/workspaces/${workspaceId}/files?${params.toString()}`, {
    method: "DELETE",
  });
}

export function searchWorkspaceFiles(
  workspaceId: string,
  query: string,
  worktreeId?: string,
  signal?: AbortSignal,
): Promise<WorkspaceFileNode[]> {
  const params = new URLSearchParams({ q: query });
  if (worktreeId) params.set("worktree_id", worktreeId);
  return requestJson<WorkspaceFileNode[]>(
    `/workspaces/${workspaceId}/files/search?${params.toString()}`,
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

export function createAgentSession(
  workspaceId: string,
  payload: AgentSessionCreatePayload = {},
): Promise<AgentSessionResponse> {
  return requestJson<AgentSessionResponse>(`/workspaces/${workspaceId}/agent/sessions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function listAgentSessions(
  workspaceId: string,
  signal?: AbortSignal,
  limit?: number,
  offset?: number,
  status?: AgentSessionStatus,
): Promise<AgentSessionResponse[]> {
  const params = new URLSearchParams();
  if (limit !== undefined) params.set("limit", String(limit));
  if (offset !== undefined) params.set("offset", String(offset));
  if (status !== undefined) params.set("status", status);
  const query = params.toString() ? `?${params.toString()}` : "";
  return requestJson<AgentSessionResponse[]>(
    `/workspaces/${workspaceId}/agent/sessions${query}`,
    { signal },
  );
}

export function getAgentSession(
  workspaceId: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<AgentSessionDetailResponse> {
  return requestJson<AgentSessionDetailResponse>(
    `/workspaces/${workspaceId}/agent/sessions/${sessionId}`,
    { signal },
  );
}

export function updateAgentSession(
  workspaceId: string,
  sessionId: string,
  payload: AgentSessionUpdatePayload,
): Promise<AgentSessionResponse> {
  return requestJson<AgentSessionResponse>(
    `/workspaces/${workspaceId}/agent/sessions/${sessionId}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export function deleteAgentSession(
  workspaceId: string,
  sessionId: string,
): Promise<{ ok: boolean }> {
  return requestJson<{ ok: boolean }>(
    `/workspaces/${workspaceId}/agent/sessions/${sessionId}`,
    { method: "DELETE" },
  );
}

export function createAgentSessionTurn(
  workspaceId: string,
  sessionId: string,
  payload: AgentTurnCreatePayload,
): Promise<AgentRunResponse> {
  return requestJson<AgentRunResponse>(
    `/workspaces/${workspaceId}/agent/sessions/${sessionId}/turns`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
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
    body: JSON.stringify({
      confirmed: payload.confirmed ?? false,
      worktree_id: payload.worktree_id ?? null,
    }),
  });
}

/** Bulk `sort_order` update; returns the whole registry in its new order. */
export function reorderCustomCommands(
  workspaceId: string,
  payload: CommandReorderPayload,
): Promise<CustomCommandResponse[]> {
  return requestJson<CustomCommandResponse[]>(`/workspaces/${workspaceId}/commands/reorder`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Read-only: proposes entries from the repo's manifests without persisting anything. */
export function detectRunCommands(
  workspaceId: string,
  worktreeId?: string | null,
): Promise<DetectCommandsResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<DetectCommandsResponse>(
    `/workspaces/${workspaceId}/commands/detect${query}`,
    { method: "POST" },
  );
}

export function importRunCommands(
  workspaceId: string,
  items: ImportCommandItem[],
): Promise<CustomCommandResponse[]> {
  return requestJson<CustomCommandResponse[]>(`/workspaces/${workspaceId}/commands/import`, {
    method: "POST",
    body: JSON.stringify({ items }),
  });
}

export function listRunPresets(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<RunPresetResponse[]> {
  return requestJson<RunPresetResponse[]>(`/workspaces/${workspaceId}/commands/presets`, {
    signal,
  });
}

export function applyRunPreset(
  workspaceId: string,
  slug: string,
): Promise<CustomCommandResponse[]> {
  return requestJson<CustomCommandResponse[]>(
    `/workspaces/${workspaceId}/commands/presets/${encodeURIComponent(slug)}/apply`,
    { method: "POST" },
  );
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

export function startEditorSession(
  workspaceId: string,
  worktreeId?: string | null,
): Promise<EditorSessionResponse> {
  return requestJson<EditorSessionResponse>(`/workspaces/${workspaceId}/editor/start`, {
    method: "POST",
    body: JSON.stringify({ worktree_id: worktreeId ?? null }),
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

/**
 * Prepare the environment (toolchain + dependencies) without running the app.
 * Scoped to a worktree when `worktreeId` is given (defaults to the base clone).
 */
export function setupDevServer(
  workspaceId: string,
  worktreeId?: string | null,
): Promise<DevServerActionResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<DevServerActionResponse>(`/workspaces/${workspaceId}/dev/setup${query}`, {
    method: "POST",
  });
}

/**
 * Run the app's dev task on demand (the Preview "Run App" button), in the given
 * worktree when `worktreeId` is set (defaults to the base clone).
 */
export function runDevServer(
  workspaceId: string,
  worktreeId?: string | null,
): Promise<DevServerActionResponse> {
  const query = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : "";
  return requestJson<DevServerActionResponse>(`/workspaces/${workspaceId}/dev/run${query}`, {
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

// ── Workspace services (Epic 11: multi-service preview) ──────────────────────

export function listWorkspaceServices(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<WorkspaceServicesResponse> {
  return requestJson<WorkspaceServicesResponse>(`/workspaces/${workspaceId}/services`, {
    signal,
  });
}

/**
 * Reconciles every service marked desired against the sandbox, restarting any that were
 * running before the sandbox slept. Safe to poll — mirrors `ensureDevServer` for the whole
 * service list.
 */
export function ensureWorkspaceServices(
  workspaceId: string,
  signal?: AbortSignal,
): Promise<WorkspaceServicesResponse> {
  return requestJson<WorkspaceServicesResponse>(`/workspaces/${workspaceId}/services/ensure`, {
    method: "POST",
    signal,
  });
}

export function startWorkspaceService(
  workspaceId: string,
  serviceId: string,
  worktreeId?: string | null,
): Promise<ServiceActionResponse> {
  return requestJson<ServiceActionResponse>(
    `/workspaces/${workspaceId}/services/${serviceId}/start`,
    {
      method: "POST",
      body: JSON.stringify({ worktree_id: worktreeId ?? null }),
    },
  );
}

export function stopWorkspaceService(
  workspaceId: string,
  serviceId: string,
): Promise<ServiceActionResponse> {
  return requestJson<ServiceActionResponse>(
    `/workspaces/${workspaceId}/services/${serviceId}/stop`,
    { method: "POST" },
  );
}

export function restartWorkspaceService(
  workspaceId: string,
  serviceId: string,
  worktreeId?: string | null,
): Promise<ServiceActionResponse> {
  return requestJson<ServiceActionResponse>(
    `/workspaces/${workspaceId}/services/${serviceId}/restart`,
    {
      method: "POST",
      body: JSON.stringify({ worktree_id: worktreeId ?? null }),
    },
  );
}

export function getWorkspaceServiceLogs(
  workspaceId: string,
  serviceId: string,
  signal?: AbortSignal,
): Promise<ServiceLogsResponse> {
  return requestJson<ServiceLogsResponse>(
    `/workspaces/${workspaceId}/services/${serviceId}/logs`,
    { signal },
  );
}

// ── Preview composition (Epic 13: service wiring) ─────────────────────────────

export type PreviewUrlKind = "capability" | "alias";
export type ServiceDiagnosticKind = "cors_missing_allow_origin" | "cors_preflight_rejected";
export type PreviewAdvisorySeverity = "info" | "warning" | "error";
export type EntryPointSource = "explicit" | "inferred";

export type ServiceDiagnosisResponse = {
  id: string;
  kind: ServiceDiagnosticKind;
  origin: string;
  request_method: string | null;
  required_header: string | null;
  request_path: string | null;
  status_code: number | null;
  detail: string | null;
  occurrences: number;
  first_seen_at: string;
  last_seen_at: string;
};

export type ComposedServiceResponse = {
  service_id: string;
  custom_command_id: string | null;
  name: string;
  role: CommandRole;
  kind: CommandKind;
  port: number | null;
  status: WorkspaceServiceStatus;
  health_status: WorkspaceServiceHealth;
  health_checked_at: string | null;
  is_entry_point: boolean;
  entry_point_source: EntryPointSource;
  branch: string | null;
  worktree_id: string | null;
  preview_session_id: string | null;
  /** Rotates hourly by default -- label it "temporary link" in the UI. */
  capability_url: string | null;
  capability_url_rotates: boolean;
  /** Stable -- safe to paste into an environment variable. Both null when aliases are
   * disabled for the workspace, or none has been minted yet for this row. */
  alias: string | null;
  alias_url: string | null;
  alias_prime_url: string | null;
  localhost_url: string | null;
  diagnoses: ServiceDiagnosisResponse[];
};

export type PreviewAdvisoryResponse = {
  code: string;
  severity: PreviewAdvisorySeverity;
  title: string;
  detail: string;
  framework: string | null;
  snippet: string | null;
  snippet_language: string | null;
  target_file: string | null;
  service_ids: string[];
};

export type PreviewCompositionResponse = {
  workspace_id: string;
  preview_alias_enabled: boolean;
  proxy_base_url: string;
  entry_point_service_id: string | null;
  services: ComposedServiceResponse[];
  advisories: PreviewAdvisoryResponse[];
};

/**
 * `checkProxy=false` (the default -- every poll-loop call) costs zero sandbox round-trips.
 * Pass `true` only on mount and on an explicit "Re-check".
 */
export function getPreviewComposition(
  workspaceId: string,
  checkProxy: boolean,
  signal?: AbortSignal,
): Promise<PreviewCompositionResponse> {
  const query = checkProxy ? "?check_proxy=true" : "";
  return requestJson<PreviewCompositionResponse>(
    `/workspaces/${workspaceId}/preview/composition${query}`,
    { signal },
  );
}

export type PreviewAliasToggleResponse = {
  preview_alias_enabled: boolean;
};

export function enablePreviewAliases(workspaceId: string): Promise<PreviewAliasToggleResponse> {
  return requestJson<PreviewAliasToggleResponse>(
    `/workspaces/${workspaceId}/preview/aliases/enable`,
    { method: "POST" },
  );
}

export function disablePreviewAliases(workspaceId: string): Promise<PreviewAliasToggleResponse> {
  return requestJson<PreviewAliasToggleResponse>(
    `/workspaces/${workspaceId}/preview/aliases/disable`,
    { method: "POST" },
  );
}

export type PreviewAliasRegenerateResponse = {
  alias: string;
  alias_url: string;
};

export function regeneratePreviewAlias(
  workspaceId: string,
  customCommandId: string | null,
): Promise<PreviewAliasRegenerateResponse> {
  return requestJson<PreviewAliasRegenerateResponse>(
    `/workspaces/${workspaceId}/preview/aliases/regenerate`,
    {
      method: "POST",
      body: JSON.stringify({ custom_command_id: customCommandId }),
    },
  );
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

// Cost & usage analytics

export type UsageScope = "workspace" | "team" | "project";
export type UsageBreakdownDimension = "project" | "team" | "workspace" | "user";

export type UsageTotals = {
  run_count: number;
  input_tokens: number;
  output_tokens: number;
  tokens_used: number;
  estimated_cost: number;
};

export type UsageSeriesPoint = {
  date: string;
  run_count: number;
  tokens_used: number;
  estimated_cost: number;
};

export type UsageBreakdownItem = {
  id: string | null;
  name: string;
  run_count: number;
  tokens_used: number;
  estimated_cost: number;
};

export type UsageSummary = {
  scope: UsageScope | null;
  scope_id: string | null;
  date_from: string;
  date_to: string;
  totals: UsageTotals;
  series: UsageSeriesPoint[];
  breakdown_dimension: UsageBreakdownDimension;
  breakdown: UsageBreakdownItem[];
  top_spenders: UsageBreakdownItem[];
};

export type UsageQuery = {
  scope?: UsageScope;
  id?: string;
  from?: string;
  to?: string;
};

export function getUsageSummary(query: UsageQuery = {}, signal?: AbortSignal): Promise<UsageSummary> {
  const params = new URLSearchParams();
  if (query.scope) params.set("scope", query.scope);
  if (query.id) params.set("id", query.id);
  if (query.from) params.set("from", query.from);
  if (query.to) params.set("to", query.to);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return requestJson<UsageSummary>(`/dashboard/usage${suffix}`, { signal });
}

// Usage budgets

export type BudgetPeriod = "daily" | "weekly" | "monthly" | "total";
export type BudgetStatus = "ok" | "warning" | "over";

export type Budget = {
  id: string;
  scope: UsageScope;
  scope_id: string;
  period: BudgetPeriod;
  limit_cost: number | null;
  limit_tokens: number | null;
  threshold_percent: number;
  enforce: boolean;
  created_at: string;
  updated_at: string;
  current_cost: number;
  current_tokens: number;
  status: BudgetStatus;
};

export type BudgetCreatePayload = {
  scope: UsageScope;
  scope_id: string;
  period?: BudgetPeriod;
  limit_cost?: number | null;
  limit_tokens?: number | null;
  threshold_percent?: number;
  enforce?: boolean;
};

export type BudgetUpdatePayload = {
  period?: BudgetPeriod;
  limit_cost?: number | null;
  limit_tokens?: number | null;
  threshold_percent?: number;
  enforce?: boolean;
};

export function listBudgets(
  scope: UsageScope,
  id: string,
  signal?: AbortSignal,
): Promise<Budget[]> {
  const params = new URLSearchParams({ scope, id });
  return requestJson<Budget[]>(`/budgets?${params.toString()}`, { signal });
}

export function createBudget(payload: BudgetCreatePayload): Promise<Budget> {
  return requestJson<Budget>("/budgets", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateBudget(budgetId: string, payload: BudgetUpdatePayload): Promise<Budget> {
  return requestJson<Budget>(`/budgets/${budgetId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteBudget(budgetId: string): Promise<void> {
  return requestJson<void>(`/budgets/${budgetId}`, { method: "DELETE" });
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
