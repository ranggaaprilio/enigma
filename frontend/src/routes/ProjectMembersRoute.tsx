import { type FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router";

import {
  addProjectMember,
  getProject,
  listProjectMembers,
  removeProjectMember,
  updateProjectMemberRole,
  type ProjectMemberResponse,
  type ProjectResponse,
  type ProjectRole,
} from "../lib/api";

export function ProjectMembersRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  const [project, setProject] = useState<ProjectResponse | null>(null);
  const [members, setMembers] = useState<ProjectMemberResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newUserId, setNewUserId] = useState("");
  const [newRole, setNewRole] = useState<ProjectRole>("member");
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    Promise.all([
      getProject(projectId, controller.signal),
      listProjectMembers(projectId, controller.signal),
    ]).then(([p, m]) => {
      setProject(p);
      setMembers(m);
    });
    return () => controller.abort();
  }, [projectId]);

  async function handleAddMember(e: FormEvent) {
    e.preventDefault();
    if (!projectId || !newUserId.trim()) return;
    setAdding(true);
    try {
      const member = await addProjectMember(projectId, {
        user_id: newUserId.trim(),
        role: newRole,
      });
      setMembers((prev) => [...prev, member]);
      setNewUserId("");
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to add member");
    } finally {
      setAdding(false);
    }
  }

  async function handleRoleChange(userId: string, role: ProjectRole) {
    if (!projectId) return;
    try {
      const updated = await updateProjectMemberRole(projectId, userId, {
        role,
      });
      setMembers((prev) =>
        prev.map((m) => (m.user_id === userId ? updated : m)),
      );
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to update role");
    }
  }

  async function handleRemove(userId: string) {
    if (!projectId) return;
    try {
      await removeProjectMember(projectId, userId);
      setMembers((prev) => prev.filter((m) => m.user_id !== userId));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to remove member");
    }
  }

  return (
    <div className="min-h-screen bg-canvas">
      <header className="flex items-center gap-2 border-b border-line bg-panel px-6 py-3 text-sm text-body">
        <Link to="/" className="hover:text-ink">
          Projects
        </Link>
        <span>/</span>
        <Link to={`/projects/${projectId}`} className="hover:text-ink">
          {project?.name}
        </Link>
        <span>/</span>
        <span className="font-medium text-ink">Members</span>
      </header>

      <main className="mx-auto max-w-2xl px-6 py-10">
        <h1 className="mb-6 text-2xl font-semibold text-ink">
          Project members
        </h1>

        {error && (
          <div className="mb-4 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
            {error}
          </div>
        )}

        <form onSubmit={handleAddMember} className="mb-6 flex gap-2">
          <input
            value={newUserId}
            onChange={(e) => setNewUserId(e.target.value)}
            placeholder="User ID"
            className="flex-1 rounded-lg border border-line bg-panel px-3 py-2 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-primary/50"
          />
          <select
            value={newRole}
            onChange={(e) => setNewRole(e.target.value as ProjectRole)}
            className="rounded-lg border border-line bg-panel px-3 py-2 text-sm text-ink"
          >
            <option value="admin">Admin</option>
            <option value="member">Member</option>
            <option value="viewer">Viewer</option>
          </select>
          <button
            type="submit"
            disabled={adding}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-white hover:bg-primary-active disabled:opacity-60"
          >
            {adding ? "Adding…" : "Add"}
          </button>
        </form>

        <div className="rounded-xl border border-line bg-panel divide-y divide-line">
          {members.map((m) => (
            <div key={m.id} className="flex items-center gap-3 px-4 py-3">
              <span className="flex-1 text-sm font-mono text-ink">
                {m.user_id}
              </span>
              <select
                value={m.role}
                onChange={(e) =>
                  handleRoleChange(m.user_id, e.target.value as ProjectRole)
                }
                className="rounded-md border border-line bg-canvas px-2 py-1 text-xs"
              >
                <option value="admin">Admin</option>
                <option value="member">Member</option>
                <option value="viewer">Viewer</option>
              </select>
              <button
                onClick={() => handleRemove(m.user_id)}
                className="text-xs text-danger hover:underline"
              >
                Remove
              </button>
            </div>
          ))}
          {members.length === 0 && (
            <p className="px-4 py-6 text-sm text-muted">No members yet.</p>
          )}
        </div>
      </main>
    </div>
  );
}
