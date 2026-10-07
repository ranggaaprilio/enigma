import { useState } from "react";
import {
  AlertCircle,
  Bot,
  ChevronDown,
  ChevronRight,
  FileDiff,
  MessageSquare,
  RotateCcw,
} from "lucide-react";

import {
  INTERRUPTED_BY_RESTART,
  type AgentEventResponse,
  type AgentRunResponse,
} from "../../lib/api";
import { CommentThread } from "../comments/CommentThread";
import { ChangedFiles } from "./ChangedFiles";
import { Notice } from "./Notice";
import { ToolCard, pairToolEvents } from "./ToolCard";

interface TurnViewProps {
  turn: AgentRunResponse;
  events: AgentEventResponse[];
  isActive: boolean;
  workspaceId: string;
  agentError?: string | null;
  streamError?: string | null;
}

/**
 * Renders a single conversation turn: the user's prompt bubble, the agent's streamed/persisted
 * output text, its tool cards, any per-turn error, and a lazily-loaded "Changed files" section
 * (this is where run-level detail lives now that the flat run-history list is gone).
 */
export function TurnView({
  turn,
  events,
  isActive,
  workspaceId,
  agentError,
  streamError,
}: TurnViewProps) {
  const [showFiles, setShowFiles] = useState(false);
  const [showComments, setShowComments] = useState(false);

  const paired = pairToolEvents(events);
  const outputEvents = events.filter((event) => event.event_type === "output_text");
  const outputText = outputEvents.map((event) => event.content).join("");
  const hasProcess = paired.length > 0 || outputEvents.length > 0;
  // A server restart closed the run out, from the persisted turn or the live terminal event.
  // That is not the agent's failure, so it gets a retry prompt instead of the error notices.
  const interrupted =
    turn.error_reason === INTERRUPTED_BY_RESTART ||
    events.some(
      (event) =>
        event.event_type === "error" && event.metadata?.reason === INTERRUPTED_BY_RESTART,
    );

  return (
    <article className="flex flex-col gap-3" data-testid="agent-turn">
      {turn.prompt && (
        <div className="ml-auto max-w-[85%] rounded-lg border border-line bg-white px-4 py-3 text-sm leading-6 text-ink">
          {turn.prompt}
        </div>
      )}

      <div className="space-y-3">
        <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted">
          <Bot className="h-3.5 w-3.5" aria-hidden="true" />
          {isActive ? "Working" : "Response"}
        </div>

        {interrupted && (
          <Notice tone="warn" testId="turn-interrupted">
            <RotateCcw className="h-4 w-4" aria-hidden="true" />
            Run interrupted by a server restart — please retry.
          </Notice>
        )}

        {agentError && !interrupted && (
          <Notice tone="danger">
            <AlertCircle className="h-4 w-4" />
            Agent error: {agentError}
          </Notice>
        )}

        {turn.error_message && !interrupted && (
          <Notice tone="danger" testId="turn-error-message">
            <AlertCircle className="h-4 w-4" />
            Run error: {turn.error_message}
          </Notice>
        )}

        {outputText && (
          <div className="whitespace-pre-wrap text-sm leading-6 text-body">{outputText}</div>
        )}

        {!hasProcess ? (
          <div className="rounded-md border border-dashed border-line bg-white px-4 py-5 text-sm text-muted">
            {isActive
              ? "The agent is preparing its next action…"
              : "No process events were recorded for this turn."}
          </div>
        ) : (
          <div className="space-y-2">
            {paired.map((pe) => {
              const key =
                pe.toolCallId ?? `unpaired-${pe.call?.id ?? ""}-${pe.result?.id ?? ""}`;
              return (
                <ToolCard
                  key={key}
                  event={pe}
                  streamError={pe.result?.event_type === "error" ? streamError ?? undefined : undefined}
                />
              );
            })}
          </div>
        )}

        {!isActive && (
          <div>
            <button
              type="button"
              onClick={() => setShowFiles((value) => !value)}
              className="inline-flex items-center gap-1.5 text-xs font-medium text-primary transition hover:text-primary-active"
            >
              {showFiles ? (
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              <FileDiff className="h-3.5 w-3.5" aria-hidden="true" />
              Changed files
            </button>
            {showFiles && (
              <div className="mt-2">
                <ChangedFiles workspaceId={workspaceId} runId={turn.id} />
              </div>
            )}
            <button
              type="button"
              onClick={() => setShowComments((value) => !value)}
              className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-primary transition hover:text-primary-active"
            >
              {showComments ? (
                <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
              )}
              <MessageSquare className="h-3.5 w-3.5" aria-hidden="true" />
              Comments
            </button>
            {showComments && (
              <div className="mt-2">
                <CommentThread targetType="run" targetId={turn.id} />
              </div>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
