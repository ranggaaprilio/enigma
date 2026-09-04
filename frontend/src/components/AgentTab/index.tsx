import { useCallback, useEffect, useState } from "react";
import { Bot, MessageSquarePlus } from "lucide-react";

import {
  createAgentSession,
  deleteAgentSession,
  listAgentSessions,
  updateAgentSession,
  type AgentSessionResponse,
} from "../../lib/api";
import { useActiveWorktree } from "../../lib/activeWorktree";
import { SessionList } from "./SessionList";
import { SessionThread } from "./SessionThread";

interface AgentTabProps {
  workspaceId: string | null;
}

const PAGE_SIZE = 20;

export function AgentTab({ workspaceId }: AgentTabProps) {
  const [activeWorktree] = useActiveWorktree(workspaceId ?? "");
  const [sessions, setSessions] = useState<AgentSessionResponse[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (!workspaceId) {
      setSessions([]);
      setSelectedSessionId(null);
      setOffset(0);
      setHasMore(false);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    listAgentSessions(workspaceId, undefined, PAGE_SIZE, 0)
      .then((data) => {
        if (cancelled) return;
        setSessions(data);
        setOffset(data.length);
        setHasMore(data.length === PAGE_SIZE);
        setSelectedSessionId((current) => current ?? data[0]?.id ?? null);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load sessions");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const loadMore = useCallback(() => {
    if (!workspaceId || loading || !hasMore) return;
    setLoading(true);
    setError(null);
    listAgentSessions(workspaceId, undefined, PAGE_SIZE, offset)
      .then((data) => {
        // `offset` is kept in step with create/delete below, but belt-and-braces: drop any
        // row already on screen so a shifted window can never produce duplicate React keys.
        setSessions((prev) => {
          const seen = new Set(prev.map((s) => s.id));
          return [...prev, ...data.filter((s) => !seen.has(s.id))];
        });
        setOffset((prev) => prev + data.length);
        setHasMore(data.length === PAGE_SIZE);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load more sessions");
      })
      .finally(() => {
        setLoading(false);
      });
  }, [workspaceId, offset, loading, hasMore]);

  const handleNewSession = useCallback(async () => {
    if (!workspaceId || creating) return;
    setCreating(true);
    setError(null);
    try {
      const session = await createAgentSession(workspaceId);
      setSessions((prev) => [session, ...prev]);
      // A new session is prepended server-side too, shifting every loaded row down one.
      // Without this the next page re-fetches a row already on screen (duplicate keys).
      setOffset((prev) => prev + 1);
      setSelectedSessionId(session.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create session");
    } finally {
      setCreating(false);
    }
  }, [workspaceId, creating]);

  const handleSessionUpdated = useCallback((updated: AgentSessionResponse) => {
    setSessions((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
  }, []);

  const handleRename = useCallback(
    async (sessionId: string, title: string) => {
      if (!workspaceId) return;
      try {
        const updated = await updateAgentSession(workspaceId, sessionId, { title });
        handleSessionUpdated(updated);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to rename session");
      }
    },
    [workspaceId, handleSessionUpdated],
  );

  const handleDelete = useCallback(
    async (sessionId: string) => {
      if (!workspaceId) return;
      try {
        await deleteAgentSession(workspaceId, sessionId);
        // Rows shift up one server-side; without this the next page skips a session.
        setOffset((prev) => Math.max(0, prev - 1));
        setSessions((prev) => {
          const next = prev.filter((s) => s.id !== sessionId);
          setSelectedSessionId((current) =>
            current === sessionId ? next[0]?.id ?? null : current,
          );
          return next;
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to delete session");
      }
    },
    [workspaceId],
  );

  if (!workspaceId) {
    return (
      <section className="flex flex-col items-center justify-center gap-4 rounded-md border border-dashed border-line bg-white px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-panel">
          <Bot className="h-6 w-6 text-ink/55" aria-hidden="true" />
        </div>
        <div>
          <h3 className="text-lg font-semibold text-ink">No workspace selected</h3>
          <p className="mt-1 text-sm text-ink/60">Select a workspace to manage agents</p>
        </div>
      </section>
    );
  }

  const selected = sessions.find((s) => s.id === selectedSessionId) ?? null;

  return (
    <section className="grid h-[calc(100vh-10.5rem)] min-h-[560px] grid-rows-[260px_minmax(0,1fr)] overflow-hidden rounded-lg border border-line bg-white xl:grid-cols-[300px_minmax(0,1fr)] xl:grid-rows-1">
      <aside className="flex min-h-0 flex-col border-b border-line bg-panel xl:border-b-0 xl:border-r">
        <div className="shrink-0 border-b border-line px-4 py-4">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">Agent</p>
          <h3 className="mt-1 text-lg font-normal tracking-[-0.01em]">Sessions</h3>
        </div>
        <div className="min-h-0 flex-1">
          <SessionList
            sessions={sessions}
            selectedSessionId={selectedSessionId}
            loading={loading}
            error={error}
            hasMore={hasMore}
            creating={creating}
            onSelect={setSelectedSessionId}
            onNewSession={handleNewSession}
            onLoadMore={loadMore}
            onRename={handleRename}
            onDelete={handleDelete}
          />
        </div>
      </aside>

      {selected ? (
        <SessionThread
          key={selected.id}
          workspaceId={workspaceId}
          session={selected}
          worktreeId={activeWorktree?.id ?? null}
          worktreeBranch={activeWorktree?.branch ?? null}
          onSessionUpdated={handleSessionUpdated}
        />
      ) : (
        <div className="flex flex-col items-center justify-center gap-4 px-6 py-16 text-center">
          <div className="flex h-11 w-11 items-center justify-center rounded-full border border-line bg-white text-primary">
            <MessageSquarePlus className="h-5 w-5" aria-hidden="true" />
          </div>
          <div>
            <h3 className="text-lg font-normal text-ink">Start a session</h3>
            <p className="mt-1 max-w-sm text-sm text-muted">
              Create a session to have a multi-turn conversation with the agent. Follow-up
              messages keep prior context.
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
