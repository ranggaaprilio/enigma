import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useOutletContext, useParams } from "react-router";

import { useAuth } from "../auth/context";
import { AgentTab } from "../components/AgentTab";
import { DashboardShell } from "../components/DashboardShell";
import { CustomCommandsPanel } from "../components/CustomCommandsPanel";
import { EditorPanel } from "../components/EditorPanel";
import { FileManagementPanel } from "../components/FileManagementPanel";
import { PreviewPanel } from "../components/PreviewPanel";
import { SpecPanel } from "../components/SpecPanel";
import { TerminalPanel } from "../components/TerminalPanel";
import { WorktreesPanel } from "../components/WorktreesPanel";
import {
  ApiError,
  cloneRepository,
  commitRepository,
  connectRepository,
  createWorkspace,
  deleteWorkspaceEnvVar,
  startDevServer,
  getRepositoryDiff,
  getRepositoryStatus,
  getHealth,
  getWorkspace,
  listAuditLogs,
  listWorkspaceEnvVars,
  listWorkspaces,
  pullRepository,
  pushRepository,
  updateWorkspace,
  upsertWorkspaceEnvVar,
  type AuditLogResponse,
  type HealthResponse,
  type RepositoryStatusResponse,
  type WorkspaceEnvVarResponse,
  type WorkspaceResponse,
  type WorkspaceStatus,
} from "../lib/api";

type HealthState =
  | { status: "loading" }
  | { status: "ready"; data: HealthResponse }
  | { status: "error"; message: string };

type WorkspaceState =
  | { status: "loading" }
  | { status: "ready"; workspace: WorkspaceResponse | null; envVars: WorkspaceEnvVarResponse[] }
  | { status: "error"; message: string };

type DashboardViewId =
  | "overview"
  | "repository"
  | "files"
  | "commands"
  | "editor"
  | "terminal"
  | "preview"
  | "spec"
  | "agent"
  | "audit";
type PreviewStatusSnapshot = {
  status: "inactive" | "active" | "stopped" | "error";
  port: number | null;
};

type DashboardOutletContext = {
  health: HealthState;
  workspaceState: WorkspaceState;
  workspaceStatus: WorkspaceStatus | "none" | "loading";
  onWorkspaceCreated: (workspace: WorkspaceResponse) => Promise<void>;
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
  onEnvVarsChanged: (envVars: WorkspaceEnvVarResponse[]) => void;
  onPreviewStatusChange: (snapshot: PreviewStatusSnapshot) => void;
};

