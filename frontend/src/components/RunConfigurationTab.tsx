import { type FormEvent, useCallback, useEffect, useState } from "react";
import { FolderTree, Loader2, Search, Sparkles } from "lucide-react";

import {
  ApiError,
  applyRunPreset,
  detectRunCommands,
  importRunCommands,
  listRunPresets,
  type CustomCommandResponse,
  type DetectedCommand,
  type RunPresetResponse,
  type WorkspaceResponse,
} from "../lib/api";

/**
 * The "what to run" half of the Commands view: where in the repository the workspace lives,
 * plus the two ways to fill the registry without typing commands by hand.
 *
 * Detection never persists anything on its own — candidates land in a review list the user
 * ticks, which is why each row shows the manifest it came from.
 */
export function RunConfigurationTab({
  workspace,
  appRoot,
  canManage,
  worktreeId,
  onSaveAppRoot,
  onCommandsImported,
}: {
  workspace: WorkspaceResponse;
  appRoot: string;
  canManage: boolean;
  worktreeId: string | null;
  onSaveAppRoot: (value: string) => Promise<void>;
  onCommandsImported: (created: CustomCommandResponse[]) => void;
}) {
  const [appRootDraft, setAppRootDraft] = useState(appRoot);
  const [isSavingAppRoot, setIsSavingAppRoot] = useState(false);
  const [appRootSaved, setAppRootSaved] = useState(false);

  const [candidates, setCandidates] = useState<DetectedCommand[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [isDetecting, setIsDetecting] = useState(false);
  const [isImporting, setIsImporting] = useState(false);

  const [presets, setPresets] = useState<RunPresetResponse[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setAppRootDraft(appRoot);
  }, [appRoot]);

  useEffect(() => {
    const controller = new AbortController();
    listRunPresets(workspace.id, controller.signal)
      .then(setPresets)
      .catch(() => undefined);
    return () => controller.abort();
  }, [workspace.id]);

  const candidateKey = useCallback(
    (candidate: DetectedCommand) => `${candidate.cwd ?? ""}::${candidate.command}`,
    [],
  );

  async function submitAppRoot(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSavingAppRoot(true);
    setAppRootSaved(false);
    setError(null);
    try {
      await onSaveAppRoot(appRootDraft);
      setAppRootSaved(true);
    } catch {
      // The parent surfaces the message; keep the draft so the user can correct it.
    } finally {
      setIsSavingAppRoot(false);
    }
  }

  async function detect() {
    setIsDetecting(true);
    setError(null);
    try {
      const response = await detectRunCommands(workspace.id, worktreeId);
      setCandidates(response.candidates);
      // Pre-tick the lifecycle commands people almost always want; leave `custom` alone.
      setSelected(
        new Set(
          response.candidates
            .filter((candidate) => candidate.role !== "custom")
            .map(candidateKey),
        ),
      );
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError
          ? caughtError.message
          : "Unable to inspect the repository.",
      );
    } finally {
      setIsDetecting(false);
    }
  }

  async function importSelected() {
    if (!candidates) {
      return;
    }
    const chosen = candidates.filter((candidate) => selected.has(candidateKey(candidate)));
    if (chosen.length === 0) {
      return;
    }
    setIsImporting(true);
    setError(null);
    try {
      const created = await importRunCommands(
        workspace.id,
        chosen.map((candidate) => ({
          name: candidate.name,
          command: candidate.command,
          role: candidate.role,
          cwd: candidate.cwd,
          port: candidate.port,
        })),
      );
      setCandidates(null);
      setSelected(new Set());
      onCommandsImported(created);
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to import commands.",
      );
    } finally {
      setIsImporting(false);
    }
  }

  async function applyPreset(slug: string) {
    setIsImporting(true);
    setError(null);
    try {
      onCommandsImported(await applyRunPreset(workspace.id, slug));
    } catch (caughtError) {
      setError(
        caughtError instanceof ApiError ? caughtError.message : "Unable to apply the preset.",
      );
    } finally {
      setIsImporting(false);
    }
  }

  function toggle(candidate: DetectedCommand) {
    const key = candidateKey(candidate);
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  return (
    <div className="grid gap-4">
      {error && (
        <div className="rounded-md border border-danger/30 bg-[#fff5f2] p-3 text-sm text-danger">
          {error}
        </div>
      )}

      <section className="rounded-md border border-line bg-white p-4">
        <div className="flex items-center gap-2">
          <FolderTree className="h-4 w-4 text-ink/55" aria-hidden="true" />
          <h4 className="font-semibold">App root</h4>
        </div>
        <p className="mt-1 text-sm text-ink/60">
          The directory inside the repository this workspace works in. Commands, the dev server
          and the terminal all start here. Leave empty for the repository root.
        </p>
        <form className="mt-3 flex flex-wrap items-center gap-2" onSubmit={submitAppRoot}>
          <input
            aria-label="App root"
            className="min-w-0 flex-1 rounded-md border border-line bg-white px-3 py-2 font-mono text-sm outline-none transition focus:border-ink"
            placeholder="apps/api"
            value={appRootDraft}
            disabled={!canManage}
            onChange={(event) => {
              setAppRootDraft(event.target.value);
              setAppRootSaved(false);
            }}
          />
          <button
            className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="submit"
            disabled={!canManage || isSavingAppRoot || appRootDraft === appRoot}
          >
            {isSavingAppRoot ? "Saving..." : "Save"}
          </button>
          {appRootSaved && <span className="text-sm text-signal">Saved</span>}
        </form>
      </section>

      <section className="rounded-md border border-line bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <Search className="h-4 w-4 text-ink/55" aria-hidden="true" />
              <h4 className="font-semibold">Detect commands</h4>
            </div>
            <p className="mt-1 text-sm text-ink/60">
              Reads your <code className="font-mono text-xs">package.json</code>,{" "}
              <code className="font-mono text-xs">pyproject.toml</code>,{" "}
              <code className="font-mono text-xs">Makefile</code> and friends. Nothing in your
              repository is executed, and nothing is saved until you choose.
            </p>
          </div>
          <button
            className="flex items-center gap-2 rounded-md border border-line bg-white px-3 py-2 text-sm font-semibold transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
            type="button"
            disabled={!canManage || isDetecting}
            onClick={() => void detect()}
          >
            {isDetecting && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
            {isDetecting ? "Inspecting..." : "Detect commands"}
          </button>
        </div>

        {candidates !== null && candidates.length === 0 && (
          <p className="mt-3 rounded-md border border-dashed border-line p-3 text-sm text-ink/60">
            Nothing recognisable found. Add a command by hand, or start from a preset below.
          </p>
        )}

        {candidates !== null && candidates.length > 0 && (
          <div className="mt-3">
            <ul className="max-h-80 space-y-2 overflow-auto">
              {candidates.map((candidate) => {
                const key = candidateKey(candidate);
                return (
                  <li key={key}>
                    <label className="flex cursor-pointer items-start gap-3 rounded-md border border-line p-3 transition hover:border-ink/45">
                      <input
                        checked={selected.has(key)}
                        className="mt-1"
                        type="checkbox"
                        onChange={() => toggle(candidate)}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex flex-wrap items-center gap-2">
                          <span className="font-semibold">{candidate.name}</span>
                          <span className="rounded-full bg-line px-2 py-0.5 text-xs font-semibold capitalize">
                            {candidate.role}
                          </span>
                          {candidate.cwd && (
                            <span className="font-mono text-xs text-ink/60">{candidate.cwd}</span>
                          )}
                          {candidate.port && (
                            <span className="text-xs text-ink/60">port {candidate.port}</span>
                          )}
                        </span>
                        <code className="mt-1 block break-words rounded bg-panel px-2 py-1 font-mono text-xs text-ink/75">
                          {candidate.command}
                        </code>
                        <span className="mt-1 block text-xs text-ink/50">{candidate.reason}</span>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
            <div className="mt-3 flex items-center justify-end gap-2">
              <button
                className="rounded-md border border-line bg-white px-4 py-2 text-sm font-semibold transition hover:bg-panel"
                type="button"
                onClick={() => setCandidates(null)}
              >
                Cancel
              </button>
              <button
                className="rounded-md bg-primary px-4 py-2 text-sm font-semibold text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
                type="button"
                disabled={isImporting || selected.size === 0}
                onClick={() => void importSelected()}
              >
                {isImporting ? "Adding..." : `Add ${selected.size} command(s)`}
              </button>
            </div>
          </div>
        )}
      </section>

      {presets.length > 0 && (
        <section className="rounded-md border border-line bg-white p-4">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-ink/55" aria-hidden="true" />
            <h4 className="font-semibold">Start from a preset</h4>
          </div>
          <p className="mt-1 text-sm text-ink/60">
            For a repository that has nothing to detect yet. Every command a preset adds is
            editable afterwards.
          </p>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {presets.map((preset) => (
              <div
                key={preset.slug}
                className="flex items-start justify-between gap-3 rounded-md border border-line p-3"
              >
                <div className="min-w-0">
                  <p className="font-semibold">{preset.name}</p>
                  <p className="mt-1 text-xs text-ink/60">{preset.description}</p>
                  <p className="mt-1 text-xs text-ink/45">
                    {preset.commands.length} commands
                  </p>
                </div>
                <button
                  className="shrink-0 rounded-md border border-line bg-white px-3 py-1.5 text-sm font-semibold transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
                  type="button"
                  disabled={!canManage || isImporting}
                  onClick={() => void applyPreset(preset.slug)}
                >
                  Use
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
