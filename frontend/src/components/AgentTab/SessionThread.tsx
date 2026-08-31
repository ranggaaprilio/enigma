import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Check, CircleDot, Pencil, RefreshCw, Sparkles, WifiOff, X } from "lucide-react";

import {
  ApiError,
  createAgentSessionTurn,
  getAgentRun,
  getAgentSession,
  stopAgentRun,
  updateAgentSession,
  type AgentEventResponse,
  type AgentRunResponse,
  type AgentSessionDetailResponse,
  type AgentSessionResponse,
} from "../../lib/api";
import { useAgentRunStream } from "../../lib/useAgentRunStream";
import { usePermissions } from "../../auth/permissions";
import { Notice } from "./Notice";
import { PromptComposer } from "./PromptComposer";
import { TurnView } from "./TurnView";

interface SessionThreadProps {
  workspaceId: string;
  session: AgentSessionResponse;
  worktreeId: string | null;
  worktreeBranch: string | null;
  onSessionUpdated: (session: AgentSessionResponse) => void;
}

function isActiveStatus(status: AgentRunResponse["status"]): boolean {
  return status === "thinking" || status === "executing";
}

function toSummary(detail: AgentSessionDetailResponse): AgentSessionResponse {
  return {
    id: detail.id,
    workspace_id: detail.workspace_id,
    user_id: detail.user_id,
    title: detail.title,
    status: detail.status,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
  };
}

