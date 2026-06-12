import { useState, useCallback } from "react";
import { ArrowUp, Square } from "lucide-react";
import {
  createAgentRun,
  stopAgentRun,
  type AgentRunResponse,
} from "../../lib/api";

interface PromptComposerProps {
  workspaceId: string;
  activeRunId?: string | null;
  isRunning?: boolean;
  onRunCreated?: (run: AgentRunResponse) => void;
  onRunStopped?: (run: AgentRunResponse) => void;
}

export function PromptComposer({
  workspaceId,
  activeRunId: externalActiveRunId,
  isRunning: externalIsRunning,
  onRunCreated,
  onRunStopped,
}: PromptComposerProps) {
  const [prompt, setPrompt] = useState("");
  const [internalIsRunning, setInternalIsRunning] = useState(false);
  const [internalActiveRunId, setInternalActiveRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isRunning = externalIsRunning ?? internalIsRunning;
  const activeRunId = externalActiveRunId ?? internalActiveRunId;
  const isControlled = externalIsRunning !== undefined;

  const handleSubmit = useCallback(async () => {
    const trimmed = prompt.trim();
    if (!trimmed || isRunning) return;

    setError(null);
    if (!isControlled) setInternalIsRunning(true);

    try {
      const run = await createAgentRun(workspaceId, { prompt: trimmed });
      if (!isControlled) setInternalActiveRunId(run.id);
      setPrompt("");
      onRunCreated?.(run);
    } catch (err) {
      if (!isControlled) setInternalIsRunning(false);
      setError(err instanceof Error ? err.message : "Failed to start run");
    }
  }, [prompt, workspaceId, isRunning, isControlled, onRunCreated]);

  const handleStop = useCallback(async () => {
    if (!activeRunId) return;

    try {
      const run = await stopAgentRun(workspaceId, activeRunId);
      onRunStopped?.(run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop run");
    } finally {
      if (!isControlled) {
        setInternalIsRunning(false);
        setInternalActiveRunId(null);
      }
    }
  }, [activeRunId, workspaceId, isControlled, onRunStopped]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  return (
    <div className="rounded-lg border border-line-strong bg-white p-2 transition focus-within:border-ink">
      <textarea
        className="min-h-16 w-full resize-none bg-transparent px-2 py-1.5 text-sm leading-6 text-ink outline-none placeholder:text-muted-soft disabled:cursor-not-allowed disabled:opacity-60"
        rows={2}
        placeholder="Describe what you want the agent to do…"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={isRunning}
      />

      {error && (
        <p className="px-2 pb-1 text-xs text-danger">{error}</p>
      )}

      <div className="flex items-center justify-between gap-3 px-1">
        <span className="text-xs text-muted">
          {isRunning ? "Agent is running" : "Enter to send · Shift+Enter for a new line"}
        </span>
        {isRunning ? (
          <button
            type="button"
            onClick={handleStop}
            className="inline-flex h-8 items-center gap-1.5 rounded-md border border-line bg-white px-3 text-sm font-medium text-ink transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-45"
          >
            <Square className="h-3.5 w-3.5" aria-hidden="true" />
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!prompt.trim()}
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-sm font-medium text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
          >
            <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
            Run
          </button>
        )}
      </div>
    </div>
  );
}
