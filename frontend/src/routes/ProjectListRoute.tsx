import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router";

import { AppShell } from "../components/AppShell";
import {
  createProject,
  listProjects,
  type ProjectResponse,
} from "../lib/api";

export function ProjectListRoute() {
  const navigate = useNavigate();
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    listProjects(controller.signal)
      .then((data) => {
        setProjects(data);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load projects");
        setLoading(false);
      });
    return () => controller.abort();
  }, []);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const project = await createProject({ name: newName.trim(), description: newDesc.trim() || null });
      navigate(`/projects/${project.id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create project");
      setCreating(false);
    }
  }

  return (
    <AppShell>
      <main className="mx-auto w-full max-w-4xl px-6 py-10">
        <div className="mb-8 flex items-center justify-between">
          <h1 className="text-2xl font-semibold text-ink">Projects</h1>
          <button
            onClick={() => setShowCreate(true)}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active"
          >
            New project
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        {loading && (
          <p className="text-sm text-muted">Loading projects…</p>
        )}

        {!loading && projects.length === 0 && (
          <div className="rounded-xl border border-line bg-panel py-16 text-center">
            <p className="text-body">No projects yet.</p>
            <button
              onClick={() => setShowCreate(true)}
              className="mt-3 text-sm font-medium text-primary hover:underline"
            >
              Create your first project
            </button>
          </div>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          {projects.map((p) => (
            <Link
              key={p.id}
              to={`/projects/${p.id}`}
              className="group rounded-xl border border-line bg-panel p-5 transition hover:border-primary hover:shadow-sm"
            >
              <h2 className="font-semibold text-ink group-hover:text-primary">{p.name}</h2>
              {p.description && (
                <p className="mt-1 text-sm text-body line-clamp-2">{p.description}</p>
              )}
              <p className="mt-3 text-xs text-muted">
                Created {new Date(p.created_at).toLocaleDateString()}
              </p>
            </Link>
          ))}
        </div>
      </main>

      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-4">
          <div className="w-full max-w-md rounded-xl border border-line bg-panel p-6 shadow-lg">
            <h2 className="mb-4 text-lg font-semibold text-ink">New project</h2>
            <form onSubmit={handleCreate} className="space-y-3">
              <div>
                <label className="mb-1 block text-sm font-medium text-body">Name</label>
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
                  placeholder="My project"
                  required
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-body">Description (optional)</label>
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
                  className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
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
