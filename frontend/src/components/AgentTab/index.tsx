import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertCircle,
  Bot,
  CircleDot,
  RefreshCw,
  Sparkles,
  WifiOff,
} from "lucide-react";
import {
  getAgentRun,
  getAgentStreamUrl,
  type AgentEventResponse,
  type AgentRunResponse,
  type AgentRunStatus,
} from "../../lib/api";
import { useActiveWorktree } from "../../lib/activeWorktree";
import { ToolCard, pairToolEvents } from "./ToolCard";
import { PromptComposer } from "./PromptComposer";
import { RunHistory } from "./RunHistory";

interface AgentTabProps {
  workspaceId: string | null;
}

type ConnectionStatus = "connected" | "reconnecting" | "disconnected" | null;

const RECONNECT_DELAYS = [1000, 2000, 4000];
const MAX_RECONNECT_RETRIES = 3;

export function AgentTab({ workspaceId }: AgentTabProps) {
  const [activeWorktree] = useActiveWorktree(workspaceId ?? "");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEventResponse[]>([]);
  const [runStatus, setRunStatus] = useState<AgentRunStatus | null>(null);
  const [connectionStatus, setConnectionStatus] =
    useState<ConnectionStatus>(null);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<AgentRunResponse | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const activityRef = useRef<HTMLDivElement | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const intentionalCloseRef = useRef(false);
  const runStatusRef = useRef<AgentRunStatus | null>(null);
  const reconnectAttemptRef = useRef(0);

  useEffect(() => {
    runStatusRef.current = runStatus;
  }, [runStatus]);
  useEffect(() => {
    reconnectAttemptRef.current = reconnectAttempt;
  }, [reconnectAttempt]);

  const connectSocket = useCallback(
    (runId: string) => {
      if (!workspaceId) return;

      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }

      if (socketRef.current) {
        intentionalCloseRef.current = true;
        socketRef.current.close();
      }

      const url = getAgentStreamUrl(workspaceId, runId);
      const socket = new WebSocket(url);
      socketRef.current = socket;
      intentionalCloseRef.current = false;

      socket.onopen = () => {
        setConnectionStatus("connected");
        setReconnectAttempt(0);
        reconnectAttemptRef.current = 0;
        setStreamError(null);
      };

      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data) as AgentEventResponse;
          setEvents((prev) => [...prev, message]);
          if (message.event_type === "status" && message.content) {
            const status = message.content as AgentRunStatus;
            runStatusRef.current = status;
            setRunStatus(status);
          }
          if (message.event_type === "error") {
            setAgentError(message.content);
          }
        } catch {
          return;
        }
      };

      socket.onerror = () => {
        setStreamError("WebSocket connection error");
      };

      socket.onclose = () => {
        if (intentionalCloseRef.current || socketRef.current !== socket) {
          return;
        }

        const attempt = reconnectAttemptRef.current + 1;
        reconnectAttemptRef.current = attempt;

        if (attempt > MAX_RECONNECT_RETRIES) {
          setConnectionStatus("disconnected");
          setStreamError("Stream disconnected after maximum retry attempts");
          return;
        }

        setConnectionStatus("reconnecting");
        setReconnectAttempt(attempt);

        const delay =
          RECONNECT_DELAYS[Math.min(attempt - 1, RECONNECT_DELAYS.length - 1)];
        reconnectTimeoutRef.current = setTimeout(() => {
          const currentRunStatus = runStatusRef.current;
          if (
            currentRunStatus === "completed" ||
            currentRunStatus === "error" ||
            currentRunStatus === "stopped"
          ) {
            getAgentRun(workspaceId, runId)
              .then((detail) => {
                setEvents(detail.events);
                setRunStatus(detail.status);
                runStatusRef.current = detail.status;
                setRunDetail(detail);
                setConnectionStatus(null);
                setReconnectAttempt(0);
                reconnectAttemptRef.current = 0;
              })
              .catch(() => {
                setStreamError("Unable to refresh run status from API");
                setConnectionStatus("disconnected");
              });
          } else {
            connectSocket(runId);
          }
        }, delay);
      };
    },
    [workspaceId],
  );

  useEffect(() => {
    return () => {
      intentionalCloseRef.current = true;
      if (socketRef.current) {
        socketRef.current.close();
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setSelectedRunId(null);
      setEvents([]);
      setRunStatus(null);
      runStatusRef.current = null;
      setConnectionStatus(null);
      setStreamError(null);
      setAgentError(null);
      setReconnectAttempt(0);
      reconnectAttemptRef.current = 0;
      setRunDetail(null);
      return;
    }
  }, [workspaceId]);

  const handleRunCreated = useCallback(
    (run: AgentRunResponse) => {
      setSelectedRunId(run.id);
      setEvents([]);
      setRunStatus(run.status);
      runStatusRef.current = run.status;
      setRunDetail(run);
      setStreamError(null);
      setAgentError(null);
      setReconnectAttempt(0);
      reconnectAttemptRef.current = 0;
      connectSocket(run.id);
    },
    [connectSocket],
  );

  const handleRunStopped = useCallback((run: AgentRunResponse) => {
    setRunStatus(run.status);
    runStatusRef.current = run.status;
    setRunDetail(run);
  }, []);

  const handleSelectRun = useCallback(
    (run: AgentRunResponse) => {
      setSelectedRunId(run.id);
      setEvents([]);
      setStreamError(null);
      setAgentError(null);
      setReconnectAttempt(0);
      reconnectAttemptRef.current = 0;
      setRunStatus(run.status);
      runStatusRef.current = run.status;
      setRunDetail(run);

      if (run.status === "thinking" || run.status === "executing") {
        connectSocket(run.id);
      } else {
        getAgentRun(run.workspace_id, run.id)
          .then((detail) => {
            setEvents(detail.events);
            setRunDetail(detail);
          })
          .catch(() => {
            setStreamError("Unable to load run details");
          });
      }
    },
    [connectSocket],
  );

  const isRunActive = runStatus === "thinking" || runStatus === "executing";

  const paired = pairToolEvents(events);
  const outputEvents = events.filter(
    (event) => event.event_type === "output_text",
  );
  const outputText = outputEvents.map((event) => event.content).join("");

  useEffect(() => {
    const activity = activityRef.current;
    if (!activity || events.length === 0) return;
    activity.scrollTop = activity.scrollHeight;
  }, [events.length]);

  if (!workspaceId) {
    return (
      <section className="flex flex-col items-center justify-center gap-4 rounded-md border border-dashed border-line bg-white px-6 py-16 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-panel">
          <Bot className="h-6 w-6 text-ink/55" aria-hidden="true" />
        </div>
        <div>
          <h3 className="text-lg font-semibold text-ink">
            No workspace selected
          </h3>
          <p className="mt-1 text-sm text-ink/60">
            Select a workspace to manage agents
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="grid h-[calc(100vh-10.5rem)] min-h-[560px] grid-rows-[220px_minmax(0,1fr)] overflow-hidden rounded-lg border border-line bg-white xl:grid-cols-[300px_minmax(0,1fr)] xl:grid-rows-1">
      <aside className="flex min-h-0 flex-col border-b border-line bg-panel xl:border-b-0 xl:border-r">
        <div className="shrink-0 border-b border-line px-4 py-4">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted">
            Agent
          </p>
          <h3 className="mt-1 text-lg font-normal tracking-[-0.01em]">
            Run history
          </h3>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <RunHistory
            workspaceId={workspaceId}
            onSelectRun={handleSelectRun}
            selectedRunId={selectedRunId}
          />
        </div>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-col">
        <header className="flex min-h-16 shrink-0 items-center justify-between gap-4 border-b border-line px-5 py-3">
          <div className="min-w-0">
            <h3 className="truncate text-base font-semibold text-ink">
              {runDetail?.prompt || "New agent task"}
            </h3>
            <p className="mt-0.5 text-xs text-muted">
              {selectedRunId
                ? "Workspace agent activity"
                : "Describe a task to start a new run"}
            </p>
          </div>
          {runStatus && (
            <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 py-1 text-xs font-medium capitalize text-body">
              <CircleDot
                className={`h-3 w-3 ${
                  isRunActive ? "animate-pulse text-primary" : "text-muted"
                }`}
                aria-hidden="true"
              />
              {runStatus}
            </span>
          )}
        </header>

        <div
          ref={activityRef}
          className="min-h-0 flex-1 overflow-y-auto bg-canvas px-4 py-6 sm:px-6"
          data-testid="agent-process"
        >
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-5">
            {connectionStatus === "reconnecting" && (
              <Notice tone="warn" testId="reconnecting-status">
                <RefreshCw className="h-4 w-4 animate-spin" />
                Reconnecting... (attempt {reconnectAttempt}/
                {MAX_RECONNECT_RETRIES})
              </Notice>
            )}
            {connectionStatus === "disconnected" && (
              <Notice tone="danger" testId="disconnected-status">
                <WifiOff className="h-4 w-4" />
                Disconnected
              </Notice>
            )}
            {streamError && (
              <Notice tone="danger">
                <AlertCircle className="h-4 w-4" />
                Stream error: {streamError}
              </Notice>
            )}
            {agentError && (
              <Notice tone="danger">
                <AlertCircle className="h-4 w-4" />
                Agent error: {agentError}
              </Notice>
            )}
            {runDetail?.error_message && (
              <Notice tone="danger" testId="run-error-message">
                <AlertCircle className="h-4 w-4" />
                Run error: {runDetail.error_message}
              </Notice>
            )}

            {!selectedRunId ? (
              <div className="flex flex-1 flex-col items-center justify-center px-6 py-20 text-center">
                <div className="flex h-11 w-11 items-center justify-center rounded-full border border-line bg-white text-primary">
                  <Sparkles className="h-5 w-5" aria-hidden="true" />
                </div>
                <h4 className="mt-4 text-lg font-normal text-ink">
                  What should the agent work on?
                </h4>
                <p className="mt-1 max-w-sm text-sm leading-6 text-muted">
                  Ask it to inspect the repository, implement a change, or run
                  checks. Progress will appear here.
                </p>
              </div>
            ) : (
              <>
                {runDetail?.prompt && (
                  <div className="ml-auto max-w-[85%] rounded-lg border border-line bg-white px-4 py-3 text-sm leading-6 text-ink">
                    {runDetail.prompt}
                  </div>
                )}

                <div className="space-y-3">
                  <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted">
                    <Bot className="h-3.5 w-3.5" aria-hidden="true" />
                    Process
                  </div>

                  {outputText && (
                    <div className="whitespace-pre-wrap text-sm leading-6 text-body">
                      {outputText}
                    </div>
                  )}

                  {paired.length === 0 && outputEvents.length > 0 && (
                    <p className="text-xs text-muted">
                      No tool events for this run yet.
                    </p>
                  )}

                  {paired.length === 0 && outputEvents.length === 0 ? (
                    <div className="rounded-md border border-dashed border-line bg-white px-4 py-5 text-sm text-muted">
                      {isRunActive
                        ? "The agent is preparing its next action..."
                        : "No process events were recorded for this run."}
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {paired.map((pe) => {
                        const key =
                          pe.toolCallId ??
                          `unpaired-${pe.call?.id ?? ""}-${pe.result?.id ?? ""}`;
                        return (
                          <ToolCard
                            key={key}
                            event={pe}
                            streamError={
                              pe.result?.event_type === "error"
                                ? streamError
                                : undefined
                            }
                          />
                        );
                      })}
                    </div>
                  )}
                </div>
              </>
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
                {activeWorktree ? activeWorktree.branch : "base branch"}
              </span>
            </p>
            <PromptComposer
              workspaceId={workspaceId}
              worktreeId={activeWorktree?.id ?? null}
              activeRunId={selectedRunId}
              isRunning={isRunActive}
              onRunCreated={handleRunCreated}
              onRunStopped={handleRunStopped}
            />
          </div>
        </div>
      </div>
    </section>
  );
}

function Notice({
  children,
  tone,
  testId,
}: {
  children: ReactNode;
  tone: "warn" | "danger";
  testId?: string;
}) {
  const toneClass =
    tone === "warn"
      ? "border-warn/30 bg-[#fffbf0] text-warn"
      : "border-danger/30 bg-[#fff5f2] text-danger";
  return (
    <div
      className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${toneClass}`}
      data-testid={testId}
    >
      {children}
    </div>
  );
}
