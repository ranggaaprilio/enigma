import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import {
  ArrowUpRight,
  Clock,
  FolderKanban,
  Plus,
  Settings,
  Users,
} from "lucide-react";

import { AppShell } from "../components/AppShell";
import {
  createTeam,
  getProject,
  listTeams,
  type ProjectResponse,
  type TeamResponse,
} from "../lib/api";

function formatDate(value: string) {
  return new Date(value).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

export function ProjectRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [project, setProject] = useState<ProjectResponse | null>(null);
  const [teams, setTeams] = useState<TeamResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    Promise.all([
      getProject(projectId, controller.signal),
      listTeams(projectId, controller.signal),
    ])
      .then(([proj, teamList]) => {
        setProject(proj);
        setTeams(teamList);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load project");
        setLoading(false);
      });
    return () => controller.abort();
  }, [projectId]);

  async function handleCreateTeam(e: FormEvent) {
    e.preventDefault();
    if (!projectId || !newName.trim()) return;
    setCreating(true);
    try {
      const team = await createTeam(projectId, {
        name: newName.trim(),
        description: newDesc.trim() || null,
      });
      navigate(`/projects/${projectId}/teams/${team.id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create team");
      setCreating(false);
    }
  }

  if (loading) {
    return (
      <AppShell projectId={projectId}>
        <div className="flex flex-1 items-center justify-center">
          <p className="text-sm text-muted">Loading…</p>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell projectId={projectId} projectName={project?.name}>
      <header className="flex items-center gap-2 border-b border-line bg-panel px-6 py-3 text-sm text-body">
        <Link to="/projects" className="hover:text-ink">
          Projects
        </Link>
        <span className="text-muted-soft">/</span>
        <span className="font-medium text-ink">{project?.name}</span>
      </header>

      <main className="mx-auto w-full max-w-5xl px-6 py-10">
        {error && (
          <div className="mb-6 rounded-lg border border-danger/30 bg-[#fff5f2] px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        {/* Project hero */}
        <section className="mb-8 rounded-xl border border-line bg-panel p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex items-start gap-4">
              <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <FolderKanban className="h-6 w-6" aria-hidden="true" />
              </div>
              <div className="min-w-0">
                <h1 className="text-2xl font-semibold text-ink">
                  {project?.name}
                </h1>
                {project?.description ? (
                  <p className="mt-1 max-w-prose text-sm text-body">
                    {project.description}
                  </p>
                ) : (
                  <p className="mt-1 text-sm text-muted">No description</p>
                )}
                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                  <span className="inline-flex items-center gap-1.5">
                    <Users className="h-3.5 w-3.5" aria-hidden="true" />
                    {teams.length} {teams.length === 1 ? "team" : "teams"}
                  </span>
                  {project?.created_at && (
                    <span className="inline-flex items-center gap-1.5">
                      <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                      Created {formatDate(project.created_at)}
                    </span>
                  )}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Link
                to={`/projects/${projectId}/members`}
                className="inline-flex items-center gap-2 rounded-lg border border-line bg-white px-3 py-2 text-sm font-medium text-ink transition hover:bg-panel"
              >
                <Users className="h-4 w-4" aria-hidden="true" />
                Members
              </Link>
              <Link
                to={`/projects/${projectId}/settings`}
                className="inline-flex items-center gap-2 rounded-lg border border-line bg-white px-3 py-2 text-sm font-medium text-ink transition hover:bg-panel"
              >
                <Settings className="h-4 w-4" aria-hidden="true" />
                Settings
              </Link>
              <button
                onClick={() => setShowCreate(true)}
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-active"
              >
                <Plus className="h-4 w-4" aria-hidden="true" />
                New team
              </button>
            </div>
          </div>
        </section>

        {/* Teams */}
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted">
          Teams
        </h2>

        {teams.length === 0 ? (
          <div className="rounded-xl border border-dashed border-line bg-panel py-16 text-center">
            <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-canvas text-muted">
              <Users className="h-6 w-6" aria-hidden="true" />
            </div>
            <p className="font-medium text-ink">No teams yet</p>
            <p className="mt-1 text-sm text-body">
              Create a team to group people and their workspaces.
            </p>
            <button
              onClick={() => setShowCreate(true)}
              className="mt-4 inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-active"
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              Create team
            </button>
          </div>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {teams.map((t) => (
              <Link
                key={t.id}
                to={`/projects/${projectId}/teams/${t.id}`}
                className="group flex flex-col rounded-xl border border-line bg-panel p-5 transition hover:border-primary hover:shadow-sm"
              >
                <div className="flex items-start gap-3">
                  <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-canvas text-body transition group-hover:text-primary">
                    <Users className="h-5 w-5" aria-hidden="true" />
                  </div>
                  <div className="min-w-0">
                    <h3 className="truncate font-semibold text-ink group-hover:text-primary">
                      {t.name}
                    </h3>
                    {t.description && (
                      <p className="mt-1 line-clamp-2 text-sm text-body">
                        {t.description}
                      </p>
                    )}
                  </div>
                </div>

                <div className="mt-4 flex items-center justify-between border-t border-line pt-3 text-xs text-muted">
                  <span className="inline-flex items-center gap-1.5">
                    <Clock className="h-3.5 w-3.5" aria-hidden="true" />
                    {formatDate(t.created_at)}
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
      </main>

      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
          <div className="w-full max-w-md rounded-xl border border-line bg-panel p-6 shadow-lg">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Users className="h-5 w-5" aria-hidden="true" />
              </div>
              <div>
                <h2 className="text-lg font-semibold text-ink">New team</h2>
                <p className="text-xs text-muted">Groups people and workspaces.</p>
              </div>
            </div>
            <form onSubmit={handleCreateTeam} className="space-y-3">
              <div>
                <label className="mb-1 block text-sm font-medium text-body">
                  Name
                </label>
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
                  placeholder="Frontend"
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-body">
                  Description (optional)
                </label>
                <textarea
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  rows={2}
                  className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
              </div>
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="rounded-lg px-4 py-2 text-sm text-body hover:bg-line"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={creating}
                  className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
                >
                  {creating ? "Creating…" : "Create"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </AppShell>
  );
}
