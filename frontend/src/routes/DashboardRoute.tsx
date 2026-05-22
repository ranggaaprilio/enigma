import { type FormEvent, useEffect, useMemo, useState } from "react";

import { useAuth } from "../auth/context";
import { DashboardShell } from "../components/DashboardShell";
import {
  ApiError,
  createWorkspace,
  deleteWorkspaceEnvVar,
  getHealth,
  listWorkspaceEnvVars,
  listWorkspaces,
  updateWorkspace,
  upsertWorkspaceEnvVar,
  type HealthResponse,
  type WorkspaceEnvVarResponse,
  type WorkspaceResponse,
} from "../lib/api";

type HealthState =
  | { status: "loading" }
  | { status: "ready"; data: HealthResponse }
  | { status: "error"; message: string };

type WorkspaceState =
  | { status: "loading" }
  | { status: "ready"; workspace: WorkspaceResponse | null; envVars: WorkspaceEnvVarResponse[] }
  | { status: "error"; message: string };

export function DashboardRoute() {
  const { user, logout } = useAuth();
  const [health, setHealth] = useState<HealthState>({ status: "loading" });
  const [workspaceState, setWorkspaceState] = useState<WorkspaceState>({ status: "loading" });

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
  }, []);

  async function loadWorkspaceData(signal?: AbortSignal) {
    setWorkspaceState({ status: "loading" });
    try {
      const workspaces = await listWorkspaces(signal);
      const workspace = workspaces[0] ?? null;
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

  return (
    <DashboardShell
      healthLabel={shellStatus.label}
      healthTone={shellStatus.tone}
      workspace={workspace}
      workspaceStatus={workspaceStatus}
      user={user}
      onLogout={logout}
    >
      <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
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

          {workspaceState.status === "loading" && <LoadingState message="Loading workspace..." />}
          {workspaceState.status === "error" && <ErrorState message={workspaceState.message} />}
          {workspaceState.status === "ready" && !workspaceState.workspace && (
            <CreateWorkspacePanel onCreated={(created) => void loadWorkspaceAfterCreate(created)} />
          )}
          {workspaceState.status === "ready" && workspaceState.workspace && (
            <WorkspaceOverview workspace={workspaceState.workspace} />
          )}
        </div>

        <div className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
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
            <li className="flex items-center justify-between gap-3">
              <span>Workspace</span>
              <span className="capitalize">{workspaceStatus}</span>
            </li>
          </ul>
        </div>
      </section>

      {workspaceState.status === "ready" && workspaceState.workspace && (
        <WorkspaceSettings
          workspace={workspaceState.workspace}
          envVars={workspaceState.envVars}
          onWorkspaceChanged={(nextWorkspace) =>
            setWorkspaceState((current) =>
              current.status === "ready"
                ? { ...current, workspace: nextWorkspace }
                : current,
            )
          }
          onEnvVarsChanged={(envVars) =>
            setWorkspaceState((current) =>
              current.status === "ready" ? { ...current, envVars } : current,
            )
          }
        />
      )}
    </DashboardShell>
  );

  async function loadWorkspaceAfterCreate(created: WorkspaceResponse) {
    const envVars = await listWorkspaceEnvVars(created.id);
    setWorkspaceState({ status: "ready", workspace: created, envVars });
  }
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

      {error && <ErrorState message={error} />}

      <button
        className="rounded-md bg-ink px-4 py-2 font-semibold text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
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
        <p className="text-sm text-ink/60">Local path</p>
        <p className="mt-2 break-all font-mono text-sm">{workspace.local_path ?? "Pending"}</p>
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
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    setName(workspace.name);
    setRepoUrl(workspace.repo_url);
    setDefaultBranch(workspace.default_branch);
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
    <form className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft" onSubmit={handleSubmit}>
      <h3 className="text-base font-semibold">Workspace settings</h3>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <TextInput label="Workspace name" value={name} onChange={setName} />
        <TextInput label="Default branch" value={defaultBranch} onChange={setDefaultBranch} />
      </div>
      <div className="mt-4">
        <TextInput label="Repository URL" value={repoUrl} onChange={setRepoUrl} />
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
        className="mt-4 rounded-md bg-ink px-4 py-2 font-semibold text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
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
    <div className="rounded-md border border-line bg-[#fffdf8] p-5 shadow-soft">
      <h3 className="text-base font-semibold">Environment variables</h3>
      <form className="mt-4 space-y-3" onSubmit={handleSubmit}>
        <TextInput label="Key" value={key} onChange={setKey} />
        <TextInput label="Value" type="password" value={value} onChange={setValue} />
        {error && <ErrorState message={error} />}
        <button
          className="rounded-md bg-ink px-4 py-2 font-semibold text-white transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:bg-ink/45"
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
              className="rounded-md border border-line px-3 py-1 text-sm font-medium transition hover:bg-[#f5f1e8]"
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