export function SessionThread({
  workspaceId,
  session,
  worktreeId,
  worktreeBranch,
  onSessionUpdated,
}: SessionThreadProps) {
  const [detail, setDetail] = useState<AgentSessionDetailResponse | null>(null);
  const [turnEvents, setTurnEvents] = useState<Record<string, AgentEventResponse[]>>({});
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [continueError, setContinueError] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");

  const activityRef = useRef<HTMLDivElement | null>(null);
  // Keep onSessionUpdated out of loadDetail's dependency list to avoid re-running the
  // mount effect when the parent re-renders.
  const onSessionUpdatedRef = useRef(onSessionUpdated);
  useEffect(() => {
    onSessionUpdatedRef.current = onSessionUpdated;
  }, [onSessionUpdated]);

  const loadDetail = useCallback(async () => {
    const data = await getAgentSession(workspaceId, session.id);
    setDetail(data);
    onSessionUpdatedRef.current(toSummary(data));

    const active = [...data.turns].reverse().find((turn) => isActiveStatus(turn.status));
    setActiveTurnId(active ? active.id : null);

    const terminalTurns = data.turns.filter((turn) => !isActiveStatus(turn.status));
    const entries = await Promise.all(
      terminalTurns.map(async (turn) => {
        try {
          const run = await getAgentRun(workspaceId, turn.id);
          return [turn.id, run.events] as const;
        } catch {
          return [turn.id, [] as AgentEventResponse[]] as const;
        }
      }),
    );
    setTurnEvents(Object.fromEntries(entries));
  }, [workspaceId, session.id]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    setContinueError(null);
    loadDetail()
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Failed to load session");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [loadDetail]);

  const handleTerminal = useCallback(() => {
    // The turn finished; reload persisted turns/events and refresh the session summary.
    void loadDetail();
  }, [loadDetail]);

  const {
    events: streamEvents,
    connectionState,
    agentError,
  } = useAgentRunStream(workspaceId, activeTurnId, {
    enabled: activeTurnId !== null,
    onTerminal: handleTerminal,
  });

  const { can } = usePermissions();
  const canRunAgent = can("agent.run");
  const turns = detail?.turns ?? [];
  const isRunning = activeTurnId !== null;

  // Auto-scroll to the newest activity.
  useEffect(() => {
    const node = activityRef.current;
    if (!node) return;
    node.scrollTop = node.scrollHeight;
  }, [turns.length, streamEvents.length]);

  const submitTurn = useCallback(
    async (prompt: string): Promise<AgentRunResponse> => {
      setContinueError(null);
      try {
        const run = await createAgentSessionTurn(workspaceId, session.id, {
          prompt,
          worktree_id: worktreeId ?? null,
        });
        setDetail((prev) => (prev ? { ...prev, turns: [...prev.turns, run] } : prev));
        setActiveTurnId(run.id);
        return run;
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          setContinueError(
            "The agent is already working on another run. Wait for it to finish before continuing.",
          );
          // Surface whatever run is actually active.
          void loadDetail();
        }
        throw err;
      }
    },
    [workspaceId, session.id, worktreeId, loadDetail],
  );

  const handleStop = useCallback(async () => {
    if (!activeTurnId) return;
    try {
      await stopAgentRun(workspaceId, activeTurnId);
    } catch {
      // The terminal stream frame (or a later reload) will reconcile state.
    }
  }, [workspaceId, activeTurnId]);

  const commitTitle = useCallback(async () => {
    const trimmed = draftTitle.trim();
    setEditingTitle(false);
    if (!trimmed || trimmed === (session.title ?? "")) return;
    try {
      const updated = await updateAgentSession(workspaceId, session.id, { title: trimmed });
      onSessionUpdated(updated);
      setDetail((prev) => (prev ? { ...prev, title: updated.title } : prev));
    } catch {
      // Ignore rename failures; the title simply stays as it was.
    }
  }, [draftTitle, session.title, session.id, workspaceId, onSessionUpdated]);

  const title = session.title || "Untitled session";

  return (
    <div className="flex min-h-0 min-w-0 flex-col">
      <header className="flex min-h-16 shrink-0 items-center justify-between gap-4 border-b border-line px-5 py-3">
        <div className="min-w-0 flex-1">
          {editingTitle ? (
            <div className="flex items-center gap-1">
              <input
                autoFocus
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void commitTitle();
                  if (e.key === "Escape") setEditingTitle(false);
                }}
                className="min-w-0 flex-1 rounded border border-line-strong bg-white px-2 py-1 text-sm text-ink outline-none focus:border-ink"
                aria-label="Session title"
              />
              <button
                type="button"
                onClick={() => void commitTitle()}
                className="rounded p-1 text-signal transition hover:bg-panel"
                aria-label="Save title"
              >
                <Check className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => setEditingTitle(false)}
                className="rounded p-1 text-muted transition hover:bg-panel"
                aria-label="Cancel rename"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => {
                setDraftTitle(session.title ?? "");
                setEditingTitle(true);
              }}
              className="group flex items-center gap-1.5 text-left"
              aria-label="Rename session"
            >
              <h3 className="truncate text-base font-semibold text-ink">{title}</h3>
              <Pencil className="h-3.5 w-3.5 shrink-0 text-muted opacity-0 transition group-hover:opacity-100" />
            </button>
          )}
          <p className="mt-0.5 text-xs text-muted">Workspace agent conversation</p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 py-1 text-xs font-medium capitalize text-body">
          <CircleDot
            className={`h-3 w-3 ${isRunning ? "animate-pulse text-primary" : "text-muted"}`}
            aria-hidden="true"
          />
          {isRunning ? "Running" : session.status}
        </span>
      </header>

      <div
        ref={activityRef}
        className="min-h-0 flex-1 overflow-y-auto bg-canvas px-4 py-6 sm:px-6"
        data-testid="agent-process"
      >
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-5">
          {connectionState === "reconnecting" && (
            <Notice tone="warn" testId="reconnecting-status">
              <RefreshCw className="h-4 w-4 animate-spin" />
              Reconnecting…
            </Notice>
          )}
          {connectionState === "disconnected" && (
            <Notice tone="danger" testId="disconnected-status">
              <WifiOff className="h-4 w-4" />
              Live stream disconnected. The agent may still be running — reload to reconnect.
            </Notice>
          )}
          {loadError && (
            <Notice tone="danger">
              <AlertCircle className="h-4 w-4" />
              {loadError}
            </Notice>
          )}

          {!loading && turns.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 py-20 text-center">
              <div className="flex h-11 w-11 items-center justify-center rounded-full border border-line bg-white text-primary">
                <Sparkles className="h-5 w-5" aria-hidden="true" />
              </div>
              <h4 className="mt-4 text-lg font-normal text-ink">
                What should the agent work on?
              </h4>
              <p className="mt-1 max-w-sm text-sm leading-6 text-muted">
                Ask it to inspect the repository, implement a change, or run checks. Follow-up
                messages keep the conversation's context.
              </p>
            </div>
          ) : (
            turns.map((turn) => (
              <TurnView
                key={turn.id}
                turn={turn}
                workspaceId={workspaceId}
                isActive={turn.id === activeTurnId}
                events={turn.id === activeTurnId ? streamEvents : turnEvents[turn.id] ?? []}
                agentError={turn.id === activeTurnId ? agentError : null}
              />
            ))
          )}
        </div>
      </div>

      <div
        className="shrink-0 border-t border-line bg-panel px-4 py-3 sm:px-6"
        data-testid="agent-composer"
      >
        <div className="mx-auto w-full max-w-3xl">
          <p className="mb-1.5 text-xs text-muted">
            Target:{" "}
            <span className="font-medium text-body">
              {worktreeBranch ? worktreeBranch : "base branch"}
            </span>
          </p>
          <PromptComposer
            submitPrompt={submitTurn}
            onStop={handleStop}
            isRunning={isRunning}
            disabled={!canRunAgent}
            externalError={continueError}
            placeholder={
              !canRunAgent
                ? "You have read-only access to this workspace."
                : turns.length === 0
                  ? "Describe what you want the agent to do…"
                  : "Send a follow-up message…"
            }
          />
        </div>
      </div>
    </div>
  );
}
