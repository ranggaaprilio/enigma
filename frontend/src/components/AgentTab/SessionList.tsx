import { useState } from "react";
import { Check, Pencil, Plus, Trash2, X } from "lucide-react";

import type { AgentSessionResponse } from "../../lib/api";
import { ConfirmModal } from "../ui/ConfirmModal";

interface SessionListProps {
  sessions: AgentSessionResponse[];
  selectedSessionId: string | null;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  creating?: boolean;
  onSelect: (sessionId: string) => void;
  onNewSession: () => void;
  onLoadMore: () => void;
  onRename: (sessionId: string, title: string) => void;
  onDelete: (sessionId: string) => void;
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const diffMs = Date.now() - then;
  if (Number.isNaN(diffMs)) return "";
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function SessionList({
  sessions,
  selectedSessionId,
  loading,
  error,
  hasMore,
  creating = false,
  onSelect,
  onNewSession,
  onLoadMore,
  onRename,
  onDelete,
}: SessionListProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [pendingDelete, setPendingDelete] = useState<AgentSessionResponse | null>(null);

  const startEditing = (session: AgentSessionResponse) => {
    setEditingId(session.id);
    setDraftTitle(session.title ?? "");
  };

  const commitEditing = () => {
    if (editingId) {
      const trimmed = draftTitle.trim();
      if (trimmed) onRename(editingId, trimmed);
    }
    setEditingId(null);
    setDraftTitle("");
  };

  return (
    <div className="flex min-h-0 flex-col">
      <div className="shrink-0 border-b border-line px-4 py-3">
        <button
          type="button"
          onClick={onNewSession}
          disabled={creating}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-white transition hover:bg-primary-active disabled:cursor-not-allowed disabled:bg-primary/45"
          data-testid="new-session"
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          New session
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && sessions.length === 0 ? (
          <div className="m-3 rounded-md border border-danger/30 bg-danger/5 px-4 py-3 text-sm text-danger">
            {error}
          </div>
        ) : !loading && sessions.length === 0 ? (
          <div className="m-3 rounded-md border border-dashed border-line bg-white px-4 py-8 text-center text-sm text-ink/55">
            No sessions yet. Start a new session to chat with the agent.
          </div>
        ) : (
          <ul role="list" className="divide-y divide-line">
            {sessions.map((session) => {
              const isSelected = session.id === selectedSessionId;
              const isEditing = session.id === editingId;
              return (
                <li key={session.id} className="group relative">
                  {isEditing ? (
                    <div className="flex items-center gap-1 px-3 py-2">
                      <input
                        autoFocus
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") commitEditing();
                          if (e.key === "Escape") {
                            setEditingId(null);
                            setDraftTitle("");
                          }
                        }}
                        className="min-w-0 flex-1 rounded border border-line-strong bg-white px-2 py-1 text-sm text-ink outline-none focus:border-ink"
                        aria-label="Session title"
                      />
                      <button
                        type="button"
                        onClick={commitEditing}
                        className="rounded p-1 text-signal transition hover:bg-panel"
                        aria-label="Save title"
                      >
                        <Check className="h-4 w-4" />
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setEditingId(null);
                          setDraftTitle("");
                        }}
                        className="rounded p-1 text-muted transition hover:bg-panel"
                        aria-label="Cancel rename"
                      >
                        <X className="h-4 w-4" />
                      </button>
                    </div>
                  ) : (
                    <div
                      className={`flex items-start gap-2 border-l-2 pl-4 pr-2 transition-colors ${
                        isSelected ? "border-primary bg-white" : "border-transparent hover:bg-white"
                      }`}
                    >
                      <button
                        type="button"
                        onClick={() => onSelect(session.id)}
                        className="min-w-0 flex-1 py-3 text-left"
                        aria-current={isSelected ? "true" : undefined}
                      >
                        <p className="truncate text-sm font-medium text-ink">
                          {session.title || "Untitled session"}
                        </p>
                        <p className="mt-0.5 text-xs capitalize text-muted">
                          {session.status} · {formatRelative(session.updated_at)}
                        </p>
                      </button>
                      <div className="flex shrink-0 items-center gap-0.5 py-3 opacity-0 transition group-hover:opacity-100 focus-within:opacity-100">
                        <button
                          type="button"
                          onClick={() => startEditing(session)}
                          className="rounded p-1 text-muted transition hover:bg-panel hover:text-ink"
                          aria-label="Rename session"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setPendingDelete(session)}
                          className="rounded p-1 text-muted transition hover:bg-panel hover:text-danger"
                          aria-label="Delete session"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {error && sessions.length > 0 && (
          <p className="px-3 py-2 text-xs text-danger">{error}</p>
        )}

        {hasMore && (
          <div className="border-t border-line px-3 py-3">
            <button
              type="button"
              onClick={onLoadMore}
              disabled={loading}
              className="text-sm font-medium text-primary transition-colors hover:text-primary-active disabled:text-muted-soft"
            >
              {loading ? "Loading…" : "Load more"}
            </button>
          </div>
        )}
      </div>

      {pendingDelete && (
        <ConfirmModal
          title="Delete session"
          body={`Delete “${pendingDelete.title || "Untitled session"}”? Its turns are kept in run history but removed from this conversation.`}
          confirmLabel="Delete"
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            onDelete(pendingDelete.id);
            setPendingDelete(null);
          }}
        />
      )}
    </div>
  );
}
