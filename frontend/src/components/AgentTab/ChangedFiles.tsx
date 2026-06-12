import { useEffect, useState } from "react";
import type { AgentFileChangeListResponse, AgentFileChangeResponse } from "../../lib/api";
import { getAgentFileChanges } from "../../lib/api";

const MAX_DISPLAY = 50;

const STATUS_STYLES: Record<AgentFileChangeResponse["status"], string> = {
  created: "bg-signal/15 text-signal",
  modified: "bg-warn/15 text-warn",
  deleted: "bg-danger/15 text-danger",
  renamed: "bg-blue-500/15 text-blue-600",
};

const STATUS_LABELS: Record<AgentFileChangeResponse["status"], string> = {
  created: "created",
  modified: "modified",
  deleted: "deleted",
  renamed: "renamed",
};

function StatusBadge({ status }: { status: AgentFileChangeResponse["status"] }) {
  return (
    <span
      className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${STATUS_STYLES[status]}`}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}

function SkeletonRow() {
  return (
    <li className="flex items-center gap-3 border-b border-line px-3 py-2.5">
      <div className="h-4 w-14 animate-pulse rounded bg-line" />
      <div className="h-4 flex-1 animate-pulse rounded bg-line" />
    </li>
  );
}

interface ChangedFilesProps {
  workspaceId: string;
  runId: string;
}

export function ChangedFiles({ workspaceId, runId }: ChangedFilesProps) {
  const [data, setData] = useState<AgentFileChangeListResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setData(null);

    getAgentFileChanges(workspaceId, runId, controller.signal)
      .then((result) => {
        setData(result);
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setData({ items: [], total: 0, capped: false });
      })
      .finally(() => {
        setLoading(false);
      });

    return () => {
      controller.abort();
    };
  }, [workspaceId, runId]);

  if (loading) {
    return (
      <div className="rounded-md border border-line bg-white">
        <ul>
          {Array.from({ length: 5 }, (_, i) => (
            <SkeletonRow key={i} />
          ))}
        </ul>
      </div>
    );
  }

  if (!data || data.items.length === 0) {
    return (
      <div className="rounded-md border border-line bg-white px-4 py-8 text-center text-sm text-ink/55">
        No file changes
      </div>
    );
  }

  const displayed = data.items.slice(0, MAX_DISPLAY);
  const remaining = data.total - displayed.length;

  return (
    <div className="rounded-md border border-line bg-white">
      <ul>
        {displayed.map((file) => (
          <li
            key={file.id}
            className="flex items-center gap-3 border-b border-line px-3 py-2 last:border-b-0"
          >
            <StatusBadge status={file.status} />
            <span className="min-w-0 truncate font-mono text-sm text-ink">
              {file.path}
            </span>
          </li>
        ))}
      </ul>
      {remaining > 0 && (
        <p className="border-t border-line px-3 py-2 text-center text-xs text-ink/55">
          {remaining} more file{remaining !== 1 ? "s" : ""}
        </p>
      )}
    </div>
  );
}