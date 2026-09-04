import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import {
  ArrowUpRight,
  Box,
  Boxes,
  Clock,
  FolderGit2,
  GitBranch,
  LayoutTemplate,
  Pencil,
  Plus,
  Users,
} from "lucide-react";

import { AppShell } from "../components/AppShell";
import { BudgetConfig } from "../components/usage/BudgetConfig";
import { UsagePanel } from "../components/usage/UsagePanel";
import { CreateWorkspaceWizard } from "../components/workspace/CreateWorkspaceWizard";
import { useAuth } from "../auth/context";
import {
  createTeamWorkspace,
  getProject,
  getTeam,
  listProjectMembers,
  listTeamMembers,
  listTeamWorkspaces,
  updateTeam,
  type ProjectResponse,
  type TeamResponse,
  type WorkspaceResponse,
  type WorkspaceStatus,
} from "../lib/api";

const STATUS_ORDER: WorkspaceStatus[] = [
  "ready",
  "busy",
  "creating",
  "stopped",
  "error",
];

const statusMeta: Record<WorkspaceStatus, { dot: string; pill: string }> = {
  ready: {
    dot: "bg-signal",
    pill: "bg-[#f0faf5] text-signal ring-1 ring-signal/30",
  },
  busy: { dot: "bg-warn", pill: "bg-[#fdf6ec] text-warn ring-1 ring-warn/30" },
  creating: {
    dot: "bg-warn",
    pill: "bg-[#fdf6ec] text-warn ring-1 ring-warn/30",
  },
  stopped: {
    dot: "bg-muted-soft",
    pill: "bg-white text-muted ring-1 ring-line",
  },
  error: {
    dot: "bg-danger",
    pill: "bg-[#fff5f2] text-danger ring-1 ring-danger/30",
  },
};

function repoLabel(ws: WorkspaceResponse) {
  if (ws.repo_url) {
    return ws.repo_url.replace(/^(https?|ssh):\/\/(git@)?/, "").replace(/\.git$/, "");
  }
  return ws.source === "template" ? "From template — no remote yet" : "Blank workspace";
}

function RepoIcon({ ws }: { ws: WorkspaceResponse }) {
  const className = "h-3.5 w-3.5 shrink-0";
  if (ws.repo_url) return <FolderGit2 className={className} aria-hidden="true" />;
  if (ws.source === "template") return <LayoutTemplate className={className} aria-hidden="true" />;
  return <Box className={className} aria-hidden="true" />;
}

