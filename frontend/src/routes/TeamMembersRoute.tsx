import { type FormEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router";

import {
  addTeamMember,
  getProject,
  getTeam,
  listTeamMembers,
  removeTeamMember,
  updateTeamMemberRole,
  type ProjectResponse,
  type TeamMemberResponse,
  type TeamResponse,
  type TeamRole,
} from "../lib/api";

export function TeamMembersRoute() {
  const { projectId, teamId } = useParams<{
    projectId: string;
    teamId: string;
  }>();
  const [project, setProject] = useState<ProjectResponse | null>(null);
  const [team, setTeam] = useState<TeamResponse | null>(null);
  const [members, setMembers] = useState<TeamMemberResponse[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newUserId, setNewUserId] = useState("");
  const [newRole, setNewRole] = useState<TeamRole>("member");
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    if (!projectId || !teamId) return;
    const controller = new AbortController();
    Promise.all([
      getProject(projectId, controller.signal),
      getTeam(projectId, teamId, controller.signal),
      listTeamMembers(projectId, teamId, controller.signal),
    ]).then(([p, t, m]) => {
      setProject(p);
      setTeam(t);
      setMembers(m);
    });
    return () => controller.abort();
  }, [projectId, teamId]);

  async function handleAddMember(e: FormEvent) {
    e.preventDefault();
    if (!projectId || !teamId || !newUserId.trim()) return;
    setAdding(true);
    try {
      const member = await addTeamMember(projectId, teamId, {
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

  async function handleRoleChange(userId: string, role: TeamRole) {
    if (!projectId || !teamId) return;
    try {
      const updated = await updateTeamMemberRole(projectId, teamId, userId, {
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
    if (!projectId || !teamId) return;
    try {
      await removeTeamMember(projectId, teamId, userId);
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
        <Link
          to={`/projects/${projectId}/teams/${teamId}`}
          className="hover:text-ink"
        >
          {team?.name}
        </Link>
        <span>/</span>
        <span className="font-medium text-ink">Members</span>
      </header>

      <main className="mx-auto max-w-2xl px-6 py-10">
        <h1 className="mb-6 text-2xl font-semibold text-ink">Team members</h1>

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
            onChange={(e) => setNewRole(e.target.value as TeamRole)}
            className="rounded-lg border border-line bg-panel px-3 py-2 text-sm text-ink"
          >
            <option value="lead">Lead</option>
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
                  handleRoleChange(m.user_id, e.target.value as TeamRole)
                }
                className="rounded-md border border-line bg-canvas px-2 py-1 text-xs"
              >
                <option value="lead">Lead</option>
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