export function DashboardRoute() {
  const { user, logout } = useAuth();
  const { workspaceId, projectId, teamId } = useParams<{
    workspaceId?: string;
    projectId?: string;
    teamId?: string;
  }>();
  const [health, setHealth] = useState<HealthState>({ status: "loading" });
  const [workspaceState, setWorkspaceState] = useState<WorkspaceState>({ status: "loading" });
  const [previewStatus, setPreviewStatus] = useState<PreviewStatusSnapshot>({
    status: "inactive",
    port: null,
  });

  useEffect(() => {
    const controller = new AbortController();

    getHealth(controller.signal)
      .then((data) => setHealth({ status: "ready", data }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHealth({
          status: "error",
          message: error instanceof Error ? error.message : "Unable to reach backend",
        });
      });

    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    loadWorkspaceData(controller.signal);
    return () => controller.abort();
  }, [workspaceId]);

  async function loadWorkspaceData(signal?: AbortSignal) {
    setWorkspaceState({ status: "loading" });
    try {
      let workspace: WorkspaceResponse | null = null;
      if (workspaceId) {
        workspace = await getWorkspace(workspaceId, signal);
      } else {
        const workspaces = await listWorkspaces(signal);
        workspace = workspaces[0] ?? null;
      }
      const envVars = workspace ? await listWorkspaceEnvVars(workspace.id, signal) : [];
      setWorkspaceState({ status: "ready", workspace, envVars });
    } catch (error: unknown) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      setWorkspaceState({
        status: "error",
        message: error instanceof Error ? error.message : "Unable to load workspace",
      });
    }
  }

  const shellStatus = useMemo(() => {
    if (health.status === "loading") {
      return { label: "Checking API", tone: "loading" as const };
    }

    if (health.status === "error") {
      return { label: "API offline", tone: "error" as const };
    }

    return health.data.status === "ok"
      ? { label: "API connected", tone: "ok" as const }
      : { label: "API degraded", tone: "degraded" as const };
  }, [health]);

  const workspace =
    workspaceState.status === "ready" ? workspaceState.workspace : null;
  const workspaceStatus =
    workspaceState.status === "loading" ? "loading" : workspace?.status ?? "none";
  const previewStatusLabel =
    previewStatus.port === null
      ? previewStatus.status
      : `${previewStatus.status} :${previewStatus.port}`;

  const handlePreviewStatusChange = useCallback((snapshot: PreviewStatusSnapshot) => {
    setPreviewStatus(snapshot);
  }, []);

  async function loadWorkspaceAfterCreate(created: WorkspaceResponse) {
    const envVars = await listWorkspaceEnvVars(created.id);
    setWorkspaceState({ status: "ready", workspace: created, envVars });
  }

  function handleWorkspaceChanged(nextWorkspace: WorkspaceResponse) {
    setWorkspaceState((current) =>
      current.status === "ready"
        ? { ...current, workspace: nextWorkspace }
        : current,
    );
  }

  function handleEnvVarsChanged(envVars: WorkspaceEnvVarResponse[]) {
    setWorkspaceState((current) =>
      current.status === "ready" ? { ...current, envVars } : current,
    );
  }

  const basePath =
    projectId && teamId && workspaceId
      ? `/projects/${projectId}/teams/${teamId}/workspaces/${workspaceId}`
      : "/";

  return (
    <DashboardShell
      healthLabel={shellStatus.label}
      healthTone={shellStatus.tone}
      workspace={workspace}
      workspaceStatus={workspaceStatus}
      previewStatusLabel={previewStatusLabel}
      user={user}
      onLogout={logout}
      basePath={basePath}
      projectId={projectId}
      teamId={teamId}
    >
      <Outlet
        context={{
          health,
          workspaceState,
          workspaceStatus,
          onWorkspaceCreated: loadWorkspaceAfterCreate,
          onWorkspaceChanged: handleWorkspaceChanged,
          onEnvVarsChanged: handleEnvVarsChanged,
          onPreviewStatusChange: handlePreviewStatusChange,
        } satisfies DashboardOutletContext}
      />
    </DashboardShell>
  );
}

export function DashboardView({ view }: { view: DashboardViewId }) {
  const {
    health,
    workspaceState,
    workspaceStatus,
    onWorkspaceCreated,
    onWorkspaceChanged,
    onEnvVarsChanged,
    onPreviewStatusChange,
  } = useOutletContext<DashboardOutletContext>();

  if (
    view === "overview" ||
    workspaceState.status !== "ready" ||
    !workspaceState.workspace
  ) {
    return (
      <DashboardOverview
        health={health}
        workspaceState={workspaceState}
        workspaceStatus={workspaceStatus}
        onWorkspaceCreated={onWorkspaceCreated}
        onWorkspaceChanged={onWorkspaceChanged}
        onEnvVarsChanged={onEnvVarsChanged}
      />
    );
  }

  const workspace = workspaceState.workspace;

  if (view === "repository") {
    return (
      <>
        <RepositoryPanel
          workspace={workspace}
          onWorkspaceChanged={onWorkspaceChanged}
        />
        <WorktreesPanel workspace={workspace} />
      </>
    );
  }

  if (view === "files") {
    return <FileManagementPanel workspace={workspace} />;
  }

  if (view === "commands") {
    return <CustomCommandsPanel workspace={workspace} />;
  }

  if (view === "editor") {
    return <EditorPanel workspace={workspace} />;
  }

  if (view === "terminal") {
    return <TerminalPanel workspace={workspace} />;
  }

  if (view === "preview") {
    return (
      <PreviewPanel
        workspace={workspace}
        onStatusChange={onPreviewStatusChange}
      />
    );
  }

  if (view === "spec") {
    return <SpecPanel workspace={workspace} />;
  }

  if (view === "agent") {
    return <AgentTab workspaceId={workspace.id} />;
  }

  return <AuditLogPanel workspace={workspace} />;
}

