import { useState, useCallback } from "react";
import { ArrowUp, Square } from "lucide-react";
import type { AgentRunResponse } from "../../lib/api";

interface PromptComposerProps {
  /** Submit the typed prompt; resolves with the created run. Throwing surfaces an error. */
  submitPrompt: (prompt: string) => Promise<AgentRunResponse>;
  onStop?: () => Promise<void> | void;
  isRunning?: boolean;
  disabled?: boolean;
  /** An error owned by the parent (e.g. a 409) shown inline; keeps the prompt intact. */
  externalError?: string | null;
  placeholder?: string;
  onRunCreated?: (run: AgentRunResponse) => void;
}

export function PromptComposer({
  submitPrompt,
  onStop,
  isRunning = false,
  disabled = false,
  externalError,
  placeholder = "Describe what you want the agent to do…",
  onRunCreated,
}: PromptComposerProps) {
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = useCallback(async () => {
    const trimmed = prompt.trim();
    if (!trimmed || isRunning || submitting) return;

    setError(null);
    setSubmitting(true);
    try {
      const run = await submitPrompt(trimmed);
      setPrompt(""); // clear only on success
      onRunCreated?.(run);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start run");
    } finally {
      setSubmitting(false);
    }
  }, [prompt, isRunning, submitting, submitPrompt, onRunCreated]);

  const handleStop = useCallback(async () => {
    try {
      await onStop?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to stop run");
    }
  }, [onStop]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit],
  );

  const inputDisabled = isRunning || disabled;
  const shownError = error ?? externalError ?? null;

  return (
    <div className="rounded-lg border border-line-strong bg-white p-2 transition focus-within:border-ink">
      <textarea
        className="min-h-16 w-full resize-none bg-transparent px-2 py-1.5 text-sm leading-6 text-ink outline-none placeholder:text-muted-soft disabled:cursor-not-allowed disabled:opacity-60"
        rows={2}
        placeholder={placeholder}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={handleKeyDown}
        disabled={inputDisabled}
      />

      {shownError && <p className="px-2 pb-1 text-xs text-danger">{shownError}</p>}

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
            disabled={!prompt.trim() || disabled || submitting}
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
