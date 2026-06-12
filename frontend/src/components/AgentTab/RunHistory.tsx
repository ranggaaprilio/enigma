import { useCallback, useEffect, useState } from "react";
import { Clock, Cpu, Loader2 } from "lucide-react";
import type { AgentRunResponse, AgentRunStatus } from "../../lib/api";
import { listAgentRuns } from "../../lib/api";

interface RunHistoryProps {
  workspaceId: string;
  onSelectRun: (run: AgentRunResponse) => void;
  selectedRunId?: string | null;
}

const PAGE_SIZE = 20;

function formatElapsed(startedAt: string, completedAt: string | null): string {
  const start = new Date(startedAt).getTime();
  const end = completedAt ? new Date(completedAt).getTime() : Date.now();
  const diffMs = end - start;
  if (diffMs < 1000) return `${diffMs}ms`;
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds}s`;
}

function formatTokenCount(tokens: number | null): string {
  if (tokens === null) return "—";
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}k`;
  return String(tokens);
}

const statusConfig: Record<AgentRunStatus, { label: string; className: string }> = {
  thinking: { label: "Thinking", className: "bg-timeline-thinking/20 text-timeline-thinking" },
  executing: { label: "Executing", className: "bg-timeline-read/20 text-timeline-read" },
  completed: { label: "Completed", className: "bg-signal/15 text-signal" },
  error: { label: "Error", className: "bg-danger/15 text-danger" },
  stopped: { label: "Stopped", className: "bg-muted-soft/30 text-muted" },
};

function StatusBadge({ status }: { status: AgentRunStatus }) {
  const config = statusConfig[status];
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.06em] ${config.className}`}
    >
      {status === "thinking" || status === "executing" ? (
        <Loader2 className="mr-1 h-3 w-3 animate-spin" />
      ) : null}
      {config.label}
    </span>
  );
}

export function RunHistory({ workspaceId, onSelectRun, selectedRunId }: RunHistoryProps) {
  const [runs, setRuns] = useState<AgentRunResponse[]>([]);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    listAgentRuns(workspaceId, undefined, PAGE_SIZE, 0)
      .then((data) => {
        if (cancelled) return;
        setRuns(data);
        setOffset(data.length);
        setHasMore(data.length === PAGE_SIZE);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load runs");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const loadMore = useCallback(() => {
    if (loading || !hasMore) return;
    setLoading(true);
    setError(null);

    listAgentRuns(workspaceId, undefined, PAGE_SIZE, offset)
      .then((data) => {
        setRuns((prev) => [...prev, ...data]);
        setOffset((prev) => prev + data.length);
        setHasMore(data.length === PAGE_SIZE);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load more runs");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [workspaceId, offset, loading, hasMore]);

  if (error && runs.length === 0) {
    return (
      <div className="rounded-md border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-danger">
        {error}
      </div>
    );
  }

  if (!loading && runs.length === 0) {
    return (
      <div className="m-3 rounded-md border border-dashed border-line bg-white px-4 py-8 text-center text-sm text-ink/55">
        No agent runs yet. Start a run to see it here.
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <ul role="list" className="divide-y divide-line">
        {runs.map((run) => {
          const isSelected = run.id === selectedRunId;
          return (
            <li key={run.id}>
              <button
                type="button"
                onClick={() => onSelectRun(run)}
                className={`flex w-full items-start gap-3 border-l-2 px-4 py-3 text-left transition-colors hover:bg-white ${
                  isSelected
                    ? "border-primary bg-white"
                    : "border-transparent"
                }`}
                aria-current={isSelected ? "true" : undefined}
              >
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">
                    {run.prompt || "(no prompt)"}
                  </p>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                    <StatusBadge status={run.status} />
                    <span className="font-mono text-ink/70">{run.model}</span>
                    <span className="inline-flex items-center gap-0.5">
                      <Clock className="h-3 w-3" />
                      {formatElapsed(run.started_at, run.completed_at)}
                    </span>
                    <span className="inline-flex items-center gap-0.5">
                      <Cpu className="h-3 w-3" />
                      {formatTokenCount(run.tokens_used)}
                    </span>
                  </div>
                </div>
              </button>
            </li>
          );
        })}
      </ul>

      {error && (
        <p className="px-3 py-2 text-xs text-danger">{error}</p>
      )}

      {hasMore && (
        <div className="border-t border-line px-3 py-3">
          <button
            type="button"
            onClick={loadMore}
            disabled={loading}
            className="text-sm font-medium text-primary transition-colors hover:text-primary-active disabled:text-muted-soft"
          >
            {loading ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </div>
  );
}