function DashboardOverview({
  health,
  workspaceState,
  workspaceStatus,
  onWorkspaceCreated,
  onWorkspaceChanged,
  onEnvVarsChanged,
}: {
  health: HealthState;
  workspaceState: WorkspaceState;
  workspaceStatus: WorkspaceStatus | "none" | "loading";
  onWorkspaceCreated: (workspace: WorkspaceResponse) => Promise<void>;
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
  onEnvVarsChanged: (envVars: WorkspaceEnvVarResponse[]) => void;
}) {
  const workspace =
    workspaceState.status === "ready" ? workspaceState.workspace : null;

  return (
    <>
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="rounded-md border border-line bg-white p-5 shadow-soft">
          <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
                Workspace
              </p>
              <h3 className="mt-1 text-2xl font-semibold">Project workspace</h3>
            </div>
            {workspace && (
              <span className="rounded-md border border-line bg-white px-3 py-2 text-sm capitalize text-ink/70">
                {workspace.status}
              </span>
            )}
          </div>

          {workspaceState.status === "loading" && (
            <LoadingState message="Loading workspace..." />
          )}
          {workspaceState.status === "error" && (
            <ErrorState message={workspaceState.message} />
          )}
          {workspaceState.status === "ready" && !workspaceState.workspace && (
            <CreateWorkspacePanel
              onCreated={(created) => void onWorkspaceCreated(created)}
            />
          )}
          {workspaceState.status === "ready" && workspaceState.workspace && (
            <WorkspaceOverview workspace={workspaceState.workspace} />
          )}
        </div>

        <FoundationChecks health={health} workspaceStatus={workspaceStatus} />
      </section>

      {workspaceState.status === "ready" && workspaceState.workspace && (
        <WorkspaceSettings
          workspace={workspaceState.workspace}
          envVars={workspaceState.envVars}
          onWorkspaceChanged={onWorkspaceChanged}
          onEnvVarsChanged={onEnvVarsChanged}
        />
      )}
    </>
  );
}