function formatDate(value: string) {
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function TeamRoute() {
  const { projectId, teamId } = useParams<{
    projectId: string;
    teamId: string;
  }>();
  const navigate = useNavigate();
  const [project, setProject] = useState<ProjectResponse | null>(null);
  const [team, setTeam] = useState<TeamResponse | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  // Mirrors `BudgetService._assert_manage_access` for the team scope: a team lead or a
  // project admin. Hardcoding this to true showed create/edit/delete controls to every
  // member — including a team viewer — that the backend then always rejected.
  const [canManageBudget, setCanManageBudget] = useState(false);
  const { user } = useAuth();

  useEffect(() => {
    if (!projectId || !teamId) return;
    const controller = new AbortController();
    Promise.all([
      getProject(projectId, controller.signal),
      getTeam(projectId, teamId, controller.signal),
      listTeamWorkspaces(projectId, teamId, controller.signal),
      // Role lookups are advisory for the UI only — the backend re-checks every mutation —
      // so a failure just leaves the budget controls hidden rather than failing the page.
      listTeamMembers(projectId, teamId, controller.signal).catch(() => []),
      listProjectMembers(projectId, controller.signal).catch(() => []),
    ])
      .then(([proj, t, wsList, teamMembers, projectMembers]) => {
        setProject(proj);
        setTeam(t);
        setWorkspaces(wsList);
        setCanManageBudget(
          teamMembers.some(
            (member) => member.user_id === user?.id && member.role === "lead",
          ) ||
            projectMembers.some(
              (member) => member.user_id === user?.id && member.role === "admin",
            ),
        );
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load team");
        setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, teamId, user?.id]);

  function openEdit() {
    setEditName(team?.name ?? "");
    setEditDesc(team?.description ?? "");
    setError(null);
    setShowEdit(true);
  }

  async function handleEditTeam(e: FormEvent) {
    e.preventDefault();
    if (!projectId || !teamId || !editName.trim()) return;
    setSaving(true);
    try {
      const updated = await updateTeam(projectId, teamId, {
        name: editName.trim(),
        description: editDesc.trim() || null,
      });
      setTeam(updated);
      setShowEdit(false);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to update team");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <AppShell projectId={projectId} teamId={teamId}>
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-muted">Loading…</p>
        </div>
      </AppShell>
    );
  }

  const statusCounts = workspaces.reduce<
    Partial<Record<WorkspaceStatus, number>>
  >((acc, ws) => {
    acc[ws.status] = (acc[ws.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <AppShell
      projectId={projectId}
      teamId={teamId}
      projectName={project?.name}
      teamName={team?.name}
    >
      <header className="flex items-center gap-2 border-b border-line bg-panel px-6 py-3 text-sm text-body">
        <Link to="/projects" className="hover:text-ink">
          Projects
        </Link>
        <span className="text-muted-soft">/</span>
        <Link to={`/projects/${projectId}`} className="hover:text-ink">
          {project?.name}
        </Link>
        <span className="text-muted-soft">/</span>
        <span className="font-medium text-ink">{team?.name}</span>
      </header>

      <main className="mx-auto w-full max-w-5xl px-6 py-10">
        {error && (
          <div className="mb-6 rounded-lg border border-danger/30 bg-[#fff5f2] px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        {/* Team hero */}
        <section className="mb-8 rounded-xl border border-line bg-panel p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex items-start gap-4">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Users className="h-6 w-6" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <h1 className="text-2xl font-semibold text-ink">
                  {team?.name}
                </h1>
                {team?.description ? (
                  <p className="mt-1 max-w-prose text-sm text-body">
                    {team.description}
                  </p>
                ) : (
                  <p className="mt-1 text-sm text-muted">No description</p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                  <span className="inline-flex items-center gap-1.5">
                    <Boxes className="h-3.5 w-3.5" aria-hidden="true" />
                    {workspaces.length}{" "}
                    {workspaces.length === 1 ? "workspace" : "workspaces"}
                  </span>
                  {team?.created_at && (
                    <span className="inline-flex items-center gap-1.5">
                      <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                      Created {formatDate(team.created_at)}
                    </span>
                  )}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={openEdit}
                className="inline-flex items-center gap-2 rounded-lg border border-line bg-white px-3 py-2 text-sm font-medium text-ink transition hover:bg-panel"
              >
                <Pencil className="h-4 w-4" aria-hidden="true" />
                Edit
              </button>
              <Link
                to={`/projects/${projectId}/teams/${teamId}/members`}
                className="inline-flex items-center gap-2 rounded-lg border border-line bg-white px-3 py-2 text-sm font-medium text-ink transition hover:bg-panel"
              >
                <Users className="h-4 w-4" aria-hidden="true" />
                Members
              </Link>
              <button
                onClick={() => setShowCreate(true)}
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-active"
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                New workspace
              </button>
            </div>
          </div>

          {workspaces.length > 0 && (
            <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
              {STATUS_ORDER.filter((s) => (statusCounts[s] ?? 0) > 0).map(
                (s) => (
                  <span
                    key={s}
                    className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${statusMeta[s].pill}`}
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${statusMeta[s].dot}`}
                    />
                    {statusCounts[s]} {s}
                  </span>
                ),
              )}
            </div>
          )}
        </section>

        {/* Workspaces */}
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
          Workspaces
        </h2>

        {workspaces.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line bg-panel py-16 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-canvas text-muted">
              <Boxes className="h-6 w-6" aria-hidden="true" />
            </div>
            <p className="font-medium text-ink">No workspaces yet</p>
            <p className="mt-1 text-sm text-body">
              Create a workspace to spin up a Daytona sandbox for this team.
            </p>
            <button
              onClick={() => setShowCreate(true)}
              className="mt-4 inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-active"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Create workspace
            </button>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {workspaces.map((ws) => (
              <Link
                key={ws.id}
                to={`/projects/${projectId}/teams/${teamId}/workspaces/${ws.id}`}
                className="group flex flex-col rounded-xl border border-line bg-panel p-5 transition hover:border-primary hover:shadow-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-canvas text-body transition group-hover:text-primary">
                      <Box className="h-5 w-5" aria-hidden="true" />
                    </div>
                    <h3 className="truncate font-semibold text-ink group-hover:text-primary">
                      {ws.name}
                    </h3>
                  </div>
                  <span
                    className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${statusMeta[ws.status]?.pill ?? "bg-white text-muted ring-1 ring-line"}`}
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${statusMeta[ws.status]?.dot ?? "bg-muted-soft"}`}
                    />
                    {ws.status}
                  </span>
                </div>

                <div className="mt-4 space-y-2 text-xs text-muted">
                  <p className="flex items-center gap-1.5">
                    <RepoIcon ws={ws} />
                    <span className="truncate">{repoLabel(ws)}</span>
                  </p>
                  <p className="flex items-center gap-1.5">
                    <GitBranch
                      className="h-3.5 w-3.5 shrink-0"
                      aria-hidden="true"
                    />
                    {ws.default_branch}
                  </p>
                </div>

                <div className="mt-4 flex items-center justify-between border-t border-line pt-3 text-xs text-muted">
                  <span className="inline-flex items-center gap-1.5">
                    <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                    {formatDate(ws.created_at)}
                  </span>
                  <ArrowUpRight
                    className="h-4 w-4 text-muted transition group-hover:text-primary"
                    aria-hidden="true"
                  />
                </div>
              </Link>
            ))}
          </div>
        )}

        {teamId && (
          <div className="mt-10 flex flex-col gap-4">
            <UsagePanel scope="team" scopeId={teamId} title="Team usage" />
            <BudgetConfig scope="team" scopeId={teamId} canManage={canManageBudget} />
          </div>
        )}
      </main>

      {projectId && teamId && (
        <CreateWorkspaceWizard
          open={showCreate}
          onClose={() => setShowCreate(false)}
          subtitle="Provisions a Daytona sandbox for this team."
          createFn={(payload) => createTeamWorkspace(projectId, teamId, payload)}
          onCreated={(ws) =>
            navigate(`/projects/${projectId}/teams/${teamId}/workspaces/${ws.id}`)
          }
        />
      )}

      {showEdit && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
          <div className="w-full max-w-md rounded-xl border border-line bg-panel p-6 shadow-lg">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Pencil className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 className="text-lg font-semibold text-ink">Edit team</h2>
                <p className="text-xs text-muted">Rename or update the description.</p>
              </div>
            </div>
            <form onSubmit={handleEditTeam} className="space-y-3">
              <div>
                <label className="mb-1 block text-sm font-medium text-body">
                  Name
                </label>
                <input
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
                  placeholder="Frontend"
                  autoFocus
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-body">
                  Description (optional)
                </label>
                <textarea
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  rows={2}
                  className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowEdit(false)}
                  className="rounded-lg px-4 py-2 text-sm text-body hover:bg-line"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={saving || !editName.trim()}
                  className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
                >
                  {saving ? "Saving…" : "Save changes"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </AppShell>
  );
}
