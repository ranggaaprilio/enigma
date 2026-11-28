import { useEffect, useState } from "react";

import {
  ApiError,
  listCustomCommands,
  updateWorkspace,
  type WorkspaceResponse,
} from "../../lib/api";

/** Workspace toggle for verifying every completed agent run; saved as soon as it changes. */
export function AutoVerifySetting({
  workspace,
  onWorkspaceChanged,
}: {
  workspace: WorkspaceResponse;
  onWorkspaceChanged: (workspace: WorkspaceResponse) => void;
}) {
  const [enabled, setEnabled] = useState(workspace.auto_verify_agent_runs);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // null until the commands load; a failed load shows no hint rather than a wrong one.
  const [hasTestCommand, setHasTestCommand] = useState<boolean | null>(null);

  useEffect(() => {
    setEnabled(workspace.auto_verify_agent_runs);
  }, [workspace.auto_verify_agent_runs]);

  useEffect(() => {
    const controller = new AbortController();
    listCustomCommands(workspace.id, controller.signal)
      .then((commands) =>
        setHasTestCommand(commands.some((command) => command.role === "test" && command.enabled)),
      )
      .catch(() => undefined);
    return () => controller.abort();
  }, [workspace.id]);

  async function handleChange(next: boolean) {
    setEnabled(next);
    setBusy(true);
    setError(null);
    try {
      const updated = await updateWorkspace(workspace.id, { auto_verify_agent_runs: next });
      setEnabled(updated.auto_verify_agent_runs);
      onWorkspaceChanged(updated);
    } catch (caughtError) {
      setEnabled(!next);
      setError(caughtError instanceof ApiError ? caughtError.message : "Unable to save setting.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-md border border-line bg-white p-5">
      <label className="flex items-center gap-2 text-sm font-semibold">
        <input
          type="checkbox"
          checked={enabled}
          disabled={busy}
          onChange={(event) => void handleChange(event.target.checked)}
        />
        Auto-verify agent runs
      </label>
      <p className="mt-2 text-xs text-ink/60">
        Runs the workspace's test command after every completed agent run and lets an agent fix
        failures.
      </p>
      {hasTestCommand === false && (
        <p className="mt-1 text-xs text-muted">
          Auto-verify needs a custom command with role "test". Add one under Commands.
        </p>
      )}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </div>
  );
}