function AuditLogPanel({ workspace }: { workspace: WorkspaceResponse }) {
  const [auditLogs, setAuditLogs] = useState<AuditLogResponse[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setStatus("loading");
    listAuditLogs(workspace.id, controller.signal)
      .then((logs) => {
        setAuditLogs(Array.isArray(logs) ? logs : []);
        setStatus("ready");
      })
      .catch((unknownError: unknown) => {
        if (unknownError instanceof DOMException && unknownError.name === "AbortError") {
          return;
        }
        setError(unknownError instanceof Error ? unknownError.message : "Unable to load audit logs");
        setStatus("error");
      });
    return () => controller.abort();
  }, [workspace.id]);

  return (
    <section className="rounded-md border border-line bg-white p-5 shadow-soft">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
            Workspace
          </p>
          <h3 className="mt-1 text-2xl font-semibold">Audit log</h3>
        </div>
      </div>

      {status === "loading" && <LoadingState message="Loading audit logs..." />}
      {status === "error" && <ErrorState message={error ?? "Unable to load audit logs"} />}
      {status === "ready" && auditLogs.length === 0 && (
        <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
          No audit events yet.
        </div>
      )}
      {status === "ready" && auditLogs.length > 0 && (
        <div className="overflow-hidden rounded-md border border-line">
          <table className="min-w-full divide-y divide-line text-sm">
            <thead className="bg-panel text-left text-xs font-semibold uppercase tracking-[0.14em] text-muted">
              <tr>
                <th className="px-4 py-3">Time</th>
                <th className="px-4 py-3">Action</th>
                <th className="px-4 py-3">Target</th>
                <th className="px-4 py-3">Metadata</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line bg-white">
              {auditLogs.map((log) => (
                <tr key={log.id}>
                  <td className="whitespace-nowrap px-4 py-3 text-muted">
                    {formatDateTime(log.created_at)}
                  </td>
                  <td className="px-4 py-3 font-semibold text-ink">{log.action}</td>
                  <td className="px-4 py-3 text-body">
                    {log.target_type} {log.target_id}
                  </td>
                  <td className="max-w-md px-4 py-3 text-body">
                    {formatAuditMetadata(log.metadata)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatDateTime(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatAuditMetadata(metadata: Record<string, unknown>) {
  const entries = Object.entries(metadata);
  if (entries.length === 0) {
    return "None";
  }
  return entries
    .map(([key, value]) => `${key}: ${String(value)}`)
    .join(" | ");
}

function FoundationChecks({
  health,
  workspaceStatus,
}: {
  health: HealthState;
  workspaceStatus: WorkspaceStatus | "none" | "loading";
}) {
  return (
    <div className="rounded-md border border-line bg-white p-5 shadow-soft">
      <h3 className="text-base font-semibold">Foundation checks</h3>
      <ul className="mt-4 space-y-3 text-sm text-ink/72">
        <li className="flex items-center justify-between gap-3 border-b border-line pb-3">
          <span>Backend health</span>
          <span>{health.status === "ready" ? health.data.status : "pending"}</span>
        </li>
        <li className="flex items-center justify-between gap-3 border-b border-line pb-3">
          <span>Database</span>
          <span>{health.status === "ready" ? health.data.database : "pending"}</span>
        </li>
        <li className="flex items-center justify-between gap-3 border-b border-line pb-3">
          <span>Redis</span>
          <span>{health.status === "ready" ? health.data.redis : "pending"}</span>
        </li>
        <li className="flex items-center justify-between gap-3 border-b border-line pb-3">
          <span>Daytona</span>
          <span>{health.status === "ready" ? health.data.daytona : "pending"}</span>
        </li>
        <li className="flex items-center justify-between gap-3">
          <span>Workspace</span>
          <span className="capitalize">{workspaceStatus}</span>
        </li>
      </ul>
    </div>
  );
}

function LoadingState({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-dashed border-line bg-white p-6 text-ink/72">
      {message}
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-danger/30 bg-[#fff5f2] p-6 text-danger">
      {message}
    </div>
  );
}

function CreateWorkspacePanel({ onCreated }: { onCreated: (workspace: WorkspaceResponse) => void }) {
  const [name, setName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("main");
  const [snapshot, setSnapshot] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (!name.trim() || !repoUrl.trim() || !defaultBranch.trim()) {
      setError("Enter a workspace name, repository URL, and default branch.");
      return;
    }

    setIsSubmitting(true);
    try {
      const workspace = await createWorkspace({
        name,
        repo_url: repoUrl,
        default_branch: defaultBranch,
        snapshot: snapshot.trim() || null,
      });
      onCreated(workspace);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to create workspace.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form className="space-y-4" onSubmit={handleSubmit}>
      <div className="rounded-md border border-dashed border-line bg-white p-5 text-ink/72">
        No workspace has been created yet.
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <TextInput label="Workspace name" value={name} onChange={setName} />
        <TextInput label="Default branch" value={defaultBranch} onChange={setDefaultBranch} />
      </div>
      <TextInput label="Repository URL" value={repoUrl} onChange={setRepoUrl} />
      <TextInput
        label="Daytona snapshot (optional)"
        value={snapshot}
        onChange={setSnapshot}
      />

      {error && <ErrorState message={error} />}

      <button
        className="rounded-md bg-primary px-4 py-2 font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
        type="submit"
        disabled={isSubmitting}
      >
        {isSubmitting ? "Creating..." : "Create workspace"}
      </button>
    </form>
  );
}

function WorkspaceOverview({ workspace }: { workspace: WorkspaceResponse }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Metric label="Status" value={workspace.status} />
        <Metric label="Branch" value={workspace.default_branch} />
        <Metric label="Slug" value={workspace.slug} />
      </div>

      <div className="rounded-md border border-line bg-white p-4">
        <p className="text-sm text-ink/60">Daytona runtime</p>
        <p className="mt-2 break-all font-mono text-sm">
          {workspace.sandbox_id ?? "Provisioning"} at {workspace.workspace_path}
        </p>
      </div>
    </div>
  );
}

function WorkspaceSettings({
  workspace,
  envVars,
  onWorkspaceChanged,
  onEnvVarsChanged,
}: {
  workspace: WorkspaceResponse;
  envVars: WorkspaceEnvVarResponse[];
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
  onEnvVarsChanged: (envVars: WorkspaceEnvVarResponse[]) => void;
}) {
  return (
    <section className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
      <WorkspaceMetadataForm workspace={workspace} onWorkspaceChanged={onWorkspaceChanged} />
      <EnvVarManager workspace={workspace} envVars={envVars} onEnvVarsChanged={onEnvVarsChanged} />
    </section>
  );
}

type RepositoryState =
  | { status: "loading" }
  | { status: "ready"; data: RepositoryStatusResponse }
  | { status: "error"; message: string };

function RepositoryPanel({
  workspace,
  onWorkspaceChanged,
}: {
  workspace: WorkspaceResponse;
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
}) {
  const [repoUrl, setRepoUrl] = useState(workspace.repo_url);
  const [token, setToken] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [diff, setDiff] = useState("");
  const [repoState, setRepoState] = useState<RepositoryState>({ status: "loading" });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  useEffect(() => {
    setRepoUrl(workspace.repo_url);
  }, [workspace.repo_url]);

  const loadRepositoryStatus = useCallback(
    async (signal?: AbortSignal) => {
      setRepoState({ status: "loading" });
      try {
        const data = await getRepositoryStatus(workspace.id, signal);
        setRepoState({ status: "ready", data });
        setSelectedPath((currentPath) => {
          if (currentPath && data.files.some((file) => file.path === currentPath)) {
            return currentPath;
          }
          return data.files[0]?.path ?? null;
        });
      } catch (caughtError) {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setRepoState({
          status: "error",
          message:
            caughtError instanceof ApiError ? caughtError.message : "Unable to load repository.",
        });
      }
    },
    [workspace.id],
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadRepositoryStatus(controller.signal);
    return () => controller.abort();
  }, [loadRepositoryStatus]);

  const statusData = repoState.status === "ready" ? repoState.data : null;
  const selectedFile =
    selectedPath && statusData
      ? statusData.files.find((file) => file.path === selectedPath) ?? null
      : null;

  useEffect(() => {
    if (!selectedPath) {
      setDiff("");
      return;
    }

    const controller = new AbortController();
    getRepositoryDiff(workspace.id, selectedPath, controller.signal)
      .then((response) => setDiff(response.diff))
      .catch((caughtError: unknown) => {
        if (caughtError instanceof DOMException && caughtError.name === "AbortError") {
          return;
        }
        setDiff("");
        setError(caughtError instanceof ApiError ? caughtError.message : "Unable to load diff.");
      });

    return () => controller.abort();
  }, [selectedPath, workspace.id]);

  async function runAction(actionName: string, action: () => Promise<string>) {
    setBusyAction(actionName);
    setError(null);
    setMessage(null);
    try {
      const nextMessage = await action();
      setMessage(nextMessage);
      await loadRepositoryStatus();
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Repository action failed.");
    } finally {
      setBusyAction(null);
    }
  }

  async function handleConnect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!repoUrl.trim() || !token.trim()) {
      setError("Enter a GitHub repository URL and personal access token.");
      return;
    }
    await runAction("connect", async () => {
      const updated = await connectRepository(workspace.id, {
        repo_url: repoUrl,
        token,
      });
      onWorkspaceChanged(updated);
      setToken("");
      return "Repository credentials saved.";
    });
  }

  async function handleCommit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!commitMessage.trim()) {
      setError("Enter a commit message.");
      return;
    }
    await runAction("commit", async () => {
      const response = await commitRepository(workspace.id, { message: commitMessage });
      setCommitMessage("");
      return response.message;
    });
  }

  return (
    <section className="mt-4 grid gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
      <div className="rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-sm font-medium uppercase tracking-[0.16em] text-ink/55">
              Repository
            </p>
            <h3 className="mt-1 text-base font-semibold">Connection</h3>
          </div>
          <span className="rounded-full bg-white px-3 py-1 text-sm font-semibold text-ink/65">
            {statusData?.cloned ? statusData.branch ?? "Detached" : "Not cloned"}
          </span>
        </div>

        <form className="mt-4 space-y-3" onSubmit={handleConnect}>
          <TextInput label="GitHub repository URL" value={repoUrl} onChange={setRepoUrl} />
          <TextInput label="Personal access token" type="password" value={token} onChange={setToken} />
          <button
            className="rounded-md bg-primary px-4 py-2 font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="submit"
            disabled={busyAction === "connect"}
          >
            {busyAction === "connect" ? "Saving..." : "Connect repository"}
          </button>
        </form>

        <div className="mt-5 grid grid-cols-3 gap-2">
          <RepoActionButton
            label="Clone"
            disabled={busyAction !== null || statusData?.cloned === true}
            onClick={() =>
              void runAction("clone", async () => {
                const response = await cloneRepository(workspace.id);
                // Auto-setup the environment from mise.toml and start dev mode.
                // Fire-and-forget — the Preview tab polls progress and opens the preview.
                void startDevServer(workspace.id).catch(() => undefined);
                return `${response.message} Setting up the environment…`;
              })
            }
          />
          <RepoActionButton
            label="Pull"
            disabled={busyAction !== null || statusData?.cloned !== true}
            onClick={() =>
              void runAction("pull", async () => {
                const response = await pullRepository(workspace.id);
                return response.message;
              })
            }
          />
          <RepoActionButton
            label="Push"
            disabled={busyAction !== null || statusData?.cloned !== true}
            onClick={() =>
              void runAction("push", async () => {
                const response = await pushRepository(workspace.id);
                return response.message;
              })
            }
          />
        </div>

        <form className="mt-5 space-y-3" onSubmit={handleCommit}>
          <TextInput label="Commit message" value={commitMessage} onChange={setCommitMessage} />
          <button
            className="rounded-md bg-primary px-4 py-2 font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
            type="submit"
            disabled={busyAction === "commit" || statusData?.cloned !== true}
          >
            {busyAction === "commit" ? "Committing..." : "Commit changes"}
          </button>
        </form>

        {message && (
          <div className="mt-4 rounded-md border border-signal/30 bg-[#f0faf5] px-3 py-2 text-sm text-signal">
            {message}
          </div>
        )}
        {error && (
          <div className="mt-4">
            <ErrorState message={error} />
          </div>
        )}
      </div>

      <div className="rounded-md border border-line bg-white p-5 shadow-soft">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold">Changes</h3>
            <p className="mt-1 text-sm text-ink/60">
              {repoState.status === "ready"
                ? statusData?.is_clean
                  ? "Working tree clean"
                  : `${statusData?.files.length ?? 0} changed file${
                      statusData?.files.length === 1 ? "" : "s"
                    }`
                : "Checking repository"}
            </p>
          </div>
          <button
            className="rounded-md border border-line bg-white px-3 py-2 text-sm font-medium transition hover:bg-panel"
            type="button"
            onClick={() => void loadRepositoryStatus()}
          >
            Refresh
          </button>
        </div>

        {repoState.status === "loading" && (
          <div className="mt-4">
            <LoadingState message="Loading repository status..." />
          </div>
        )}
        {repoState.status === "error" && (
          <div className="mt-4">
            <ErrorState message={repoState.message} />
          </div>
        )}
        {repoState.status === "ready" && (
          <div className="mt-4 grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
            <div className="space-y-2">
              {repoState.data.files.length === 0 && (
                <div className="rounded-md border border-dashed border-line bg-white p-4 text-sm text-ink/65">
                  No changed files.
                </div>
              )}
              {repoState.data.files.map((file) => (
                <button
                  className={`flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left transition ${
                    file.path === selectedPath
                      ? "border-ink bg-white"
                      : "border-line bg-white hover:bg-panel"
                  }`}
                  key={`${file.status}-${file.path}`}
                  type="button"
                  onClick={() => setSelectedPath(file.path)}
                >
                  <span className="min-w-0 truncate font-mono text-sm">{file.path}</span>
                  <span className="shrink-0 rounded bg-line px-2 py-1 font-mono text-xs">
                    {file.status}
                  </span>
                </button>
              ))}
            </div>

            <div className="min-w-0 rounded-md border border-line bg-white">
              <div className="border-b border-line px-3 py-2">
                <p className="truncate font-mono text-sm font-semibold">
                  {selectedFile?.path ?? "No file selected"}
                </p>
              </div>
              <pre className="max-h-[460px] overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-5 text-ink/80">
                {diff || "Select a changed file to view its diff."}
              </pre>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

function RepoActionButton({
  label,
  disabled,
  onClick,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className="rounded-md border border-line bg-white px-3 py-2 text-sm font-semibold transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
      type="button"
      disabled={disabled}
      onClick={onClick}
    >
      {label}
    </button>
  );
}

function WorkspaceMetadataForm({
  workspace,
  onWorkspaceChanged,
}: {
  workspace: WorkspaceResponse;
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
}) {
  const [name, setName] = useState(workspace.name);
  const [repoUrl, setRepoUrl] = useState(workspace.repo_url);
  const [defaultBranch, setDefaultBranch] = useState(workspace.default_branch);
  const [snapshot, setSnapshot] = useState(workspace.snapshot ?? "");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    setName(workspace.name);
    setRepoUrl(workspace.repo_url);
    setDefaultBranch(workspace.default_branch);
    setSnapshot(workspace.snapshot ?? "");
  }, [workspace]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setMessage(null);
    setIsSubmitting(true);

    try {
      const updated = await updateWorkspace(workspace.id, {
        name,
        repo_url: repoUrl,
        default_branch: defaultBranch,
        snapshot: snapshot.trim() || null,
      });
      onWorkspaceChanged(updated);
      setMessage("Workspace settings saved.");
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to save workspace.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form className="rounded-md border border-line bg-white p-5 shadow-soft" onSubmit={handleSubmit}>
      <h3 className="text-base font-semibold">Workspace settings</h3>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <TextInput label="Workspace name" value={name} onChange={setName} />
        <TextInput label="Default branch" value={defaultBranch} onChange={setDefaultBranch} />
      </div>
      <div className="mt-4">
        <TextInput label="Repository URL" value={repoUrl} onChange={setRepoUrl} />
      </div>
      <div className="mt-4">
        <TextInput
          label="Daytona snapshot"
          value={snapshot}
          onChange={setSnapshot}
        />
      </div>

      {message && (
        <div className="mt-4 rounded-md border border-signal/30 bg-[#f0faf5] px-3 py-2 text-sm text-signal">
          {message}
        </div>
      )}
      {error && (
        <div className="mt-4">
          <ErrorState message={error} />
        </div>
      )}

      <button
        className="mt-4 rounded-md bg-primary px-4 py-2 font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
        type="submit"
        disabled={isSubmitting}
      >
        {isSubmitting ? "Saving..." : "Save settings"}
      </button>
    </form>
  );
}

function EnvVarManager({
  workspace,
  envVars,
  onEnvVarsChanged,
}: {
  workspace: WorkspaceResponse;
  envVars: WorkspaceEnvVarResponse[];
  onEnvVarsChanged: (envVars: WorkspaceEnvVarResponse[]) => void;
}) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (!key.trim()) {
      setError("Enter an environment variable key.");
      return;
    }

    setIsSubmitting(true);
    try {
      const saved = await upsertWorkspaceEnvVar(workspace.id, { key, value });
      onEnvVarsChanged([saved, ...envVars.filter((envVar) => envVar.key !== saved.key)]);
      setKey("");
      setValue("");
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to save variable.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(envKey: string) {
    setError(null);
    try {
      await deleteWorkspaceEnvVar(workspace.id, envKey);
      onEnvVarsChanged(envVars.filter((envVar) => envVar.key !== envKey));
    } catch (caughtError) {
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to delete variable.");
    }
  }

  return (
    <div className="rounded-md border border-line bg-white p-5 shadow-soft">
      <h3 className="text-base font-semibold">Environment variables</h3>
      <form className="mt-4 space-y-3" onSubmit={handleSubmit}>
        <TextInput label="Key" value={key} onChange={setKey} />
        <TextInput label="Value" type="password" value={value} onChange={setValue} />
        {error && <ErrorState message={error} />}
        <button
          className="rounded-md bg-primary px-4 py-2 font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
          type="submit"
          disabled={isSubmitting}
        >
          {isSubmitting ? "Saving..." : "Save variable"}
        </button>
      </form>

      <div className="mt-5 space-y-2">
        {envVars.length === 0 && (
          <div className="rounded-md border border-dashed border-line bg-white p-4 text-sm text-ink/65">
            No environment variables yet.
          </div>
        )}
        {envVars.map((envVar) => (
          <div
            className="flex items-center justify-between gap-3 rounded-md border border-line bg-white p-3"
            key={envVar.id}
          >
            <div className="min-w-0">
              <p className="truncate font-mono text-sm font-semibold">{envVar.key}</p>
              <p className="mt-1 font-mono text-sm text-ink/55">{envVar.masked_value}</p>
            </div>
            <button
              className="rounded-md border border-line px-3 py-1 text-sm font-medium transition hover:bg-panel"
              type="button"
              onClick={() => void handleDelete(envVar.key)}
            >
              Delete
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-line bg-white p-4">
      <p className="text-sm text-ink/60">{label}</p>
      <p className="mt-2 break-words text-xl font-semibold capitalize">{value}</p>
    </div>
  );
}

function TextInput({
  label,
  value,
  onChange,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: "text" | "password";
}) {
  return (
    <label className="block text-sm font-medium">
      {label}
      <input
        className="mt-2 w-full rounded-md border border-line bg-white px-3 py-2 outline-none transition focus:border-ink"
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
