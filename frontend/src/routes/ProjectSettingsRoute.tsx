import { type FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import {
  deleteProject,
  getProject,
  updateProject,
  type ProjectResponse,
} from "../lib/api";

export function ProjectSettingsRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [project, setProject] = useState<ProjectResponse | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    getProject(projectId, controller.signal).then((p) => {
      setProject(p);
      setName(p.name);
      setDescription(p.description ?? "");
    });
    return () => controller.abort();
  }, [projectId]);

  async function handleSave(e: FormEvent) {
    e.preventDefault();
    if (!projectId) return;
    setSaving(true);
    try {
      const updated = await updateProject(projectId, {
        name: name.trim(),
        description: description.trim() || null,
      });
      setProject(updated);
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!projectId) return;
    setDeleting(true);
    try {
      await deleteProject(projectId);
      navigate("/");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to delete");
      setDeleting(false);
    }
  }

  return (
    <div className="min-h-screen bg-canvas">
      <header className="flex items-center gap-2 border-b border-line bg-panel px-6 py-3 text-sm text-body">
        <Link to="/" className="hover:text-ink">Projects</Link>
        <span>/</span>
        <Link to={`/projects/${projectId}`} className="hover:text-ink">{project?.name}</Link>
        <span>/</span>
        <span className="font-medium text-ink">Settings</span>
      </header>

      <main className="mx-auto max-w-2xl px-6 py-10">
        <h1 className="mb-6 text-2xl font-semibold text-ink">Project settings</h1>

        {error && (
          <div className="mb-4 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        <form onSubmit={handleSave} className="space-y-4 rounded-xl border border-line bg-panel p-6">
          <div>
            <label className="mb-1 block text-sm font-medium text-body">Name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
              required
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-body">Description</label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="w-full rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>
          <div className="flex justify-end">
            <button
              type="submit"
              disabled={saving}
              className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
            >
              {saving ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>

        <div className="mt-8 rounded-xl border border-danger/30 bg-panel p-6">
          <h2 className="mb-2 font-semibold text-danger">Danger zone</h2>
          <p className="mb-4 text-sm text-body">
            Deleting this project will permanently remove all teams and workspaces within it.
          </p>
          {confirmDelete ? (
            <div className="flex gap-2">
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="rounded-lg bg-danger px-4 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-60"
              >
                {deleting ? "Deleting…" : "Yes, delete"}
              </button>
              <button
                onClick={() => setConfirmDelete(false)}
                className="rounded-lg px-4 py-2 text-sm text-body hover:bg-line"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmDelete(true)}
              className="rounded-lg border border-danger/50 px-4 py-2 text-sm font-medium text-danger hover:bg-danger/10"
            >
              Delete project
            </button>
          )}
        </div>
      </main>
    </div>
  );
}
